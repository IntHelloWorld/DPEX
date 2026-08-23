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
from mllmfl.domain.interaction import IMAGE_ONLY_MODE, TEXT_INDEX_MODE
from mllmfl.domain.schemas import (
    validate_candidates,
    validate_defect_context,
    validate_localization,
    validate_uml_index,
)
from mllmfl.domain.test_slice import validate_slice_metadata
from mllmfl.infrastructure.io import append_jsonl, read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.java_source import extract_methods, find_java_file
from mllmfl.infrastructure.layout import RunLayout


DEFAULT_INVALID_FINAL_JSON_RETRIES = 2


def invalid_final_json_retries(config: Dict[str, Any]) -> int:
    cfg = config.get("mllm", config)
    value = int(
        cfg.get(
            "invalid_final_json_retries",
            DEFAULT_INVALID_FINAL_JSON_RETRIES,
        )
    )
    if value < 0:
        raise ValueError("invalid_final_json_retries must be non-negative")
    return value


SYSTEM_PROMPT = """You are a software defect-localization agent.

## Localization Approach
Use the error information, failing-test code, and runtime execution order shown in the linked
sequence subgraphs to reconstruct how the defect is triggered and identify methods that could have
caused it. Because the execution is split across connected subgraphs, repeatedly call
view_sequence_diagram(diagram_id) with directly linked IDs to inspect additional subgraphs
until you have enough evidence, then return the final localization result.

Distinguish the caller that triggers or observes the failure from a callee whose implementation
contains the defect. A caller's proximity to the exception is evidence about the trigger path, not
by itself evidence that the caller is faulty. Follow arguments, state changes, calls, and
returns/throws to find the earliest callee behavior that violates the expected contract and explains
the downstream failure. Rank a caller only when its own logic is independently implicated.

## Diagram Guide
Each image is one runtime sequence subgraph read from top to bottom. Named participants and their
vertical lifelines represent the classes involved. Solid arrows are method calls, dashed arrows are
returns or throws, activation bars show nested execution, call labels show execution order and
method signatures, repetition markers denote compressed repeated executions, and TO/FROM D-xxx
notes link adjacent subgraphs. The initial subgraph is supplied with the first request. Each viewed
subgraph returns its visible fully qualified method signatures and directly linked subgraph IDs.

## Tool-Use Preamble
Call view_sequence_diagram at most once in each assistant response. If multiple linked images may
be useful, choose one now and request the others in later turns. Immediately before the tool call,
put a brief progress update in the assistant content as one JSON object with exactly these keys:
{"evidence":"what the viewed evidence suggests","next_action":"why this image is next"}

## Output Contract
Once you have enough evidence and do not need another tool call, return only one JSON object, with
no Markdown fence, extra text, or extra keys:
{"ranked":[{"signature":"...","reason":"..."}]}
The ranked array must contain one or more evidence-supported entries in descending suspiciousness
and must not exceed the requested maximum. Copy every signature verbatim from method_signatures
supplied with the initial subgraph or a successful tool result. Give each entry a brief,
method-specific reason."""

IMAGE_ONLY_SYSTEM_PROMPT = """You are a software defect-localization agent.

## Localization Approach
Use the error information, failing-test code, and runtime execution order shown in the linked
sequence subgraph images to reconstruct how the defect is triggered and identify methods that could
have caused it. Because the execution is split across connected subgraphs, repeatedly call
view_sequence_diagram(diagram_id) with D-xxx IDs visible in viewed images to inspect additional
subgraphs until you have enough evidence, then return the final localization result.

Distinguish the caller that triggers or observes the failure from a callee whose implementation
contains the defect. A caller's proximity to the exception is evidence about the trigger path, not
by itself evidence that the caller is faulty. Follow arguments, state changes, calls, and
returns/throws to find the earliest callee behavior that violates the expected contract and explains
the downstream failure. Rank a caller only when its own logic is independently implicated.

## Diagram Guide
Each image is one runtime sequence subgraph read from top to bottom. Its title contains the D-xxx
diagram ID. Named participants and their vertical lifelines represent the classes involved. Solid
arrows are method calls, dashed arrows are returns or throws, and activation bars show nested
execution. Each call label contains a Cxxx runtime call-occurrence ID, an Mxxx method ID reused for
the same method across images, and the readable method signature. A repetition marker denotes
compressed repeated executions, while TO/FROM D-xxx notes link adjacent subgraphs that can be opened
with the tool.

## Tool-Use Preamble
Call view_sequence_diagram at most once in each assistant response. If multiple linked images may
be useful, choose one now and request the others in later turns. Immediately before the tool call,
put a brief progress update in the assistant content as one JSON object with exactly these keys:
{"evidence":"what the viewed evidence suggests","next_action":"why this image is next"}

## Output Contract
Once you have enough evidence and do not need another tool call, return only one JSON object, with
no Markdown fence, extra text, or extra keys:
{"ranked":[{"method_id":"M001","method_signature":"add(TickUnit)","reason":"..."}]}
The ranked array must contain one or more evidence-supported entries in descending suspiciousness
and must not exceed the requested maximum. For every entry, copy both the method_id and the readable
method name with parameter types verbatim from the same call label in a successfully viewed image.
The method_signature must omit the class name, as in add(TickUnit). Give each entry a brief,
method-specific reason."""

DIAGRAM_TOOL = {
    "type": "function",
    "name": "view_sequence_diagram",
    "description": (
        "Open one directly linked sequence subgraph and return its image, visible fully-qualified "
        "method signatures, folds, and links to adjacent subgraphs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "diagram_id": {
                "type": "string",
                "description": "Exact directly linked diagram ID returned by a viewed subgraph.",
            }
        },
        "required": ["diagram_id"],
        "additionalProperties": False,
    },
}

IMAGE_ONLY_DIAGRAM_TOOL = {
    "type": "function",
    "name": "view_sequence_diagram",
    "description": (
        "Open one directly linked sequence subgraph image. The target D-xxx ID must be visible "
        "in a subgraph image that has already been viewed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "diagram_id": {
                "type": "string",
                "description": "Exact linked D-xxx diagram ID visible in a viewed image.",
            }
        },
        "required": ["diagram_id"],
        "additionalProperties": False,
    },
}


def system_prompt(interaction_mode: str) -> str:
    return (
        IMAGE_ONLY_SYSTEM_PROMPT
        if interaction_mode == IMAGE_ONLY_MODE else SYSTEM_PROMPT
    )


def diagram_tool(interaction_mode: str) -> Dict[str, Any]:
    return (
        IMAGE_ONLY_DIAGRAM_TOOL
        if interaction_mode == IMAGE_ONLY_MODE else DIAGRAM_TOOL
    )


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
    if metadata.get("applied") and statements:
        blocks = []
        for statement in statements:
            start = int(statement["start_line"])
            blocks.append(_numbered_code(str(statement.get("code") or ""), start))
        return "\n".join(blocks)

    if "::" not in test:
        raise ValueError("failing-test source method is unavailable")
    class_name, method = test.split("::", 1)
    source_path = find_java_file(layout.workspace_dir(project, bug), class_name)
    if source_path is None:
        raise ValueError("failing-test source method is unavailable")
    methods = extract_methods(
        source_path.read_text(encoding="utf-8", errors="ignore"),
        class_name,
        method,
    )
    if len(methods) != 1:
        raise ValueError("failing-test source method is unavailable")
    return _numbered_code(str(methods[0]["code"]), int(methods[0]["start_line"]))


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
    interaction_mode: str = TEXT_INDEX_MODE,
) -> str:
    rendered_test_output = test_output if test_output else "(empty)"
    complete_trace = str(uml_index.get("strategy") or "").startswith("synthetic-root")
    code_heading = (
        "Failing-Test Code With Original Line Numbers"
        if complete_trace else "Sliced Failing-Test Code With Original Line Numbers"
    )
    nodes = {item["diagram_id"]: item for item in uml_index.get("nodes") or []}
    entry_id = str(uml_index["entry_diagram_id"])
    entry = nodes[entry_id]
    if interaction_mode == IMAGE_ONLY_MODE:
        return f"""[Failing Test] {test}

[{code_heading}]
{test_code}

[Error Stack]
{error_stack}

[Test Output]
{rendered_test_output}

[Task Parameters]
Maximum ranked methods: {top_k}"""
    return f"""[Failing Test] {test}

[{code_heading}]
{test_code}

[Error Stack]
{error_stack}

[Test Output]
{rendered_test_output}

[Task Parameters]
Maximum ranked methods: {top_k}

[Initial Sequence Subgraph]
ID: `{entry_id}`
Entry: `{entry['entry_signature']}`
Visible Units: {int(entry['visible_unit_count'])}
Participants: {int(entry['participant_count'])}

The initial subgraph image, its visible method signatures, folds, and direct links are supplied
with this request."""
