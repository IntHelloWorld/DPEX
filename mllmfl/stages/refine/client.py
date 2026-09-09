import base64
import copy
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

import requests

from .context import build_system_prompt


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_VISION_MODEL = "gpt-5.4"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
REASONING_EFFORTS = {
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
}
API_STYLES = {"responses", "chat_completions"}
DEFAULT_INVALID_FINAL_RESPONSE_RETRIES = 2


def invalid_final_response_retries(config: Dict[str, Any]) -> int:
    cfg = config.get("mllm", config)
    value = int(
        cfg.get(
            "invalid_final_response_retries",
            cfg.get(
                "invalid_final_json_retries",
                DEFAULT_INVALID_FINAL_RESPONSE_RETRIES,
            ),
        )
    )
    if value < 0:
        raise ValueError("invalid_final_response_retries must be non-negative")
    return value


def invalid_final_json_retries(config: Dict[str, Any]) -> int:
    """Backward-compatible alias for the former JSON response setting."""
    return invalid_final_response_retries(config)


def _response_message(data: Dict[str, Any]) -> Dict[str, Any]:
    output = data.get("output")
    if not isinstance(output, list):
        raise ValueError("model response output is not an array")

    content_parts = []
    reasoning_parts = []
    tool_calls = []
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("model response output item is not an object")
        item_type = item.get("type")
        if item_type == "reasoning":
            summary = item.get("summary") or []
            if not isinstance(summary, list):
                raise ValueError("model reasoning summary is not an array")
            for part in summary:
                if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                    raise ValueError("model reasoning summary item is invalid")
                reasoning_parts.append(part["text"])
        elif item_type == "message":
            content = item.get("content") or []
            if not isinstance(content, list):
                raise ValueError("model response message content is not an array")
            for part in content:
                if not isinstance(part, dict):
                    raise ValueError("model response content item is not an object")
                if part.get("type") != "output_text" or not isinstance(
                    part.get("text"), str
                ):
                    raise ValueError("model response content item is not output_text")
                content_parts.append(part["text"])
        elif item_type == "function_call":
            call_id = str(item.get("call_id") or "")
            name = str(item.get("name") or "")
            arguments = item.get("arguments")
            if not call_id or not name or not isinstance(arguments, str):
                raise ValueError("model function_call item is invalid")
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            })

    normalized: Dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) if content_parts else None,
    }
    if reasoning_parts:
        normalized["reasoning_content"] = "\n".join(reasoning_parts)
    if tool_calls:
        normalized["tool_calls"] = tool_calls
    return normalized


def _response_input(items: Sequence[Dict[str, Any]]) -> list[Dict[str, Any]]:
    normalized = []
    for item in copy.deepcopy(list(items)):
        if item.get("type") in {
            "reasoning", "message", "function_call", "function_call_output",
        }:
            normalized.append(item)
            continue
        role = item.get("role")
        if role == "tool":
            call_id = str(item.get("tool_call_id") or "")
            output = item.get("content")
            if not call_id or not isinstance(output, str):
                raise ValueError("tool result is missing tool_call_id or text content")
            normalized.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            })
            continue
        if role not in {"user", "assistant", "system", "developer"}:
            raise ValueError(f"unsupported Responses input role: {role}")
        content = item.get("content")
        if isinstance(content, str):
            normalized.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise ValueError("Responses message content is not text or an array")
        converted_parts = []
        for part in content:
            if not isinstance(part, dict):
                raise ValueError("Responses content item is not an object")
            part_type = part.get("type")
            if part_type == "text":
                text = part.get("text")
                if not isinstance(text, str):
                    raise ValueError("Responses text content is invalid")
                converted_parts.append({
                    "type": "output_text" if role == "assistant" else "input_text",
                    "text": text,
                })
            elif part_type == "image_url" and role == "user":
                image_url = part.get("image_url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                if not isinstance(image_url, str) or not image_url:
                    raise ValueError("Responses image_url content is invalid")
                converted_parts.append({
                    "type": "input_image",
                    "image_url": image_url,
                })
            else:
                raise ValueError(f"unsupported Responses content type: {part_type}")
        normalized.append({"role": role, "content": converted_parts})
    return normalized


def _chat_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    if tool.get("type") != "function":
        raise ValueError("Chat Completions supports only function tools")
    function = {
        key: copy.deepcopy(value)
        for key, value in tool.items()
        if key != "type"
    }
    return {"type": "function", "function": function}


def _chat_messages(
    items: Sequence[Dict[str, Any]], instructions: str
) -> list[Dict[str, Any]]:
    messages = [{"role": "system", "content": instructions}]
    for item in copy.deepcopy(list(items)):
        role = item.get("role")
        if role not in {"user", "assistant", "system", "developer", "tool"}:
            raise ValueError(f"unsupported Chat Completions message role: {role}")
        if role == "developer":
            role = "system"
        message = dict(item)
        message["role"] = role
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in {
                    "text", "image_url",
                }:
                    raise ValueError("unsupported Chat Completions content item")
        elif content is not None and not isinstance(content, str):
            raise ValueError("Chat Completions content is not text or an array")
        messages.append(message)
    return messages


def _chat_response_message(data: Dict[str, Any]) -> Dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("model response choices is not a non-empty array")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ValueError("model response message is not an object")
    message = copy.deepcopy(choice["message"])
    message["role"] = "assistant"
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("model response content is not text")
    reasoning = message.get("reasoning_content")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ValueError("model response reasoning_content is not text")
    tool_calls = message.get("tool_calls")
    if tool_calls is not None and (
        not isinstance(tool_calls, list)
        or not all(isinstance(item, dict) for item in tool_calls)
    ):
        raise ValueError("model response tool_calls is not an array of objects")
    return message


def _post_response(
    config: Dict[str, Any],
    input_items: Sequence[Dict[str, Any]],
    timeout: int,
    *,
    previous_response_id: str | None = None,
    prompt_cache_key: str | None = None,
    instructions: str | None = None,
    tools: Sequence[Dict[str, Any]] | None = None,
    max_tokens: int | None = None,
) -> tuple[
    Dict[str, Any], str, str, List[Dict[str, Any]], Dict[str, Any], str | None,
]:
    """Post one stateless OpenAI-compatible model request.

    The historical name is retained because tests and callers patch this import path.
    The fourth item contains the complete response output for replay; the final item is
    the provider finish reason when that API exposes one.
    """
    cfg = config.get("mllm", config)
    if cfg.get("api_key"):
        raise ValueError("inline api_key is forbidden; configure api_key_env")
    env_name = str(cfg.get("api_key_env") or DEFAULT_API_KEY_ENV)
    api_key = os.environ.get(env_name)
    if not api_key:
        raise ValueError(f"API key missing from {env_name}")
    base_url = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    model = str(
        cfg.get("vision_model") or cfg.get("model") or DEFAULT_VISION_MODEL
    )
    reasoning_effort = str(cfg.get("reasoning_effort") or "medium").lower()
    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(
            "reasoning_effort must be none, minimal, low, medium, high, xhigh, or max"
        )
    selected_max_tokens = (
        int(cfg.get("max_tokens", 4096)) if max_tokens is None else max_tokens
    )
    if (
        not isinstance(selected_max_tokens, int)
        or isinstance(selected_max_tokens, bool)
        or selected_max_tokens <= 0
    ):
        raise ValueError("max_tokens must be positive")

    api_style = str(cfg.get("api_style") or "responses")
    if api_style not in API_STYLES:
        raise ValueError("api_style must be responses or chat_completions")
    selected_instructions = instructions or build_system_prompt(
        int(cfg.get("top_k", 5))
    )
    if tools is None:
        raise ValueError("refinement tools must be provided")
    selected_tools = copy.deepcopy(list(tools))

    if api_style == "chat_completions":
        payload: Dict[str, Any] = {
            "model": model,
            "messages": _chat_messages(input_items, selected_instructions),
            "reasoning_effort": reasoning_effort,
            "thinking": {"type": "enabled"},
            "max_tokens": selected_max_tokens,
            "tools": [_chat_tool(tool) for tool in selected_tools],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }
        endpoint = base_url + "/chat/completions"
    else:
        payload = {
            "model": model,
            "instructions": selected_instructions,
            "input": _response_input(input_items),
            "reasoning": {"effort": reasoning_effort},
            "parallel_tool_calls": False,
            "max_output_tokens": selected_max_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        payload["tools"] = selected_tools
        payload["tool_choice"] = "auto"
        endpoint = base_url + "/responses"

    retry = cfg.get("retry") or {}
    attempts = int(retry.get("max_retries", 3))
    backoff = float(retry.get("backoff_s", 2))
    if attempts <= 0:
        raise ValueError("max_retries must be positive")
    last_error = ""
    for attempt in range(attempts):
        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"HTTP {response.status_code}: {response.text[:500]}"
                )
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("model response is not an object")
            response_id = str(data.get("id") or "")
            if not response_id:
                raise ValueError("model response id is missing")
            if api_style == "chat_completions":
                message = _chat_response_message(data)
                output = [copy.deepcopy(message)]
                choice = data["choices"][0]
                finish_reason = choice.get("finish_reason")
                if finish_reason is not None and not isinstance(
                    finish_reason, str
                ):
                    raise ValueError("model finish_reason is not text")
            else:
                status = data.get("status")
                if status not in {None, "completed"}:
                    raise ValueError(
                        f"model response status is not completed: {status}"
                    )
                output = data.get("output")
                if not isinstance(output, list) or not all(
                    isinstance(item, dict) for item in output
                ):
                    raise ValueError(
                        "model response output is not an array of objects"
                    )
                message = _response_message(data)
                finish_reason = None
            usage = data.get("usage")
            if usage is None:
                usage = {}
            if not isinstance(usage, dict):
                raise ValueError("model response usage is not an object")
            return (
                message,
                str(data.get("model") or model),
                response_id,
                copy.deepcopy(output),
                copy.deepcopy(usage),
                finish_reason,
            )
        except (
            requests.RequestException,
            RuntimeError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as error:
            last_error = str(error)
            if attempt + 1 < attempts:
                time.sleep(min(backoff * (2**attempt), 15))
    raise RuntimeError(last_error or "model request failed")


def _image_part(path: Path) -> Dict[str, Any]:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{encoded}"},
    }
