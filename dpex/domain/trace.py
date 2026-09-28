import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .models import Call, Invocation

FULL_TRACE_SCHEMA = "fullchain-trace"
EXECUTION_SCHEMA = "fullchain-execution"
SCHEMA_VERSION = 6
AGENT_PROTOCOL_VERSION = 5
VALUE_CAPTURE_LEGACY_SCHEMA_VERSION = 4
LEGACY_SCHEMA_VERSION = 3
ASSERTION_EVENTS = {"ASSERT_START", "ASSERT_PASS", "ASSERT_FAIL"}
ALLOWED_EVENTS = {
    "ENTER", "RETURN", "THROW", "TEST_START", "TEST_FAILURE", "TEST_END",
    *ASSERTION_EVENTS,
}
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
    try:
        with path.open("r", encoding="utf-8", errors="strict") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"invalid event JSON at line {line_number}: {error}"
                    ) from error
                if not isinstance(event, dict):
                    raise ValueError(f"event at line {line_number} is not an object")
                events.append(event)
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot stream trace events {path}: {error}") from error
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
    assertions: List[Dict[str, Any]] = []
    unmatched_exits: List[Dict[str, Any]] = []
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
                arguments=(
                    dict(event["arguments"])
                    if isinstance(event.get("arguments"), dict) else None
                ),
            )
        elif event_type in {"RETURN", "THROW"}:
            invocation_id = int(event["invocation_id"])
            current = invocations.get(invocation_id)
            if current is None:
                unmatched_exits.append(dict(event))
                continue
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
                    "return_value": (
                        dict(event["return_value"])
                        if isinstance(event.get("return_value"), dict) else None
                    ),
                }
            )
        elif event_type == "TEST_START":
            test_start = dict(event)
        elif event_type == "TEST_FAILURE":
            failures.append(dict(event))
        elif event_type in ASSERTION_EVENTS:
            assertions.append(dict(event))
        elif event_type == "TEST_END":
            test_end = dict(event)

    ordered = sorted(invocations.values(), key=lambda item: item.enter_seq)
    unclosed = [item.invocation_id for item in ordered if item.exit_type is None]
    terminal_stack_overflow = (
        isinstance(test_end, dict)
        and test_end.get("successful") is False
        and any(
            failure.get("exception_class") == "java.lang.StackOverflowError"
            for failure in failures
        )
    )
    recovery = None
    if unmatched_exits or unclosed:
        if not terminal_stack_overflow:
            if unmatched_exits:
                raise ValueError(
                    "exit without ENTER: invocation "
                    f"{int(unmatched_exits[0]['invocation_id'])}"
                )
            raise ValueError(f"unclosed invocations: {unclosed[:20]}")
        failure = next(
            item for item in failures
            if item.get("exception_class") == "java.lang.StackOverflowError"
        )
        terminal_seq = int(failure.get("seq") or test_end.get("seq") or 0)
        terminal_ns = int(failure.get("ts_ns") or test_end.get("ts_ns") or 0)
        for invocation_id in unclosed:
            current = invocations[invocation_id]
            invocations[invocation_id] = Invocation(
                **{
                    **current.__dict__,
                    "exit_seq": terminal_seq,
                    "exit_ns": terminal_ns,
                    "exit_type": "THROW",
                    "duration_ns": max(0, terminal_ns - current.enter_ns),
                    "exception_class": "java.lang.StackOverflowError",
                    "message": str(failure.get("message") or ""),
                }
            )
        recovery = {
            "reason": "terminal_stack_overflow",
            "ignored_exit_without_enter_ids": [
                int(item["invocation_id"]) for item in unmatched_exits
            ],
            "synthesized_throw_invocation_ids": unclosed,
        }
        ordered = sorted(invocations.values(), key=lambda item: item.enter_seq)
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
                parent_chain=(
                    _ancestor_chain(by_id, parent.invocation_id)
                    if int((test_start or {}).get("agent_protocol_version") or 3) < 5
                    else None
                ),
                thread_id=child.thread_id,
                enter_seq=child.enter_seq,
                exit_seq=int(child.exit_seq or 0),
                exit_type=str(child.exit_type),
                origin_test_line=child.origin_test_line,
            )
        )
    protocol_version = int((test_start or {}).get("agent_protocol_version") or 3)
    if protocol_version not in {
        LEGACY_SCHEMA_VERSION, VALUE_CAPTURE_LEGACY_SCHEMA_VERSION, AGENT_PROTOCOL_VERSION,
    }:
        raise ValueError(f"unsupported agent protocol version: {protocol_version}")
    schema_version = (
        SCHEMA_VERSION if protocol_version == AGENT_PROTOCOL_VERSION
        else protocol_version
    )
    result = {
        "schema": FULL_TRACE_SCHEMA,
        "schema_version": schema_version,
        "event_count": len(events),
        "invocation_count": len(ordered),
        "call_count": len(calls),
        "test_start": test_start,
        "test_end": test_end,
        "test_failures": failures,
        "assertions": assertions,
        "invocations": [item.to_dict() for item in ordered],
        "calls": [item.to_dict() for item in calls],
    }
    if recovery is not None:
        result["event_recovery"] = recovery
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
    schema_version = value.get("schema_version")
    if schema_version not in {
        LEGACY_SCHEMA_VERSION, VALUE_CAPTURE_LEGACY_SCHEMA_VERSION, AGENT_PROTOCOL_VERSION, SCHEMA_VERSION,
    }:
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
    invocations = value["invocations"]
    seen_invocations = set()
    capture_values = False
    config = None
    if schema_version in {VALUE_CAPTURE_LEGACY_SCHEMA_VERSION, AGENT_PROTOCOL_VERSION, SCHEMA_VERSION}:
        test_start = value.get("test_start")
        if not isinstance(test_start, dict):
            raise ValueError("value-capturing fullchain trace requires TEST_START metadata")
        if test_start.get("agent_protocol_version") != (
            AGENT_PROTOCOL_VERSION if schema_version == SCHEMA_VERSION else schema_version
        ):
            raise ValueError("fullchain schema and agent protocol versions differ")
        config = _validate_value_capture_config(
            test_start.get("value_capture"), schema_version
        )
        capture_values = config["capture_values"]
    for index, invocation in enumerate(invocations):
        if not isinstance(invocation, dict):
            raise ValueError(f"invalid invocation at index {index}")
        invocation_id = invocation.get("invocation_id")
        if (
            not isinstance(invocation_id, int)
            or isinstance(invocation_id, bool)
            or invocation_id <= 0
            or invocation_id in seen_invocations
            or invocation.get("exit_type") not in {"RETURN", "THROW"}
        ):
            raise ValueError(f"invalid invocation at index {index}")
        seen_invocations.add(invocation_id)
        if schema_version in {VALUE_CAPTURE_LEGACY_SCHEMA_VERSION, AGENT_PROTOCOL_VERSION, SCHEMA_VERSION}:
            has_arguments = "arguments" in invocation
            has_return = "return_value" in invocation
            if capture_values != has_arguments:
                raise ValueError(
                    f"inconsistent arguments capture: invocation {invocation_id}"
                )
            if capture_values:
                expected_count = _descriptor_argument_count(
                    str(invocation.get("descriptor") or "")
                )
                _validate_arguments(invocation["arguments"], expected_count, config)
            if invocation["exit_type"] == "RETURN":
                if capture_values != has_return:
                    raise ValueError(
                        f"inconsistent return capture: invocation {invocation_id}"
                    )
                if capture_values:
                    returned = _validate_value_item(
                        invocation["return_value"], allow_index=False,
                        max_chars=config.get("value_max_chars"),
                    )
                    descriptor_void = str(invocation.get("descriptor") or "").endswith(
                        ")V"
                    )
                    if (returned["kind"] == "void") != descriptor_void:
                        raise ValueError(
                            f"inconsistent void return: invocation {invocation_id}"
                        )
            elif has_return:
                raise ValueError(
                    f"THROW invocation has return value: invocation {invocation_id}"
                )
    if schema_version == SCHEMA_VERSION:
        parents = {}
        for item in invocations:
            parent = item.get("parent_id")
            if not isinstance(parent, int) or isinstance(parent, bool) or parent < 0:
                raise ValueError("invalid invocation parent pointer")
            if parent and parent not in seen_invocations:
                raise ValueError("missing invocation parent")
            parents[item["invocation_id"]] = parent
        visited = {0}
        for invocation_id in parents:
            path = set()
            current = invocation_id
            while current not in visited:
                if current in path:
                    raise ValueError("cyclic invocation parent pointers")
                path.add(current)
                current = parents[current]
            visited.update(path)
        call_ids = set()
        for call in value["calls"]:
            invocation_id = call["invocation_id"]
            if invocation_id not in parents or invocation_id in call_ids:
                raise ValueError("missing or duplicate call invocation")
            call_ids.add(invocation_id)
            if "parent_chain" in call:
                raise ValueError("schema v6 stores parent pointers, not parent_chain")
            if call["parent_invocation_id"] != parents[invocation_id]:
                raise ValueError("call parent pointer does not match invocation")
    assertions = value.get("assertions", [])
    if not isinstance(assertions, list):
        raise ValueError("trace assertions must be an array")
    previous_seq = 0
    for index, event in enumerate(assertions):
        if (
            not isinstance(event, dict)
            or event.get("type") not in ASSERTION_EVENTS
            or not isinstance(event.get("assertion_id"), str)
            or not event["assertion_id"]
            or not isinstance(event.get("seq"), int)
            or isinstance(event["seq"], bool)
            or event["seq"] <= previous_seq
            or not isinstance(event.get("source_start_line"), int)
            or isinstance(event["source_start_line"], bool)
            or event["source_start_line"] <= 0
            or not isinstance(event.get("source_end_line"), int)
            or isinstance(event["source_end_line"], bool)
            or event["source_end_line"] < event["source_start_line"]
        ):
            raise ValueError(f"invalid assertion event at index {index}")
        previous_seq = event["seq"]
    instrumentation = value.get("assertion_instrumentation")
    if instrumentation is not None and (
        not isinstance(instrumentation, dict)
        or instrumentation.get("schema") != "assertion-instrumentation"
        or instrumentation.get("schema_version") != 1
        or not isinstance(instrumentation.get("configured_ranges"), str)
    ):
        raise ValueError("invalid assertion instrumentation metadata")
    return value


VALUE_KINDS = {
    "null", "boolean", "number", "string", "char", "enum", "array",
    "collection", "map", "object", "cycle", "error", "void",
}


def _validate_value_capture_config(
    value: Any, schema_version: int = SCHEMA_VERSION
) -> Dict[str, Any]:
    if schema_version == VALUE_CAPTURE_LEGACY_SCHEMA_VERSION:
        required = {
            "capture_values", "value_max_chars", "value_max_items",
            "value_max_depth", "value_max_arguments_chars",
        }
        positive_fields = (
            "value_max_chars", "value_max_items", "value_max_arguments_chars",
        )
    else:
        required = {
            "capture_values", "value_string_edge_chars",
            "value_container_edge_items", "value_nested_container_edge_items",
            "value_max_depth", "value_max_arguments",
        }
        positive_fields = (
            "value_string_edge_chars", "value_container_edge_items",
            "value_nested_container_edge_items", "value_max_arguments",
        )
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid value capture configuration")
    if not isinstance(value["capture_values"], bool):
        raise ValueError("invalid value capture enabled flag")
    for field in positive_fields:
        if (
            not isinstance(value[field], int)
            or isinstance(value[field], bool)
            or value[field] <= 0
        ):
            raise ValueError(f"invalid value capture setting: {field}")
    if (
        not isinstance(value["value_max_depth"], int)
        or isinstance(value["value_max_depth"], bool)
        or value["value_max_depth"] < 0
    ):
        raise ValueError("invalid value capture setting: value_max_depth")
    return value


def _descriptor_argument_count(descriptor: str) -> int:
    if not descriptor.startswith("(") or ")" not in descriptor:
        raise ValueError(f"invalid JVM method descriptor: {descriptor!r}")
    index = 1
    count = 0
    try:
        while descriptor[index] != ")":
            while descriptor[index] == "[":
                index += 1
            if descriptor[index] == "L":
                index = descriptor.index(";", index) + 1
            elif descriptor[index] in "ZBCSIJFD":
                index += 1
            else:
                raise ValueError
            count += 1
    except (IndexError, ValueError) as error:
        raise ValueError(f"invalid JVM method descriptor: {descriptor!r}") from error
    return count


def _validate_value_item(
    value: Any, allow_index: bool, max_chars: int | None = None
) -> Dict[str, Any]:
    required = {"declared_type", "runtime_type", "kind", "text", "truncated"}
    allowed = required | ({"index"} if allow_index else set())
    if not isinstance(value, dict) or set(value) != allowed:
        raise ValueError("invalid captured value structure")
    if allow_index and (
        not isinstance(value["index"], int)
        or isinstance(value["index"], bool)
        or value["index"] < 0
    ):
        raise ValueError("invalid captured value index")
    if (
        not isinstance(value["declared_type"], str)
        or not value["declared_type"]
        or not isinstance(value["runtime_type"], str)
        or value["kind"] not in VALUE_KINDS
        or not isinstance(value["text"], str)
        or not isinstance(value["truncated"], bool)
    ):
        raise ValueError("invalid captured value metadata")
    if max_chars is not None and len(value["text"]) > max_chars:
        raise ValueError("captured value exceeds configured character limit")
    if value["kind"] == "null" and (
        value["runtime_type"] or value["text"] != "null"
    ):
        raise ValueError("invalid captured null value")
    if value["kind"] == "void" and (
        value["runtime_type"] or value["text"] or value["truncated"]
    ):
        raise ValueError("invalid captured void value")
    return value


def _validate_arguments(
    value: Any, expected_count: int, config: Dict[str, Any]
) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "count", "items", "omitted_count", "truncated"
    }:
        raise ValueError("invalid captured arguments structure")
    count = value["count"]
    omitted = value["omitted_count"]
    items = value["items"]
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count != expected_count
        or not isinstance(items, list)
        or not isinstance(omitted, int)
        or isinstance(omitted, bool)
        or omitted < 0
        or omitted != count - len(items)
        or not isinstance(value["truncated"], bool)
    ):
        raise ValueError("invalid captured argument counts")
    legacy = "value_max_items" in config
    if len(items) > config[
        "value_max_items" if legacy else "value_max_arguments"
    ]:
        raise ValueError("captured arguments exceed configured item limit")
    for index, item in enumerate(items):
        _validate_value_item(
            item,
            allow_index=True,
            max_chars=config["value_max_chars"] if legacy else None,
        )
        if item["index"] != index:
            raise ValueError("captured argument indexes are not contiguous")
    expected_truncated = omitted > 0 or any(item["truncated"] for item in items)
    if value["truncated"] != expected_truncated:
        raise ValueError("inconsistent captured arguments truncation")
    if legacy:
        text_chars = sum(
            len(f"arg{item['index']}=") + len(item["text"]) for item in items
        ) + 2 * max(0, len(items) - 1)
        if text_chars > config["value_max_arguments_chars"]:
            raise ValueError("captured arguments exceed configured total limit")
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
    result = {
        "schema": EXECUTION_SCHEMA,
        "schema_version": full_trace["schema_version"],
        "test": {"class": test_class, "method": test_method},
        "original_call_count": len(full_trace["calls"]),
        "filtered_call_count": len(calls),
        "call_count": len(calls),
        "test_start": full_trace.get("test_start"),
        "test_end": full_trace.get("test_end"),
        "test_failures": full_trace.get("test_failures", []),
        "assertions": list(full_trace.get("assertions") or []),
        "assertion_instrumentation": full_trace.get("assertion_instrumentation"),
        "invocations": list(full_trace["invocations"]),
        "calls": calls,
    }
    validate_trace(result, EXECUTION_SCHEMA)
    return result
