"""
OpenAI Router - Handles OpenAI format API requests
处理OpenAI格式请求的路由模块
"""

import json
import time
import uuid
import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import (
    get_available_models,
    is_fake_streaming_model,
    is_anti_truncation_model,
    get_base_model_from_feature_model,
    get_anti_truncation_max_attempts,
    is_image_model,
    get_gemini_retry_if_no_image_enabled,
    get_gemini_retry_if_no_image_max_attempts,
)
from log import log
from .anti_truncation import apply_anti_truncation_to_stream
from .credential_manager import CredentialManager
from .google_chat_api import send_gemini_request
from .models import ChatCompletionRequest, ModelList, Model
from .task_manager import create_managed_task
from .openai_transfer import (
    openai_request_to_gemini_payload,
    gemini_response_to_openai,
    gemini_stream_chunk_to_openai,
    _convert_usage_metadata,
)

# 创建路由器
router = APIRouter()
security = HTTPBearer()

# 全局凭证管理器实例
credential_manager = None


@asynccontextmanager
async def get_credential_manager():
    """获取全局凭证管理器实例"""
    global credential_manager
    if not credential_manager:
        credential_manager = CredentialManager()
        await credential_manager.initialize()
    yield credential_manager


async def authenticate(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """验证用户密码"""
    from config import get_api_password

    password = await get_api_password()
    token = credentials.credentials
    if token != password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="密码错误")
    return token


@router.get("/v1/models", response_model=ModelList)
async def list_models():
    """返回OpenAI格式的模型列表"""
    models = get_available_models("openai")
    return ModelList(data=[Model(id=m) for m in models])


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, token: str = Depends(authenticate)):
    """处理OpenAI格式的聊天完成请求"""

    # 获取原始请求数据
    try:
        raw_data = await request.json()
    except Exception as e:
        log.error(f"Failed to parse JSON request: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    # 创建请求对象
    try:
        request_data = ChatCompletionRequest(**raw_data)
    except Exception as e:
        log.error(f"Request validation failed: {e}")
        raise HTTPException(
            status_code=400, detail=f"Request validation error: {str(e)}"
        )

    # 健康检查
    if (
        len(request_data.messages) == 1
        and getattr(request_data.messages[0], "role", None) == "user"
        and getattr(request_data.messages[0], "content", None) == "Hi"
    ):
        return JSONResponse(
            content={
                "choices": [
                    {"message": {"role": "assistant", "content": "gcli2api正常工作中"}}
                ]
            }
        )

    # 限制max_tokens
    if (
        getattr(request_data, "max_tokens", None) is not None
        and request_data.max_tokens is not None
        and request_data.max_tokens > 65535
    ):
        request_data.max_tokens = 65535

    # 覆写 top_k 为 64
    setattr(request_data, "top_k", 64)

    # 过滤空消息
    filtered_messages = []
    for m in request_data.messages:
        content = getattr(m, "content", None)
        if content:
            if isinstance(content, str) and content.strip():
                filtered_messages.append(m)
            elif isinstance(content, list) and len(content) > 0:
                has_valid_content = False
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text" and part.get("text", "").strip():
                            has_valid_content = True
                            break
                        elif part.get("type") == "image_url" and part.get(
                            "image_url", {}
                        ).get("url"):
                            has_valid_content = True
                            break
                if has_valid_content:
                    filtered_messages.append(m)

    request_data.messages = filtered_messages

    # 处理模型名称和功能检测
    model = request_data.model
    log.info(f"收到 OpenAI 格式请求, 模型: {model}")
    use_fake_streaming = is_fake_streaming_model(model)
    use_anti_truncation = is_anti_truncation_model(model)

    # 获取基础模型名
    real_model = get_base_model_from_feature_model(model)
    request_data.model = real_model

    # 获取凭证管理器
    from src.credential_manager import get_credential_manager

    cred_mgr = await get_credential_manager()

    # 获取有效凭证
    credential_result = await cred_mgr.get_valid_credential()
    if not credential_result:
        log.error("当前无可用凭证，请去控制台获取")
        raise HTTPException(status_code=500, detail="当前无可用凭证，请去控制台获取")

    current_file = credential_result
    log.debug(f"Using credential: {current_file}")

    # 增加调用计数
    cred_mgr.increment_call_count()

    # 转换为Gemini API payload格式
    try:
        api_payload = await openai_request_to_gemini_payload(request_data)
    except Exception as e:
        log.error(f"OpenAI to Gemini conversion failed: {e}")
        raise HTTPException(status_code=500, detail="Request conversion failed")

    # 处理假流式
    if use_fake_streaming and getattr(request_data, "stream", False):
        request_data.stream = False
        return await fake_stream_response(api_payload, cred_mgr)

    # 处理抗截断 (仅流式传输时有效)
    is_streaming = getattr(request_data, "stream", False)
    if use_anti_truncation and is_streaming:
        log.info("启用流式抗截断功能")
        max_attempts = await get_anti_truncation_max_attempts()

        # 使用流式抗截断处理器
        gemini_response = await apply_anti_truncation_to_stream(
            lambda api_payload: send_gemini_request(
                api_payload, is_streaming, cred_mgr
            ),
            api_payload,
            max_attempts,
        )

        return await convert_streaming_response(gemini_response, model)
    elif use_anti_truncation and not is_streaming:
        log.warning("抗截断功能仅在流式传输时有效，非流式请求将忽略此设置")

    # 发送请求
    is_streaming = getattr(request_data, "stream", False)
    log.debug(f"Sending request: streaming={is_streaming}, model={real_model}")

    # 如果是流式响应，直接发送并返回
    if is_streaming:
        response = await send_gemini_request(api_payload, True, cred_mgr)
        return await convert_streaming_response(response, model)

    # --- 非流式响应处理，增加“无图重试”逻辑 ---
    retry_enabled = await get_gemini_retry_if_no_image_enabled()
    max_attempts = await get_gemini_retry_if_no_image_max_attempts()
    is_img_model = is_image_model(real_model)

    response = None
    response_data = None
    attempts = 0

    if retry_enabled and is_img_model and not is_streaming:
        while attempts < max_attempts:
            attempts += 1
            log.info(
                f"Attempt {attempts}/{max_attempts} for image model '{real_model}' (OpenAI format)"
            )

            # 发送请求
            response = await send_gemini_request(api_payload, False, cred_mgr)

            # 解析响应
            try:
                body_to_decode = getattr(
                    response, "body", getattr(response, "content", None)
                )
                if body_to_decode:
                    response_data = json.loads(
                        body_to_decode.decode()
                        if isinstance(body_to_decode, bytes)
                        else body_to_decode
                    )
                else:
                    response_data = json.loads(str(response))

                # 检查原始Gemini响应中是否包含图片
                if "candidates" in response_data and response_data["candidates"]:
                    candidate = response_data["candidates"][0]
                    if "content" in candidate and "parts" in candidate["content"]:
                        parts = candidate["content"]["parts"]
                        has_image = any(
                            "inlineData" in part or "fileData" in part for part in parts
                        )
                        if has_image:
                            log.info(
                                f"Image found in Gemini response on attempt {attempts}. Success."
                            )
                            break  # 成功获取图片，跳出循环
                        else:
                            log.warning(
                                f"No image found in Gemini response on attempt {attempts}. Retrying..."
                            )
                else:
                    log.warning(
                        f"Invalid Gemini response structure on attempt {attempts}: {response_data}"
                    )

            except Exception as e:
                log.error(f"Error processing response on attempt {attempts}: {e}")

            if attempts < max_attempts:
                await asyncio.sleep(1)
    else:
        # 原始逻辑：发送一次请求
        response = await send_gemini_request(api_payload, False, cred_mgr)
        try:
            body_to_decode = getattr(
                response, "body", getattr(response, "content", None)
            )
            if body_to_decode:
                response_data = json.loads(
                    body_to_decode.decode()
                    if isinstance(body_to_decode, bytes)
                    else body_to_decode
                )
            else:
                response_data = json.loads(str(response))
        except Exception as e:
            log.error(f"Response decoding failed: {e}")
            log.error(f"Response object: {response}")
            raise HTTPException(status_code=500, detail="Response decoding failed")

    # 转换并返回最终的响应
    if not response_data:
        raise HTTPException(
            status_code=500, detail="Failed to get a valid response after all attempts."
        )

    try:
        openai_response = gemini_response_to_openai(response_data, model)
        return JSONResponse(content=openai_response)
    except Exception as e:
        log.error(f"Final response conversion to OpenAI format failed: {e}")
        raise HTTPException(status_code=500, detail="Response conversion failed")


async def fake_stream_response(
    api_payload: dict, cred_mgr: CredentialManager
) -> StreamingResponse:
    """处理假流式响应"""

    async def stream_generator():
        try:
            # 发送心跳
            heartbeat = {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ]
            }
            yield f"data: {json.dumps(heartbeat)}\n\n".encode()

            # 异步发送实际请求
            async def get_response():
                return await send_gemini_request(api_payload, False, cred_mgr)

            # 创建请求任务
            response_task = create_managed_task(
                get_response(), name="openai_fake_stream_request"
            )

            try:
                # 每3秒发送一次心跳，直到收到响应
                while not response_task.done():
                    await asyncio.sleep(3.0)
                    if not response_task.done():
                        yield f"data: {json.dumps(heartbeat)}\n\n".encode()

                # 获取响应结果
                response = await response_task

            except asyncio.CancelledError:
                # 取消任务并传播取消
                response_task.cancel()
                try:
                    await response_task
                except asyncio.CancelledError:
                    pass
                raise
            except Exception as e:
                # 取消任务并处理其他异常
                response_task.cancel()
                try:
                    await response_task
                except asyncio.CancelledError:
                    pass
                log.error(f"Fake streaming request failed: {e}")
                raise

            # 发送实际请求
            # response 已在上面获取

            # 处理结果
            if hasattr(response, "body"):
                body_str = (
                    response.body.decode()
                    if isinstance(response.body, bytes)
                    else str(response.body)
                )
            elif hasattr(response, "content"):
                body_str = (
                    response.content.decode()
                    if isinstance(response.content, bytes)
                    else str(response.content)
                )
            else:
                body_str = str(response)

            try:
                response_data = json.loads(body_str)

                # 从Gemini响应中提取内容，使用思维链分离逻辑
                content = ""
                reasoning_content = ""
                if "candidates" in response_data and response_data["candidates"]:
                    # Gemini格式响应 - 使用思维链分离
                    from .openai_transfer import _extract_content_and_reasoning

                    candidate = response_data["candidates"][0]
                    if "content" in candidate and "parts" in candidate["content"]:
                        parts = candidate["content"]["parts"]
                        content, reasoning_content = _extract_content_and_reasoning(
                            parts
                        )
                elif "choices" in response_data and response_data["choices"]:
                    # OpenAI格式响应
                    content = (
                        response_data["choices"][0]
                        .get("message", {})
                        .get("content", "")
                    )

                # 如果没有正常内容但有思维内容，给出警告
                if not content and reasoning_content:
                    log.warning("Fake stream response contains only thinking content")
                    content = "[模型正在思考中，请稍后再试或重新提问]"

                if content:
                    # 构建响应块，包括思维内容（如果有）
                    delta = {"role": "assistant", "content": content}
                    if reasoning_content:
                        delta["reasoning_content"] = reasoning_content

                    # 转换usageMetadata为OpenAI格式
                    usage = _convert_usage_metadata(response_data.get("usageMetadata"))

                    # 构建完整的OpenAI格式的流式响应块
                    content_chunk = {
                        "id": str(uuid.uuid4()),
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "gcli2api-streaming",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": "stop"}
                        ],
                    }

                    # 只有在有usage数据时才添加usage字段（确保在最后一个chunk中）
                    if usage:
                        content_chunk["usage"] = usage

                    yield f"data: {json.dumps(content_chunk)}\n\n".encode()
                else:
                    log.warning(f"No content found in response: {response_data}")
                    # 如果完全没有内容，提供默认回复
                    error_chunk = {
                        "id": str(uuid.uuid4()),
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "gcli2api-streaming",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": "[响应为空，请重新尝试]",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    }
                    yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            except json.JSONDecodeError:
                error_chunk = {
                    "id": str(uuid.uuid4()),
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "gcli2api-streaming",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": body_str},
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(error_chunk)}\n\n".encode()

            yield "data: [DONE]\n\n".encode()

        except Exception as e:
            log.error(f"Fake streaming error: {e}")
            error_chunk = {
                "id": str(uuid.uuid4()),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "gcli2api-streaming",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": f"Error: {str(e)}"},
                        "finish_reason": "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            yield "data: [DONE]\n\n".encode()

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


async def convert_streaming_response(gemini_response, model: str) -> StreamingResponse:
    """转换流式响应为OpenAI格式"""
    response_id = str(uuid.uuid4())

    async def openai_stream_generator():
        try:
            # 处理不同类型的响应对象
            if hasattr(gemini_response, "body_iterator"):
                # FastAPI StreamingResponse
                async for chunk in gemini_response.body_iterator:
                    if not chunk:
                        continue

                    # 更具容错性地处理数据块
                    try:
                        payload_str = (
                            chunk.decode("utf-8")
                            if isinstance(chunk, bytes)
                            else str(chunk)
                        )

                        # 移除 SSE 的 "data: " 前缀（如果存在）
                        if payload_str.startswith("data: "):
                            payload_str = payload_str[len("data: ") :]

                        # 忽略空的或只有空格的 payload
                        payload_str = payload_str.strip()
                        if not payload_str:
                            continue

                        # 忽略 SSE 的结束标记
                        if payload_str == "[DONE]":
                            continue

                        gemini_chunk = json.loads(payload_str)
                        openai_chunk = gemini_stream_chunk_to_openai(
                            gemini_chunk, model, response_id
                        )

                        # 确保转换后的 chunk 有内容再发送
                        if openai_chunk and openai_chunk.get("choices"):
                            # 确保 choices[0].delta 有内容或 finish_reason
                            first_choice = openai_chunk["choices"][0]
                            if first_choice.get("delta") or first_choice.get(
                                "finish_reason"
                            ):
                                yield f"data: {json.dumps(openai_chunk, separators=(',',':'))}\n\n".encode()

                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        log.debug(f"无法解析流式数据块，已跳过: {chunk}. 错误: {e}")
                        continue
            else:
                # 其他类型的响应，尝试直接处理
                log.warning(f"Unexpected response type: {type(gemini_response)}")
                error_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "Response type error",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(error_chunk)}\n\n".encode()

            # 发送结束标记
            yield "data: [DONE]\n\n".encode()

        except Exception as e:
            log.error(f"Stream conversion error: {e}")
            error_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": f"Stream error: {str(e)}",
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            yield "data: [DONE]\n\n".encode()

    return StreamingResponse(openai_stream_generator(), media_type="text/event-stream")
