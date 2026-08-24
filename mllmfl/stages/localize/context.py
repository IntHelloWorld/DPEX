from pathlib import Path
from typing import Any, Dict, Sequence

from mllmfl.domain.failure import extract_error_stack
from mllmfl.domain.schemas import validate_defect_context
from mllmfl.domain.test_slice import validate_slice_metadata
from mllmfl.infrastructure.io import read_json
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
The initial user message lists every failing test and its entry sequence-diagram ID. Use
view_sequence_diagram(diagram_id) to open whichever failing-test entry is useful. An entry image is
returned together with that test's line-numbered code, error stack, and test output. Follow linked
Txxx-Dxxx IDs to inspect child subgraphs until you have enough evidence, then return one bug-level
localization result. You may explore the failing tests selectively, but base every ranked method on
a successfully viewed image.

Distinguish the caller that triggers or observes the failure from a callee whose implementation
contains the defect. A caller's proximity to the exception is evidence about the trigger path, not
by itself evidence that the caller is faulty. Follow arguments, state changes, calls, and
returns/throws to find the earliest callee behavior that violates the expected contract and explains
the downstream failure. Rank a caller only when its own logic is independently implicated.

## Diagram Guide
Each image is one runtime sequence subgraph read from top to bottom. Its title contains a Txxx-Dxxx
diagram ID. Named participants and their vertical lifelines represent the classes involved. Solid
arrows are method calls, dashed arrows are returns or throws, and activation bars show nested
execution. Each call label contains a Cxxx runtime call-occurrence ID, an Mxxx method ID reused for
the same method across all failing-test images for this bug, and the readable method signature. A
repetition marker denotes compressed repeated executions, while TO/FROM/VIEW Txxx-Dxxx notes link
adjacent subgraphs that can be opened with the tool.

## Tool-Use Preamble
Call view_sequence_diagram at most once in each assistant response. If multiple images may be useful,
choose one now and request the others in later turns. Immediately before the tool call, put a brief
progress update in the assistant content as one JSON object with exactly these keys:
{"evidence":"what the viewed evidence suggests","next_action":"why this image is next"}

## Output Contract
Once you have enough evidence and do not need another tool call, return only one JSON object, with
no Markdown fence, extra text, or extra keys:
{"ranked":[{"method_id":"M001","method_signature":"add(TickUnit)","reason":"..."}]}
The ranked array must contain one or more evidence-supported entries in descending suspiciousness
and must not exceed __TOP_K__ entries. For every entry, copy both the method_id and the readable
method name with parameter types verbatim from the same call label in a successfully viewed image.
The method_signature must omit the class name, as in add(TickUnit). Give each entry a brief,
method-specific reason."""


DIAGRAM_TOOL = {
    "type": "function",
    "name": "view_sequence_diagram",
    "description": (
        "Open an accessible sequence-diagram image. Entry IDs listed in the initial message are "
        "always accessible and also return their failing-test details. Child diagrams return only "
        "the image and must be directly linked from a diagram already viewed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "diagram_id": {
                "type": "string",
                "description": "Exact Txxx-Dxxx entry or linked diagram ID.",
            }
        },
        "required": ["diagram_id"],
        "additionalProperties": False,
    },
}


def build_system_prompt(top_k: int) -> str:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    return SYSTEM_PROMPT.replace("__TOP_K__", str(top_k))


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


def build_prompt(tests: Sequence[Dict[str, Any]]) -> str:
    if not tests:
        raise ValueError("failing tests must be non-empty")
    lines = ["[Failing Tests]"]
    for item in tests:
        lines.append(
            f"{item['test_id']} | {item['test']} | Entry: {item['entry_diagram_id']}"
        )
    return "\n".join(lines)


def entry_failure_context(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "diagram_id": str(item["entry_diagram_id"]),
        "test_id": str(item["test_id"]),
        "failing_test": str(item["test"]),
        "test_code": str(item["test_code"]),
        "error_stack": str(item["error_stack"]),
        "test_output": str(item["test_output"] or "(empty)"),
    }
