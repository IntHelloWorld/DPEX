import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .models import Call, Invocation

FULL_TRACE_SCHEMA = "fullchain-trace"
WINDOW_SCHEMA = "fullchain-window"
SCHEMA_VERSION = 2
ALLOWED_EVENTS = {"ENTER", "RETURN", "THROW", "TEST_START", "TEST_FAILURE", "TEST_END"}
NOISE_PREFIXES = (
    "org.junit.",
    "junit.",
    "java.lang.reflect.",
    "sun.reflect.",
    "jdk.internal.reflect.",
    "fltrace.",
)


def load_events(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid event JSON at line {line_number}: {error}") from error
        if not isinstance(event, dict):
            raise ValueError(f"event at line {line_number} is not an object")
        events.append(event)
    events.sort(key=lambda item: int(item.get("seq") or 0))
    return events


def _ancestor_chain(invocations: Dict[int, Invocation], parent_id: int) -> List[int]:
    chain: List[int] = []
    seen = set()
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        chain.append(parent_id)
        parent = invocations.get(parent_id)
        if parent is None:
            break
        parent_id = parent.parent_id
    chain.reverse()
    return chain


def build_trace(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    unknown = sorted(
        {
            str(event.get("type"))
            for event in events
            if event.get("type") not in ALLOWED_EVENTS
        }
    )
    if unknown:
        raise ValueError(f"unsupported event types: {unknown}")
    if not any(event.get("type") == "ENTER" for event in events):
        raise ValueError("fullchain v2 requires at least one ENTER event")

    invocations: Dict[int, Invocation] = {}
    test_start = None
    test_end = None
    failures: List[Dict[str, Any]] = []
    for event in events:
        event_type = event.get("type")
        if event_type == "ENTER":
            invocation_id = int(event["invocation_id"])
            if invocation_id in invocations:
                raise ValueError(f"duplicate invocation id: {invocation_id}")
            invocations[invocation_id] = Invocation(
                invocation_id=invocation_id,
                parent_id=int(event.get("parent_id") or 0),
                class_name=str(event.get("class") or ""),
                method=str(event.get("method") or ""),
                descriptor=str(event.get("descriptor") or ""),
                thread_id=int(event.get("thread_id") or 0),
                thread_name=str(event.get("thread_name") or ""),
                enter_seq=int(event.get("seq") or 0),
                enter_ns=int(event.get("ts_ns") or 0),
            )
        elif event_type in {"RETURN", "THROW"}:
            invocation_id = int(event["invocation_id"])
            current = invocations.get(invocation_id)
            if current is None:
                raise ValueError(f"exit without ENTER: invocation {invocation_id}")
            if current.exit_type is not None:
                raise ValueError(f"duplicate exit: invocation {invocation_id}")
            invocations[invocation_id] = Invocation(
                **{
                    **current.__dict__,
                    "exit_seq": int(event.get("seq") or 0),
                    "exit_ns": int(event.get("ts_ns") or 0),
                    "exit_type": str(event_type),
                    "duration_ns": int(event.get("duration_ns") or 0),
                    "exception_class": str(event.get("exception_class") or ""),
                    "message": str(event.get("message") or ""),
                }
            )
        elif event_type == "TEST_START":
            test_start = dict(event)
        elif event_type == "TEST_FAILURE":
            failures.append(dict(event))
        elif event_type == "TEST_END":
            test_end = dict(event)

    ordered = sorted(invocations.values(), key=lambda item: item.enter_seq)
    unclosed = [item.invocation_id for item in ordered if item.exit_type is None]
    if unclosed:
        raise ValueError(f"unclosed invocations: {unclosed[:20]}")
    by_id = {item.invocation_id: item for item in ordered}
    calls: List[Call] = []
    for child in ordered:
        parent = by_id.get(child.parent_id)
        if parent is None:
            continue
        calls.append(
            Call(
                caller=parent.function,
                callee=child.function,
                caller_class=parent.class_name,
                callee_class=child.class_name,
                caller_method=parent.method,
                callee_method=child.method,
                caller_descriptor=parent.descriptor,
                callee_descriptor=child.descriptor,
                parent_invocation_id=parent.invocation_id,
                invocation_id=child.invocation_id,
                parent_chain=_ancestor_chain(by_id, parent.invocation_id),
                thread_id=child.thread_id,
                enter_seq=child.enter_seq,
                exit_seq=int(child.exit_seq or 0),
                exit_type=str(child.exit_type),
            )
        )
    result = {
        "schema": FULL_TRACE_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event_count": len(events),
        "invocation_count": len(ordered),
        "call_count": len(calls),
        "test_start": test_start,
        "test_end": test_end,
        "test_failures": failures,
        "invocations": [item.to_dict() for item in ordered],
        "calls": [item.to_dict() for item in calls],
    }
    validate_trace(result, FULL_TRACE_SCHEMA)
    return result


def validate_trace(value: Any, expected_schema: str | None = None) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("trace must be a JSON object")
    schema = value.get("schema")
    if schema not in {FULL_TRACE_SCHEMA, WINDOW_SCHEMA}:
        raise ValueError(f"unsupported trace schema: {schema!r}")
    if expected_schema and schema != expected_schema:
        raise ValueError(f"expected schema {expected_schema}, got {schema}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {value.get('schema_version')!r}")
    if not isinstance(value.get("calls"), list) or not isinstance(value.get("invocations"), list):
        raise ValueError("trace calls and invocations must be arrays")
    required = {"caller", "callee", "invocation_id", "parent_invocation_id", "exit_type"}
    for index, call in enumerate(value["calls"]):
        if not isinstance(call, dict) or not required.issubset(call):
            raise ValueError(f"invalid call at index {index}")
    return value


def extract_stack_methods(text: str) -> List[str]:
    return list(dict.fromkeys(re.findall(r"^\s*at\s+([A-Za-z0-9_.$<>]+)\(", text, flags=re.M)))


def _endpoint_matches(value: str, target: str) -> bool:
    a, b = value.replace("/", ".").split("."), target.replace("/", ".").split(".")
    return a == b or (len(a) >= 2 and len(b) >= 2 and a[-2:] == b[-2:])


def _context_calls(
    first: Dict[str, Any],
    invocations: Dict[int, Dict[str, Any]],
    selected: set[int],
) -> List[Dict[str, Any]]:
    """Recreate omitted ancestor edges so the window retains its call context."""
    result: List[Dict[str, Any]] = []
    chain = [int(value) for value in first.get("parent_chain") or []]
    for parent_id, child_id in zip(chain, chain[1:]):
        if child_id in selected:
            continue
        parent, child = invocations.get(parent_id), invocations.get(child_id)
        if not parent or not child:
            continue
        result.append(
            {
                "caller": f"{parent['class']}.{parent['method']}",
                "callee": f"{child['class']}.{child['method']}",
                "caller_class": parent["class"],
                "callee_class": child["class"],
                "caller_method": parent["method"],
                "callee_method": child["method"],
                "caller_descriptor": parent.get("descriptor", ""),
                "callee_descriptor": child.get("descriptor", ""),
                "parent_invocation_id": parent_id,
                "invocation_id": child_id,
                "parent_chain": [],
                "thread_id": child.get("thread_id", 0),
                "enter_seq": child.get("enter_seq", 0),
                "exit_seq": child.get("exit_seq", 0),
                "exit_type": child.get("exit_type", "RETURN"),
                "count": 1,
                "context": True,
            }
        )
    return result


def _compress_siblings(calls: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse only adjacent, equivalent sibling calls while preserving order."""
    compact: List[Dict[str, Any]] = []
    for raw in calls:
        call = dict(raw)
        key = (
            call["caller"],
            call["callee"],
            call.get("parent_invocation_id"),
            call.get("caller_descriptor", ""),
            call.get("callee_descriptor", ""),
        )
        if compact and compact[-1]["_key"] == key:
            compact[-1]["count"] += 1
            compact[-1]["invocation_ids"].append(call["invocation_id"])
            continue
        call["_key"] = key
        call["count"] = 1
        call["invocation_ids"] = [call["invocation_id"]]
        compact.append(call)
    for call in compact:
        call.pop("_key", None)
    return compact


def project_fault_window(
    full_trace: Dict[str, Any],
    test_class: str,
    test_method: str,
    failure_text: str = "",
    before: int = 80,
    after: int = 80,
    max_calls: int = 160,
) -> Dict[str, Any]:
    validate_trace(full_trace, FULL_TRACE_SCHEMA)
    calls = [
        call
        for call in full_trace["calls"]
        if not any(
            str(call.get(endpoint, "")).startswith(NOISE_PREFIXES)
            for endpoint in ("caller", "callee")
        )
    ]
    if not calls:
        raise ValueError("no calls remain after noise filtering")
    test_fqn = f"{test_class}.{test_method}"
    stack_methods = extract_stack_methods(failure_text)
    stack_indexes = [
        index
        for index, call in enumerate(calls)
        if any(
            _endpoint_matches(call[endpoint], method)
            for method in stack_methods
            for endpoint in ("caller", "callee")
        )
    ]
    test_indexes = [
        index
        for index, call in enumerate(calls)
        if any(
            _endpoint_matches(call[endpoint], test_fqn)
            for endpoint in ("caller", "callee")
        )
    ]
    # Prefer the failure stack, then the test method, and finally the trace tail.
    if stack_indexes:
        anchor, mode = stack_indexes[-1], "failure_stack"
    elif test_indexes:
        anchor, mode = test_indexes[-1], "test_method"
    else:
        anchor, mode = len(calls) - 1, "tail"
    start = max(0, anchor - max(0, before))
    end = min(len(calls), anchor + max(0, after) + 1)
    window = calls[start:end]
    if max_calls > 0 and len(window) > max_calls:
        window = window[-max_calls:]
        start = end - len(window)
    selected_ids = {int(call["invocation_id"]) for call in window}
    invocations = {
        int(item["invocation_id"]): item for item in full_trace["invocations"]
    }
    context = _context_calls(window[0], invocations, selected_ids) if window else []
    compact = _compress_siblings([*context, *window])
    relevant_ids = set()
    for call in compact:
        relevant_ids.add(int(call["invocation_id"]))
        relevant_ids.update(int(value) for value in call.get("invocation_ids") or [])
        relevant_ids.add(int(call["parent_invocation_id"]))
        relevant_ids.update(int(value) for value in call.get("parent_chain") or [])
    relevant = [
        item
        for item in full_trace["invocations"]
        if int(item["invocation_id"]) in relevant_ids
    ]
    result = {
        "schema": WINDOW_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "test": {"class": test_class, "method": test_method},
        "focus": {
            "mode": mode,
            "anchor_index": anchor,
            "window_start": start,
            "window_end": end,
            "stack_methods": stack_methods[:20],
        },
        "original_call_count": len(full_trace["calls"]),
        "filtered_call_count": len(calls),
        "call_count": len(compact),
        "test_start": full_trace.get("test_start"),
        "test_end": full_trace.get("test_end"),
        "test_failures": full_trace.get("test_failures", []),
        "invocations": relevant,
        "calls": compact,
    }
    validate_trace(result, WINDOW_SCHEMA)
    return result
