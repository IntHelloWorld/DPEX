import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import requests

from mllmfl.domain.models import Ranking
from mllmfl.domain.failure import extract_error_stack
from mllmfl.domain.schemas import (
    validate_candidates,
    validate_defect_context,
    validate_localization,
    validate_uml_index,
)
from mllmfl.domain.test_slice import validate_slice_metadata
from mllmfl.infrastructure.io import append_jsonl, read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout


SYSTEM_PROMPT = """You are an expert software defect-localization agent. Use the failing
behavior, sliced failing-test code, and runtime sequence diagrams to rank
the most likely defective functions. Sequence diagrams are not attached initially. Inspect any
useful diagram by calling view_sequence_diagram with a diagram_id from the supplied index. You may
call the tool repeatedly, but request exactly one diagram per assistant turn. Inspect at least one
diagram before producing the ranking. Each successful
tool result includes that diagram's image and all method signatures visible in it. When ready, return JSON
only in the requested ranking format. Every signature in the ranking must be copied verbatim from a
successful view_sequence_diagram tool result. Do not shorten, rewrite, or omit parameter types. The
application will validate each signature against the viewed diagrams and its local eligible-function
set."""

DIAGRAM_TOOL = {
    "type": "function",
    "name": "view_sequence_diagram",
    "description": "Load one first-level runtime invocation subtree sequence diagram.",
    "parameters": {
        "type": "object",
        "properties": {
            "diagram_id": {
                "type": "string",
                "description": "Exact diagram_id from the supplied sequence diagram index.",
            }
        },
        "required": ["diagram_id"],
        "additionalProperties": False,
    },
}


def parse_model_response(text: str) -> Dict[str, Any] | None:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else ""
    if value.endswith("```"):
        value = value.rsplit("\n", 1)[0]
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group())
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def validate_model_ranking_payload(value: Dict[str, Any], top_k: int) -> List[Dict[str, str]]:
    if set(value) != {"ranked"} or not isinstance(value["ranked"], list):
        raise ValueError("model ranking JSON must contain only a ranked array")
    if len(value["ranked"]) != top_k:
        raise ValueError(f"model ranking must contain exactly {top_k} entries")
    result = []
    for index, item in enumerate(value["ranked"]):
        if not isinstance(item, dict) or set(item) != {"signature", "reason"}:
            raise ValueError(f"invalid model ranking entry at index {index}")
        signature = item["signature"]
        reason = item["reason"]
        if not isinstance(signature, str) or not signature.strip():
            raise ValueError(f"empty model ranking signature at index {index}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"empty model ranking reason at index {index}")
        result.append({"signature": signature.strip(), "reason": reason.strip()})
    return result


def gate_ranking(
    raw: Any,
    candidates: Sequence[str],
    viewed_signatures: Sequence[str],
    top_k: int,
) -> Tuple[List[Ranking], List[str]]:
    """Accept only exact signatures from viewed diagrams whose methods are candidates."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    candidate_set = set(candidates)
    lookup = {
        signature: signature.rsplit("(", 1)[0]
        for signature in viewed_signatures
        if signature.rsplit("(", 1)[0] in candidate_set
    }
    result, dropped, seen = [], [], set()
    if not isinstance(raw, list):
        return result, dropped
    for item in raw:
        if not isinstance(item, dict):
            continue
        supplied = str(item.get("signature") or "").strip()
        candidate = lookup.get(supplied)
        if candidate is None:
            dropped.append(supplied)
            continue
        if supplied in seen:
            continue
        seen.add(supplied)
        result.append(Ranking(
            function=candidate,
            signature=supplied,
            rank=len(result) + 1,
            reason=str(item.get("reason") or "")[:300],
        ))
        if len(result) >= top_k:
            break
    return result, dropped


def _numbered_code(code: str, start_line: int) -> str:
    return "\n".join(
        f"{line_number:>5} | {line}"
        for line_number, line in enumerate(code.splitlines(), start_line)
    )


def test_code_context(
    layout: RunLayout,
    project: str,
    bug: str,
    directory: Path,
    test: str,
) -> str:
    slice_path = directory / "test_slice.json"
    if not slice_path.is_file():
        raise FileNotFoundError(f"test slice not found: {slice_path}")
    metadata = validate_slice_metadata(read_json(slice_path))
    statements = metadata.get("selected_statements") or []
    if not metadata.get("applied") or not statements:
        raise ValueError("failing-test source slice is unavailable")
    blocks = []
    for statement in statements:
        start = int(statement["start_line"])
        blocks.append(_numbered_code(str(statement.get("code") or ""), start))
    return "\n".join(blocks)


def defect_output_context(
    layout: RunLayout,
    project: str,
    bug: str,
    trigger: str,
    directory: Path,
    test: str,
) -> tuple[str, str]:
    context_path = directory / "defect_context.json"
    if context_path.is_file():
        context = validate_defect_context(read_json(context_path))
        if context["test"] != test:
            raise ValueError("defect context test does not match trigger test")
        return str(context["error_stack"]), str(context["test_output"])

    stack_path = directory / "error_stack.txt"
    if stack_path.is_file():
        error_stack = stack_path.read_text(encoding="utf-8", errors="ignore").strip()
    else:
        trace_log = layout.stage_log_dir("trace", project, bug, trigger) / "trace.stdout.log"
        trace_output = (
            trace_log.read_text(encoding="utf-8", errors="ignore")
            if trace_log.is_file()
            else ""
        )
        error_stack = extract_error_stack(trace_output)
    if not error_stack:
        raise ValueError("error stack is unavailable")

    output_path = directory / "test_output.txt"
    if output_path.is_file():
        test_output = output_path.read_text(encoding="utf-8", errors="ignore").strip()
    else:
        collect_log = layout.stage_log_dir("collect", project, bug, trigger)
        parts = []
        for name in ("test.stdout.log", "test.stderr.log"):
            path = collect_log / name
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8", errors="ignore").rstrip())
        fallback = directory / "failure.txt"
        if not parts and fallback.is_file():
            parts.append(fallback.read_text(encoding="utf-8", errors="ignore").rstrip())
        test_output = "\n".join(part for part in parts if part).strip()
    return error_stack, test_output


def build_prompt(
    test: str,
    test_code: str,
    error_stack: str,
    test_output: str,
    uml_index: Dict[str, Any],
    top_k: int,
) -> str:
    rendered_test_output = test_output if test_output else "(empty)"
    diagrams = "\n".join(
        "- {diagram_id} | {function} {signature} | test line {line} | "
        "calls {calls} | image {image}".format(
            diagram_id=item["diagram_id"],
            function=item["function"],
            signature=item.get("signature", ""),
            line=item.get("origin_test_line", 0),
            calls=item.get("call_count", 0),
            image=item["image"],
        )
        for item in uml_index["segments"]
    )
    return f"""[Failing Test] {test}

[Sliced Failing-Test Code With Original Line Numbers]
{test_code}

[Error Stack]
{error_stack}

[Test Output]
{rendered_test_output}

[First-Level Sequence Diagram Index]
{diagrams}

Inspect any useful diagrams with view_sequence_diagram. Then rank the top {top_k} most likely
defective methods. Before answering, view enough diagrams to obtain the signatures needed for the
ranking. Return exactly one JSON object, with no Markdown fence or extra keys:
{{"ranked":[{{"signature":"fully.qualified.Class.method(Type, Type[])","reason":"brief evidence"}}]}}

Requirements:
- `ranked` is ordered from most to least suspicious and contains exactly {top_k} entries.
- `signature` is copied character-for-character from `method_signatures` in a successful tool result.
- `reason` is a short, method-specific explanation."""


def _response_message(data: Dict[str, Any]) -> Dict[str, Any]:
    content_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    output = data.get("output")
    if not isinstance(output, list):
        raise ValueError("model response output is not an array")
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "")
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": str(item.get("arguments") or "{}"),
                },
            })
        elif item.get("type") == "message":
            content = item.get("content") or []
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str):
                        content_parts.append(text)
    message: Dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(content_parts) if content_parts else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _post_response(
    config: Dict[str, Any],
    input_items: Sequence[Dict[str, Any]],
    timeout: int,
    previous_response_id: str | None = None,
) -> tuple[Dict[str, Any], str, str]:
    cfg = config.get("mllm", config)
    if cfg.get("api_key"):
        raise ValueError("inline api_key is forbidden; configure api_key_env")
    env_name = str(cfg.get("api_key_env") or "OPENAI_API_KEY")
    api_key = os.environ.get(env_name)
    if not api_key:
        raise ValueError(f"API key missing from {env_name}")
    base_url = str(cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model = str(cfg.get("vision_model") or cfg.get("model") or "")
    if not model:
        raise ValueError("vision_model is required")
    reasoning_effort = str(cfg.get("reasoning_effort") or "medium").lower()
    if reasoning_effort not in {"low", "medium", "high"}:
        raise ValueError("reasoning_effort must be low, medium, or high")
    payload = {
        "model": model,
        "instructions": SYSTEM_PROMPT,
        "reasoning": {"effort": reasoning_effort},
        "max_output_tokens": int(cfg.get("max_tokens", 4096)),
        "input": list(input_items),
        "tools": [DIAGRAM_TOOL],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
    }
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    retry = cfg.get("retry") or {}
    attempts = int(retry.get("max_retries", 3))
    backoff = float(retry.get("backoff_s", 2))
    if attempts <= 0:
        raise ValueError("max_retries must be positive")
    last_error = ""
    for attempt in range(attempts):
        try:
            response = requests.post(
                base_url + "/responses",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("model response is not an object")
            response_id = str(data.get("id") or "")
            if not response_id:
                raise ValueError("model response id is missing")
            return _response_message(data), model, response_id
        except (requests.RequestException, RuntimeError, KeyError, IndexError,
                TypeError, ValueError) as error:
            last_error = str(error)
            if attempt + 1 < attempts:
                time.sleep(min(backoff * (2**attempt), 15))
    raise RuntimeError(last_error or "model request failed")


def _image_part(path: Path) -> Dict[str, Any]:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{encoded}",
    }


def run_agent(
    config: Dict[str, Any],
    prompt: str,
    uml_index: Dict[str, Any],
    directory: Path,
    timeout: int,
    conversation_path: Path | None = None,
) -> tuple[str, str, List[str], int, int]:
    segments = {item["diagram_id"]: item for item in uml_index["segments"]}
    logged_messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    if conversation_path is not None:
        write_text(conversation_path, "")
        for initial_message in logged_messages:
            append_jsonl(conversation_path, initial_message)
    pending_input: List[Dict[str, Any]] = [{
        "role": "user",
        "content": [{"type": "input_text", "text": prompt}],
    }]
    previous_response_id: str | None = None
    viewed, viewed_set = [], set()
    tool_rounds = 0
    diagram_view_count = 0
    model = ""
    while True:
        message, model, previous_response_id = _post_response(
            config, pending_input, timeout, previous_response_id
        )
        if conversation_path is not None:
            logged_assistant = dict(message)
            logged_assistant.setdefault("role", "assistant")
            append_jsonl(conversation_path, logged_assistant)
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            if not isinstance(tool_calls, list):
                raise ValueError("model tool_calls is not an array")
            tool_rounds += 1
            pending_input = []
            image_content: List[Dict[str, Any]] = []
            logged_image_content: List[Dict[str, Any]] = []
            loaded_this_round = False
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
                segment = segments.get(diagram_id)
                if name != "view_sequence_diagram":
                    result = {"ok": False, "error": f"unsupported tool: {name}"}
                elif arguments is None or not diagram_id:
                    result = {"ok": False, "error": "diagram_id must be valid JSON text"}
                elif segment is None:
                    result = {"ok": False, "error": f"unknown diagram_id: {diagram_id}"}
                elif loaded_this_round:
                    result = {
                        "ok": False,
                        "error": (
                            "only one diagram can be viewed per turn; request this diagram "
                            "again in the next turn"
                        ),
                    }
                else:
                    image_path = directory / Path(*Path(segment["image"]).parts)
                    result = {
                        "ok": True,
                        "diagram_id": diagram_id,
                        "function": segment["function"],
                        "invocation_id": segment["invocation_id"],
                        "method_signatures": segment["method_signatures"],
                    }
                    loaded_this_round = True
                    diagram_view_count += 1
                    if diagram_id not in viewed_set:
                        viewed_set.add(diagram_id)
                        viewed.append(diagram_id)
                    image_text = {
                        "type": "text", "text": (
                            f"Sequence diagram {diagram_id} for {segment['function']} "
                            f"(invocation {segment['invocation_id']}). All method signatures "
                            "in this diagram:\n- "
                            + "\n- ".join(segment["method_signatures"])
                        ),
                    }
                    image_content.extend([image_text, _image_part(image_path)])
                    logged_image_content.extend([
                        image_text,
                        {"type": "image_ref", "diagram_id": diagram_id},
                    ])
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
                pending_input.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": tool_message["content"],
                })
                if conversation_path is not None:
                    append_jsonl(conversation_path, tool_message)
            if image_content:
                response_image_content = []
                for part in image_content:
                    if part["type"] == "text":
                        response_image_content.append({
                            "type": "input_text", "text": part["text"]
                        })
                    else:
                        response_image_content.append(part)
                pending_input.append({"role": "user", "content": response_image_content})
                if conversation_path is not None:
                    append_jsonl(conversation_path, {
                        "role": "user",
                        "content": logged_image_content,
                    })
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model returned neither tool calls nor final text")
        return content, model, viewed, tool_rounds, diagram_view_count


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
    config_path: Path,
    timeout: int,
    top_k: int | None,
    dry_run: bool,
    force: bool = False,
) -> List[Dict[str, object]]:
    config = read_json(config_path)
    cfg = config.get("mllm", config)
    selected_top_k = top_k if top_k is not None else int(cfg.get("top_k", 5))
    if selected_top_k <= 0:
        raise ValueError("top_k must be positive")
    rows = []
    for project, bug, number, directory in layout.discover_triggers(projects, bugs, trigger):
        result_path = directory / "localization.json"
        if result_path.exists() and not force:
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "SKIPPED", "top1": ""})
            continue
        try:
            data = read_json(directory / "candidates.json")
            validate_candidates(data)
            uml_index = read_json(directory / "uml.json")
            validate_uml_index(uml_index, directory)
            candidates = data.get("candidates") or []
            test = (directory / "trigger_test.txt").read_text(encoding="utf-8").strip()
            test_code = test_code_context(layout, project, bug, directory, test)
            error_stack, test_output = defect_output_context(
                layout, project, bug, number, directory, test
            )
            prompt = build_prompt(
                test, test_code, error_stack, test_output, uml_index, selected_top_k
            )
            write_text(directory / "prompt.txt", SYSTEM_PROMPT + "\n\n" + prompt + "\n")
            conversation_path = directory / "conversation.jsonl"
            conversation_path.unlink(missing_ok=True)
            if dry_run:
                status, ranking, dropped, model = "DRY_RUN", [], [], ""
                viewed, tool_rounds, diagram_view_count = [], 0, 0
            else:
                raw, model, viewed, tool_rounds, diagram_view_count = run_agent(
                    config, prompt, uml_index, directory, timeout, conversation_path
                )
                parsed = parse_model_response(raw)
                if parsed is None:
                    raise ValueError("model returned invalid final ranking JSON")
                model_ranking = validate_model_ranking_payload(parsed, selected_top_k)
                viewed_set = set(viewed)
                viewed_signatures = [
                    signature
                    for segment in uml_index["segments"]
                    if segment["diagram_id"] in viewed_set
                    for signature in segment["method_signatures"]
                ]
                ranking, dropped = gate_ranking(
                    model_ranking,
                    [candidate["function"] for candidate in candidates],
                    viewed_signatures,
                    selected_top_k,
                )
                if dropped or len(ranking) != selected_top_k:
                    raise ValueError(
                        "model ranking contains signatures not returned by viewed diagrams "
                        "or not present in the local candidate set"
                    )
                status = "OK" if ranking else "EMPTY_RANKING"
            output = {
                "schema": "fault-localization",
                "schema_version": 3,
                "project": project,
                "bug": bug,
                "trigger": number,
                "status": status,
                "model": model,
                "candidate_count": len(candidates),
                "diagram_count": len(uml_index["segments"]),
                "tool_rounds": tool_rounds,
                "diagram_view_count": diagram_view_count,
                "viewed_diagrams": viewed,
                "ranking": [item.to_dict() for item in ranking],
                "dropped_invalid_signatures": dropped,
            }
            validate_localization(output)
            write_json(result_path, output)
            top1 = ranking[0].function if ranking else ""
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": status, "top1": top1})
        except Exception as error:
            write_text(layout.stage_log_dir("localize", project, bug, number) / "error.log",
                       str(error) + "\n")
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "ERROR", "top1": ""})
    write_csv(layout.logs / "localize.csv", rows,
              ["project", "bug", "trigger", "status", "top1"])
    return rows
