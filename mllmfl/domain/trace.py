import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .models import Call, Invocation

FULL_TRACE_SCHEMA = "fullchain-trace"
EXECUTION_SCHEMA = "fullchain-execution"
SCHEMA_VERSION = 3
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


def _without_class_initializers(
    events: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Drop every class initializer and its complete invocation subtree."""
    entered = {
        int(event["invocation_id"]): event
        for event in events
        if event.get("type") == "ENTER"
    }
    excluded = {
        invocation_id
        for invocation_id, event in entered.items()
        if event.get("method") == "<clinit>"
    }
    if not excluded:
        return [dict(event) for event in events]

    children: Dict[int, List[int]] = {}
    for invocation_id, event in entered.items():
        children.setdefault(int(event.get("parent_id") or 0), []).append(invocation_id)
    pending = list(excluded)
    while pending:
        for invocation_id in children.get(pending.pop(), []):
            if invocation_id not in excluded:
                excluded.add(invocation_id)
                pending.append(invocation_id)
    return [
        dict(event)
        for event in events
        if int(event.get("invocation_id") or 0) not in excluded
    ]


def build_trace(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    events = _without_class_initializers(events)
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
                origin_test_line=int(event.get("origin_test_line") or 0),
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
                origin_test_line=child.origin_test_line,
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
    if schema not in {FULL_TRACE_SCHEMA, EXECUTION_SCHEMA}:
        raise ValueError(f"unsupported trace schema: {schema!r}")
    if expected_schema and schema != expected_schema:
        raise ValueError(f"expected schema {expected_schema}, got {schema}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {value.get('schema_version')!r}")
    if not isinstance(value.get("calls"), list) or not isinstance(value.get("invocations"), list):
        raise ValueError("trace calls and invocations must be arrays")
    required = {
        "caller", "callee", "invocation_id", "parent_invocation_id", "exit_type",
        "origin_test_line",
    }
    for index, call in enumerate(value["calls"]):
        if not isinstance(call, dict) or not required.issubset(call):
            raise ValueError(f"invalid call at index {index}")
    return value


def project_execution(
    full_trace: Dict[str, Any],
    test_class: str,
    test_method: str,
) -> Dict[str, Any]:
    """Project every recorded application call without cropping or compression."""
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
    projected_calls = []
    for raw in calls:
        call = dict(raw)
        call.update({"count": 1, "context": False, "invocation_ids": [call["invocation_id"]]})
        projected_calls.append(call)
    result = {
        "schema": EXECUTION_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "test": {"class": test_class, "method": test_method},
        "original_call_count": len(full_trace["calls"]),
        "filtered_call_count": len(calls),
        "call_count": len(projected_calls),
        "test_start": full_trace.get("test_start"),
        "test_end": full_trace.get("test_end"),
        "test_failures": full_trace.get("test_failures", []),
        "invocations": list(full_trace["invocations"]),
        "calls": projected_calls,
    }
    validate_trace(result, EXECUTION_SCHEMA)
    return result
