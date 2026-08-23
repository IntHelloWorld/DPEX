import base64
import copy
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import requests

from mllmfl.domain.models import Ranking
from mllmfl.domain.failure import extract_error_stack
from mllmfl.domain.interaction import (
    IMAGE_ONLY_MODE,
    localization_interaction_mode,
)
from mllmfl.domain.schemas import (
    validate_candidates,
    validate_defect_context,
    validate_localization,
    validate_uml_index,
)
from mllmfl.domain.test_slice import validate_slice_metadata
from mllmfl.infrastructure.io import append_jsonl, read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout

from .client import (
    _image_part,
    _post_response,
    _response_message,
)
from .context import (
    DIAGRAM_TOOL,
    IMAGE_ONLY_DIAGRAM_TOOL,
    IMAGE_ONLY_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    _numbered_code,
    build_prompt,
    defect_output_context,
    invalid_final_json_retries,
    system_prompt,
    test_code_context,
)
from .parsing import (
    attach_source_locations,
    gate_method_id_ranking,
    gate_ranking,
    parse_model_response,
    validate_model_ranking_payload,
)


def run_agent(
    config: Dict[str, Any],
    prompt: str,
    uml_index: Dict[str, Any],
    directory: Path,
    timeout: int,
    conversation_path: Path | None = None,
    top_k: int | None = None,
) -> tuple[str, str, List[str], int, int]:
    interaction_mode = localization_interaction_mode(config)
    cfg = config.get("mllm", config)
    selected_top_k = top_k if top_k is not None else int(cfg.get("top_k", 5))
    if selected_top_k <= 0:
        raise ValueError("top_k must be positive")
    selected_final_json_retries = invalid_final_json_retries(config)
    selected_system_prompt = system_prompt(interaction_mode)
    node_items = uml_index.get("nodes") or uml_index.get("segments") or []
    nodes = {item["diagram_id"]: item for item in node_items}
    entry_id = str(uml_index.get("entry_diagram_id") or "")
    if entry_id not in nodes:
        raise ValueError("UML graph entry_diagram_id is missing")
    entry_node = nodes[entry_id]

    def tool_result(node: Dict[str, Any]) -> Dict[str, Any]:
        if interaction_mode == IMAGE_ONLY_MODE:
            return {"ok": True, "diagram_id": node["diagram_id"]}
        return {
            "ok": True,
            "diagram_id": node["diagram_id"],
            "entry_signature": node.get("entry_signature"),
            "origin_test_line": int(node.get("origin_test_line") or 0),
            "visible_calls": int(node.get("visible_call_count") or 0),
            "visible_units": int(node.get("visible_unit_count") or 0),
            "participants": int(node.get("participant_count") or 0),
            "method_signatures": node["method_signatures"],
            "folds": list(node.get("folds") or []),
            "links": list(node.get("links") or []),
            "sibling_group": node.get("sibling_group"),
        }

    def image_text(node: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "text",
            "text": (
                f"[Sequence subgraph `{node['diagram_id']}`] | Entry: "
                f"`{node.get('entry_signature')}` | Visible Units: "
                f"{result['visible_units']} | Participants: {result['participants']}\n"
                "All visible method signatures in this subgraph:\n- "
                + "\n- ".join(node["method_signatures"])
                + "\nDirect links:\n"
                + json.dumps(result["links"], ensure_ascii=False)
                + "\nSibling view group:\n"
                + json.dumps(result["sibling_group"], ensure_ascii=False)
            ),
        }

    entry_result = tool_result(entry_node)
    entry_text = (
        None
        if interaction_mode == IMAGE_ONLY_MODE
        else image_text(entry_node, entry_result)
    )
    pending_input: List[Dict[str, Any]] = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            *(
                [] if entry_text is None
                else [{"type": "text", "text": entry_text["text"]}]
            ),
            {"type": "image_ref", "diagram_id": entry_id},
        ],
    }]
    if conversation_path is not None:
        write_text(conversation_path, "")
        write_text(conversation_path.with_name("response_usage.jsonl"), "")
        append_jsonl(conversation_path, {
            "role": "system", "content": selected_system_prompt,
        })
        for item in pending_input:
            append_jsonl(conversation_path, item)
    history: List[Dict[str, Any]] = []
    viewed, viewed_set = [entry_id], {entry_id}
    discovered = {
        str(link["diagram_id"])
        for link in entry_node.get("links") or []
    } | {entry_id}
    tool_rounds = 0
    diagram_view_count = 1
    final_json_retry_count = 0
    model = ""
    cache_material = json.dumps(
        {
            "prompt": prompt,
            "entry_diagram_id": entry_id,
            "interaction_mode": interaction_mode,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    prompt_cache_key_prefix = (
        "mllmfl-localize-"
        + hashlib.sha256(cache_material.encode("utf-8")).hexdigest()[:32]
    )

    def new_prompt_cache_key() -> str:
        return prompt_cache_key_prefix + "-" + secrets.token_hex(4)

    prompt_cache_key = new_prompt_cache_key()

    def final_response_error(content: str) -> str:
        parsed = parse_model_response(content)
        if parsed is None:
            return "response is not valid JSON"
        try:
            validate_model_ranking_payload(
                parsed,
                selected_top_k,
                interaction_mode,
            )
        except ValueError as error:
            return str(error)
        return ""

    def hydrate_image_refs(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        hydrated = copy.deepcopy(list(items))
        for item in hydrated:
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for index, part in enumerate(content):
                if not isinstance(part, dict) or part.get("type") != "image_ref":
                    continue
                diagram_id = str(part.get("diagram_id") or "")
                node = nodes.get(diagram_id)
                if node is None:
                    raise ValueError(f"request input references unknown diagram: {diagram_id}")
                image_path = directory / Path(*Path(node["image"]).parts)
                content[index] = _image_part(image_path)
        return hydrated

    while True:
        request_input = [*history, *pending_input]
        route_retries = 0
        while True:
            try:
                message, model, response_id, response_output, usage = _post_response(
                    config,
                    hydrate_image_refs(request_input),
                    timeout,
                    previous_response_id=None,
                    prompt_cache_key=prompt_cache_key,
                )
                break
            except RuntimeError as error:
                route_error = any(
                    marker in str(error)
                    for marker in (
                        "RateLimitReached",
                        "do_request_failed",
                        "invalid_encrypted_content",
                    )
                )
                if not route_error or route_retries >= 3:
                    raise
                route_retries += 1
                prompt_cache_key = new_prompt_cache_key()
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            raise ValueError("model tool_calls is not an array")
        if len(tool_calls) > 1:
            tool_calls = copy.deepcopy(tool_calls[:1])
            message = copy.deepcopy(message)
            message["tool_calls"] = copy.deepcopy(tool_calls)
            retained_call_id = str(tool_calls[0].get("id") or "")
            response_output = [
                item
                for item in response_output
                if item.get("type") != "function_call"
                or str(item.get("call_id") or "") == retained_call_id
            ]
        history.extend(copy.deepcopy(pending_input))
        history.extend(copy.deepcopy(response_output))
        if conversation_path is not None:
            append_jsonl(
                conversation_path.with_name("response_usage.jsonl"),
                {
                    "schema": "responses-usage",
                    "schema_version": 1,
                    "previous_response_id": None,
                    "response_id": response_id,
                    "usage": usage,
                },
            )
            logged_assistant = copy.deepcopy(message)
            logged_assistant.setdefault("role", "assistant")
            append_jsonl(conversation_path, logged_assistant)
        if tool_calls:
            tool_rounds += 1
            pending_input = []
            image_content: List[Dict[str, Any]] = []
            available_this_round = set(discovered)
            for index, tool_call in enumerate(tool_calls):
                call_id = str(tool_call.get("id") or f"missing-tool-call-id-{index}")
                function = tool_call.get("function") or {}
                name = str(function.get("name") or "")
                try:
                    arguments = json.loads(str(function.get("arguments") or "{}"))
                except json.JSONDecodeError:
                    arguments = None
                diagram_id = (
                    str(arguments.get("diagram_id") or "").strip()
                    if isinstance(arguments, dict)
                    else ""
                )
                node = nodes.get(diagram_id)
                if name != "view_sequence_diagram":
                    result = {"ok": False, "error": f"unsupported tool: {name}"}
                elif arguments is None or not diagram_id:
                    result = {"ok": False, "error": "diagram_id must be valid JSON text"}
                elif node is None:
                    result = {"ok": False, "error": f"unknown diagram_id: {diagram_id}"}
                elif diagram_id not in available_this_round:
                    result = {
                        "ok": False,
                        "error": f"diagram_id is not directly linked from a viewed subgraph: {diagram_id}",
                    }
                else:
                    result = tool_result(node)
                    diagram_view_count += 1
                    if diagram_id not in viewed_set:
                        viewed_set.add(diagram_id)
                        viewed.append(diagram_id)
                    discovered.update(
                        str(link["diagram_id"])
                        for link in node.get("links") or []
                    )
                    if interaction_mode == IMAGE_ONLY_MODE:
                        image_content.append({
                            "type": "image_ref", "diagram_id": diagram_id,
                        })
                    else:
                        rendered_image_text = image_text(node, result)
                        image_content.extend([
                            {"type": "text", "text": rendered_image_text["text"]},
                            {"type": "image_ref", "diagram_id": diagram_id},
                        ])
                tool_output = (
                    result if isinstance(result, str)
                    else json.dumps(result, ensure_ascii=False)
                )
                tool_item = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": tool_output,
                }
                pending_input.append(tool_item)
                if conversation_path is not None:
                    append_jsonl(conversation_path, tool_item)
            if image_content:
                image_item = {"role": "user", "content": image_content}
                pending_input.append(image_item)
                if conversation_path is not None:
                    append_jsonl(conversation_path, image_item)
            continue
        content = message.get("content")
        validation_error = (
            "response content is empty"
            if not isinstance(content, str) or not content.strip()
            else final_response_error(content)
        )
        if validation_error and final_json_retry_count < selected_final_json_retries:
            final_json_retry_count += 1
            correction = {
                "role": "user",
                "content": (
                    f"Your previous final response was invalid: {validation_error}. "
                    "Retry the final answer now. Return only one valid JSON object matching "
                    "the Output Contract, with every string correctly JSON-escaped. Do not "
                    "include prose or Markdown fences."
                ),
            }
            pending_input = [correction]
            if conversation_path is not None:
                append_jsonl(conversation_path, correction)
            continue
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model returned neither tool calls nor final text")
        return content, model, viewed, tool_rounds, diagram_view_count


from .stage import run

__all__ = [
    "DIAGRAM_TOOL",
    "IMAGE_ONLY_DIAGRAM_TOOL",
    "IMAGE_ONLY_SYSTEM_PROMPT",
    "SYSTEM_PROMPT",
    "build_prompt",
    "attach_source_locations",
    "defect_output_context",
    "gate_ranking",
    "gate_method_id_ranking",
    "parse_model_response",
    "run",
    "run_agent",
    "test_code_context",
    "validate_model_ranking_payload",
]
