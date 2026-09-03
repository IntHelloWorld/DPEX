import copy
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any, Dict, List, Sequence

from mllmfl.infrastructure.io import append_jsonl, write_text
from mllmfl.infrastructure.method_location import resolve_source_method_reference
from .client import _image_part, _post_response, invalid_final_json_retries

from .context import build_system_prompt
from .graphs import (
    EXECUTION_GRAPH_TOOL,
    FIND_METHOD_INVOCATION_ID_TOOL,
    MethodExecutionGraphs,
)
from .parsing import (
    parse_method_line,
    parse_model_response,
    validate_model_refinement,
)
from .shell import BASH_TOOL, execute_bash


def finalization_limits(config: Dict[str, Any]) -> tuple[int, int]:
    cfg = config.get("mllm", config)
    base_max_tokens = int(cfg.get("max_tokens", 4096))
    final_retry_max_tokens = int(
        cfg.get("final_length_retry_max_tokens", base_max_tokens)
    )
    if base_max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if final_retry_max_tokens < base_max_tokens:
        raise ValueError(
            "final_length_retry_max_tokens must be >= max_tokens"
        )
    return base_max_tokens, final_retry_max_tokens


def run_agent(
    config: Dict[str, Any],
    prompt: str,
    candidates: Sequence[Dict[str, Any]],
    candidate_method_ids: Dict[str, str],
    graphs: MethodExecutionGraphs,
    workspace: Path,
    timeout: int,
    conversation_path: Path | None,
    top_k: int,
) -> Dict[str, Any]:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    cfg = config.get("mllm", config)
    retries = invalid_final_json_retries(config)
    system_prompt = build_system_prompt(top_k)
    tools = [FIND_METHOD_INVOCATION_ID_TOOL, EXECUTION_GRAPH_TOOL, BASH_TOOL]
    pending_input: List[Dict[str, Any]] = [{
        "role": "user",
        "content": [{"type": "text", "text": prompt}],
    }]
    history: List[Dict[str, Any]] = []
    mapped_method_ids = set(candidate_method_ids.values())
    candidate_by_location = {
        (str(item["source_file"]), int(item["start_line"]), int(item["end_line"])):
        str(item["candidate_id"])
        for item in candidates
    }
    tool_rounds = 0
    terminal_commands = 0
    final_retry_count = 0
    model = ""
    cache_prefix = (
        "mllmfl-refine-"
        + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:32]
    )
    prompt_cache_key = cache_prefix + "-" + secrets.token_hex(4)
    terminal_timeout = min(timeout, int(cfg.get("terminal_timeout", 30)))
    if terminal_timeout <= 0:
        raise ValueError("terminal timeout must be positive")
    base_max_tokens, final_retry_max_tokens = finalization_limits(config)
    finalization_attempts: list[Dict[str, Any]] = []
    total_usage: Dict[str, Any] = {}
    request_count = 0

    usage_path = (
        conversation_path.with_name("refine_response_usage.jsonl")
        if conversation_path is not None else None
    )
    if conversation_path is not None:
        write_text(conversation_path, "")
        write_text(usage_path, "")
        append_jsonl(conversation_path, {"role": "system", "content": system_prompt})
        append_jsonl(conversation_path, pending_input[0])

    def record(path: Path | None, value: Dict[str, Any]) -> None:
        if path is not None:
            append_jsonl(path, value)

    def add_usage(target: Dict[str, Any], source: Dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                target[key] = target.get(key, 0) + value
            elif isinstance(value, dict):
                child = target.setdefault(key, {})
                if isinstance(child, dict):
                    add_usage(child, value)

    def hydrate(items: Sequence[Dict[str, Any]]) -> list[Dict[str, Any]]:
        result = copy.deepcopy(list(items))
        for item in result:
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for index, part in enumerate(content):
                if not isinstance(part, dict) or part.get("type") != "image_ref":
                    continue
                diagram_id = str(part.get("diagram_id") or "")
                content[index] = _image_part(graphs.image_path(diagram_id))
        return result

    def final_error(content: str) -> str:
        if mapped_method_ids and not graphs.viewed:
            return "at least one runtime method graph must be viewed"
        parsed = parse_model_response(content)
        if parsed is None:
            return "response is not valid JSON"
        try:
            ranking = validate_model_refinement(parsed, top_k)
            resolved = []
            for item in ranking:
                method = item["method"]
                source_file, line = parse_method_line(method["line"])
                location, _ = resolve_source_method_reference(
                    workspace, source_file, line, method["name"]
                )
                resolved.append((item, location))
        except ValueError as error:
            return str(error)
        returned_candidate_ids = {
            candidate_by_location.get((
                location.source_file, location.start_line, location.end_line,
            ))
            for _, location in resolved
        }
        returned_candidate_ids.discard(None)
        required_method_ids = {
            candidate_method_ids[candidate_id]
            for candidate_id in returned_candidate_ids
            if candidate_id in candidate_method_ids
        }
        missing_method_ids = required_method_ids - set(graphs.inspected_method_ids)
        if missing_method_ids:
            return (
                "every retained runtime candidate must be inspected; missing: "
                + ", ".join(sorted(missing_method_ids))
            )
        if (
            len(returned_candidate_ids) < len(resolved)
            or any(candidate_id not in candidate_method_ids
                   for candidate_id in returned_candidate_ids)
        ) and terminal_commands == 0:
            return (
                "new methods and retained candidates absent from the trace require "
                "source inspection"
            )
        return ""

    while True:
        request_input = [*history, *pending_input]
        requested_max_tokens = base_max_tokens
        while True:
            (
                message, model, response_id, response_output, usage,
                finish_reason,
            ) = _post_response(
                config,
                hydrate(request_input),
                timeout,
                previous_response_id=None,
                prompt_cache_key=prompt_cache_key,
                instructions=system_prompt,
                tools=tools,
                max_tokens=requested_max_tokens,
            )
            request_count += 1
            add_usage(total_usage, usage)
            tool_calls = message.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                raise ValueError("model tool_calls is not an array")
            if len(tool_calls) > 1:
                tool_calls = copy.deepcopy(tool_calls[:1])
                message = copy.deepcopy(message)
                message["tool_calls"] = copy.deepcopy(tool_calls)
                retained = str(tool_calls[0].get("id") or "")
                retained_output = []
                for item in response_output:
                    if item.get("type") == "function_call":
                        if str(item.get("call_id") or "") == retained:
                            retained_output.append(item)
                        continue
                    replay_item = copy.deepcopy(item)
                    replay_calls = replay_item.get("tool_calls")
                    if isinstance(replay_calls, list):
                        replay_item["tool_calls"] = [
                            call for call in replay_calls
                            if str(call.get("id") or "") == retained
                        ]
                    retained_output.append(replay_item)
                response_output = retained_output
            record(usage_path, {
                "schema": "responses-usage",
                "schema_version": 2,
                "previous_response_id": None,
                "response_id": response_id,
                "finish_reason": finish_reason,
                "requested_max_tokens": requested_max_tokens,
                "usage": usage,
            })
            record(conversation_path, message)
            content = message.get("content")
            content_empty = (
                not isinstance(content, str) or not content.strip()
            )
            if not tool_calls:
                finalization_attempts.append({
                    "response_id": response_id,
                    "max_tokens": requested_max_tokens,
                    "finish_reason": finish_reason,
                    "content_empty": content_empty,
                    "usage": copy.deepcopy(usage),
                })
                if (
                    finish_reason == "length"
                    and content_empty
                    and requested_max_tokens < final_retry_max_tokens
                ):
                    requested_max_tokens = min(
                        requested_max_tokens * 2, final_retry_max_tokens
                    )
                    continue
            break
        history.extend(copy.deepcopy(pending_input))
        history.extend(copy.deepcopy(response_output))

        if tool_calls:
            tool_rounds += 1
            call = tool_calls[0]
            call_id = str(call.get("id") or "missing-tool-call-id")
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            try:
                arguments = json.loads(str(function.get("arguments") or "{}"))
            except json.JSONDecodeError:
                arguments = None
            image_id = ""
            if name == "find_method_invocation_id":
                result = graphs.find_invocation_ids(arguments)
                image_path = None
            elif name == "inspect_execution_graph":
                result, image_path = graphs.inspect(arguments)
                if image_path is not None:
                    image_id = graphs.viewed_diagram_id(
                        str(result["invocation_id"])
                    )
            elif name == "bash":
                terminal_commands += 1
                command = (
                    arguments.get("command") if isinstance(arguments, dict) else None
                )
                max_output_chars = (
                    arguments.get("max_output_chars")
                    if isinstance(arguments, dict) else None
                )
                result = execute_bash(
                    command,
                    max_output_chars,
                    workspace,
                    terminal_timeout,
                )
                image_path = None
            else:
                result = {"ok": False, "error": f"unsupported tool: {name}"}
                image_path = None
            tool_item = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False),
            }
            pending_input = [tool_item]
            record(conversation_path, tool_item)
            if image_path is not None:
                image_item = {
                    "role": "user",
                    "content": [{"type": "image_ref", "diagram_id": image_id}],
                }
                pending_input.append(image_item)
                record(conversation_path, image_item)
            continue

        content = message.get("content")
        validation_error = (
            "response content is empty"
            if not isinstance(content, str) or not content.strip()
            else final_error(content)
        )
        if validation_error and final_retry_count < retries:
            final_retry_count += 1
            correction = {
                "role": "user",
                "content": (
                    f"Your final response was invalid: {validation_error}. Retry now "
                    "with exactly one JSON array matching the Output Contract."
                ),
            }
            pending_input = [correction]
            record(conversation_path, correction)
            continue
        if validation_error:
            raise ValueError(f"invalid final response: {validation_error}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model returned neither a tool call nor final text")
        parsed = parse_model_response(content)
        if parsed is None:
            raise ValueError("model returned invalid final JSON")
        ranking = validate_model_refinement(parsed, top_k)
        resolved_ranking = []
        for item in ranking:
            method = item["method"]
            source_file, line = parse_method_line(method["line"])
            location, signature = resolve_source_method_reference(
                workspace, source_file, line, method["name"]
            )
            resolved_ranking.append({
                **item,
                **location.to_dict(),
                "signature": signature,
                "input_candidate_id": candidate_by_location.get((
                    location.source_file, location.start_line, location.end_line,
                )),
            })
        return {
            "model": model,
            "ranking": resolved_ranking,
            "tool_rounds": tool_rounds,
            "diagram_view_count": len(graphs.viewed),
            "viewed_diagrams": list(graphs.viewed),
            "inspected_method_ids": list(graphs.inspected_method_ids),
            "inspected_invocation_ids": list(graphs.inspected_invocation_ids),
            "queried_methods": [dict(item) for item in graphs.queried_methods],
            "terminal_command_count": terminal_commands,
            "finalization_attempts": finalization_attempts,
            "request_count": request_count,
            "usage": total_usage,
            "finalization_attempt_count": len(finalization_attempts),
            "final_length_retry_count": sum(
                1 for item in finalization_attempts
                if item["finish_reason"] == "length" and item["content_empty"]
            ),
            "final_finish_reason": finalization_attempts[-1]["finish_reason"],
        }
