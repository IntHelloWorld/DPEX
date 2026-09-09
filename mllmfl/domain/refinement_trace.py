import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Iterable

from mllmfl.infrastructure.sequence_diagram import readable_signature

from .trace import EXECUTION_SCHEMA, validate_trace


REFINEMENT_TRACE_SCHEMA = "refinement-trace"
REFINEMENT_TRACE_VERSION = 2

# Compact call/context row fields. Keeping this protocol positional removes the
# repeated field-name cost without forcing consumers to materialize call objects.
(
    ROW_INVOCATION_ID,
    ROW_PARENT_ID,
    ROW_METHOD_ID,
    ROW_ENTER_SEQ,
    ROW_EXIT_SEQ,
    ROW_OUTCOME,
    ROW_ORIGIN_TEST_LINE,
    ROW_CHILDREN,
    ROW_SUBTREE_CALL_COUNT,
    ROW_ARGUMENTS,
    ROW_RESULT,
    ROW_CHILD_CALL_PREFIX,
) = range(12)
ROW_LENGTH = 12


def method_key(invocation: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(invocation["class"]),
        str(invocation["method"]),
        str(invocation.get("descriptor") or ""),
    )


def method_signature(key: tuple[str, str, str]) -> str:
    class_name, method, descriptor = key
    return f"{class_name}.{readable_signature(method, descriptor)}"


def build_method_catalog(
    executions: Iterable[Dict[str, Any]],
) -> tuple[list[Dict[str, str]], Dict[tuple[str, str, str], str], str]:
    recorded_keys = set()
    for execution in executions:
        call_ids = {int(call["invocation_id"]) for call in execution["calls"]}
        recorded_keys.update(
            method_key(invocation)
            for invocation in execution["invocations"]
            if int(invocation.get("invocation_id") or 0) in call_ids
        )
        del execution
    keys = sorted(recorded_keys)
    method_ids = {key: f"M{index}" for index, key in enumerate(keys, 1)}
    catalog = [
        {
            "method_id": method_ids[key],
            "function": f"{key[0]}.{key[1]}",
            "signature": method_signature(key),
            "descriptor": key[2],
        }
        for key in keys
    ]
    material = json.dumps(
        catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return catalog, method_ids, fingerprint


def _value(value: Any) -> list[Any] | None:
    if not isinstance(value, dict):
        return None
    omitted_count = int(value.get("omitted_count") or 0)
    if value.get("truncated") is True and omitted_count == 0:
        omitted_count = 1
    return [
        str(value.get("kind") or ""),
        str(value.get("runtime_type") or ""),
        str(value.get("text") or ""),
        omitted_count,
    ]


def _arguments(value: Any) -> list[Any] | None:
    if not isinstance(value, dict):
        return None
    return [
        int(value.get("omitted_count") or 0),
        [
            [
                str(item.get("kind") or ""),
                str(item.get("runtime_type") or ""),
                str(item.get("text") or ""),
                1 if item.get("truncated") is True else 0,
            ]
            for item in value.get("items") or []
        ],
    ]


def _folding_summary(value: Dict[str, Any]) -> Dict[str, int]:
    fields = (
        "original_call_count",
        "retained_call_count",
        "folded_call_count",
        "assertion_interval_count",
        "successful_assertion_count",
        "failed_assertion_count",
        "unmatched_assertion_event_count",
    )
    return {field: int(value.get(field) or 0) for field in fields}


def _fingerprint(value: Dict[str, Any]) -> str:
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    material = {key: item for key, item in value.items() if key != "fingerprint"}
    for chunk in encoder.iterencode(material):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def build_refinement_trace(
    execution: Dict[str, Any],
    *,
    project: str,
    test_id: str,
    test: str,
    method_ids: Dict[tuple[str, str, str], str],
    catalog_fingerprint: str,
    assertion_folding: Dict[str, Any],
    error_stack: str,
    test_output: str,
) -> Dict[str, Any]:
    """Build the sole, assertion-pruned runtime artifact used by refinement."""
    validate_trace(execution, EXECUTION_SCHEMA)
    invocations = {
        int(item["invocation_id"]): item for item in execution["invocations"]
    }
    ordered_calls = sorted(
        execution["calls"],
        key=lambda item: (
            int(item.get("enter_seq") or 0), int(item["invocation_id"]),
        ),
    )
    call_ids = {int(item["invocation_id"]) for item in ordered_calls}
    node_ids = set(call_ids)
    node_ids.update(int(item["parent_invocation_id"]) for item in ordered_calls)
    missing = sorted(node_ids - set(invocations))
    if missing:
        raise ValueError(f"execution is missing refinement context: {missing[:20]}")

    children: Dict[int, list[int]] = {}
    for call in ordered_calls:
        children.setdefault(int(call["parent_invocation_id"]), []).append(
            int(call["invocation_id"])
        )
    for values in children.values():
        values.sort(key=lambda invocation_id: (
            int(invocations[invocation_id].get("enter_seq") or 0), invocation_id,
        ))

    subtree_counts: Dict[int, int] = {}
    for call in reversed(ordered_calls):
        invocation_id = int(call["invocation_id"])
        subtree_counts[invocation_id] = 1 + sum(
            subtree_counts[child_id]
            for child_id in children.get(invocation_id, [])
        )
    for context_id in node_ids - call_ids:
        subtree_counts[context_id] = sum(
            subtree_counts[child_id] for child_id in children.get(context_id, [])
        )

    def row(invocation_id: int) -> list[Any]:
        invocation = invocations[invocation_id]
        key = method_key(invocation)
        method_id = method_ids.get(key)
        if method_id is None:
            # Boundary-only test/framework contexts are not part of the lookup
            # catalog, but still need a stable method table entry for rendering.
            method_id = ""
        child_ids = children.get(invocation_id, [])
        prefix = [0]
        for child_id in child_ids:
            prefix.append(prefix[-1] + subtree_counts[child_id])
        outcome = str(invocation.get("exit_type") or "")
        result = _value(invocation.get("return_value"))
        if outcome == "THROW":
            result = [
                "throw",
                str(invocation.get("exception_class") or ""),
                str(invocation.get("message") or ""),
                0,
            ]
        return [
            invocation_id,
            int(invocation.get("parent_id") or 0),
            method_id,
            int(invocation.get("enter_seq") or 0),
            int(invocation.get("exit_seq") or 0),
            outcome,
            int(invocation.get("origin_test_line") or 0),
            child_ids,
            subtree_counts[invocation_id],
            _arguments(invocation.get("arguments")),
            result,
            prefix,
        ]

    used_keys = sorted({method_key(invocations[item]) for item in node_ids})
    boundary_keys = [key for key in used_keys if key not in method_ids]
    boundary_ids = {
        key: f"B{index}" for index, key in enumerate(boundary_keys, 1)
    }
    # Fill boundary method IDs after assigning their compact local table IDs.
    raw_rows = [row(item) for item in sorted(node_ids)]
    for item in raw_rows:
        if not item[ROW_METHOD_ID]:
            item[ROW_METHOD_ID] = boundary_ids[method_key(invocations[item[0]])]

    methods = [
        [method_ids[key], *key]
        for key in used_keys if key in method_ids
    ] + [
        [boundary_ids[key], *key] for key in boundary_keys
    ]
    call_rows = [item for item in raw_rows if item[ROW_INVOCATION_ID] in call_ids]
    context_rows = [
        item for item in raw_rows if item[ROW_INVOCATION_ID] not in call_ids
    ]
    grouped: "OrderedDict[str, list[int]]" = OrderedDict()
    for item in sorted(call_rows, key=lambda value: (value[ROW_ENTER_SEQ], value[0])):
        grouped.setdefault(str(item[ROW_METHOD_ID]), []).append(int(item[0]))

    failures = [
        {
            "exception_class": str(item.get("exception_class") or ""),
            "message": str(item.get("message") or ""),
        }
        for item in execution.get("test_failures") or []
    ]
    capture = dict((execution.get("test_start") or {}).get("value_capture") or {
        "capture_values": False,
        "value_string_edge_chars": 10,
        "value_container_edge_items": 2,
        "value_nested_container_edge_items": 1,
        "value_max_depth": 2,
        "value_max_arguments": 8,
    })
    value: Dict[str, Any] = {
        "schema": REFINEMENT_TRACE_SCHEMA,
        "schema_version": REFINEMENT_TRACE_VERSION,
        "project": project,
        "test_id": test_id,
        "test": test,
        "method_catalog_fingerprint": catalog_fingerprint,
        "capture": capture,
        "failure": {
            "process_exit_code": int(execution.get("process_exit_code") or 0),
            "error_stack": error_stack,
            "test_output": test_output,
            "events": failures,
        },
        "assertion_folding": _folding_summary(assertion_folding),
        "methods": methods,
        "contexts": context_rows,
        "calls": call_rows,
        "method_invocations": [
            [method_id, invocation_ids]
            for method_id, invocation_ids in grouped.items()
        ],
        "call_count": len(call_rows),
        "root_ids": [
            int(item[ROW_INVOCATION_ID]) for item in context_rows
            if item[ROW_SUBTREE_CALL_COUNT] > 0
        ],
    }
    value["fingerprint"] = _fingerprint(value)
    return validate_refinement_trace(value)


def _validate_value(value: Any) -> None:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(isinstance(item, str) for item in value[:3])
        or (
            not isinstance(value[3], int)
            or isinstance(value[3], bool)
            or value[3] < 0
        )
    ):
        raise ValueError("invalid refinement trace value")


def validate_refinement_trace(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("refinement trace must be a JSON object")
    required = {
        "schema", "schema_version", "project", "test_id", "test",
        "method_catalog_fingerprint", "capture", "failure",
        "assertion_folding", "methods", "contexts", "calls",
        "method_invocations", "call_count", "root_ids", "fingerprint",
    }
    if (
        set(value) != required
        or value.get("schema") != REFINEMENT_TRACE_SCHEMA
        or value.get("schema_version") != REFINEMENT_TRACE_VERSION
    ):
        raise ValueError("unsupported refinement trace schema")
    if (
        not isinstance(value.get("project"), str)
        or not value["project"].strip()
        or not isinstance(value.get("test_id"), str)
        or re.fullmatch(r"T[1-9]\d*", value["test_id"]) is None
        or not isinstance(value.get("test"), str)
        or "::" not in value["test"]
        or not isinstance(value.get("method_catalog_fingerprint"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["method_catalog_fingerprint"])
        is None
        or not isinstance(value.get("fingerprint"), str)
        or value["fingerprint"] != _fingerprint(value)
    ):
        raise ValueError("invalid refinement trace identity")
    methods = value.get("methods")
    if not isinstance(methods, list) or not methods:
        raise ValueError("refinement trace methods must be a non-empty array")
    method_ids = set()
    for item in methods:
        if (
            not isinstance(item, list)
            or len(item) != 4
            or not isinstance(item[0], str)
            or re.fullmatch(r"[MB][1-9]\d*", item[0]) is None
            or item[0] in method_ids
            or not all(isinstance(part, str) and part for part in item[1:3])
            or not isinstance(item[3], str)
        ):
            raise ValueError("invalid refinement trace method")
        method_ids.add(item[0])
    capture = value.get("capture")
    capture_fields = {
        "capture_values", "value_string_edge_chars",
        "value_container_edge_items", "value_nested_container_edge_items",
        "value_max_depth", "value_max_arguments",
    }
    legacy_capture_fields = {
        "capture_values", "value_max_chars", "value_max_items",
        "value_max_depth", "value_max_arguments_chars",
    }
    actual_capture_fields = set(capture) if isinstance(capture, dict) else set()
    positive_capture_fields = (
        actual_capture_fields - {"capture_values", "value_max_depth"}
    )
    if (
        not isinstance(capture, dict)
        or actual_capture_fields not in (capture_fields, legacy_capture_fields)
        or not isinstance(capture["capture_values"], bool)
        or any(
            not isinstance(capture[field], int)
            or isinstance(capture[field], bool)
            or capture[field] <= 0
            for field in positive_capture_fields
        )
        or not isinstance(capture["value_max_depth"], int)
        or isinstance(capture["value_max_depth"], bool)
        or capture["value_max_depth"] < 0
    ):
        raise ValueError("invalid refinement trace capture configuration")
    rows = value.get("contexts"), value.get("calls")
    if not all(isinstance(items, list) for items in rows):
        raise ValueError("refinement trace rows must be arrays")
    seen_ids = set()
    call_ids = set()
    row_by_id: Dict[int, list[Any]] = {}
    for is_call, items in ((False, value["contexts"]), (True, value["calls"])):
        previous_id = 0
        for item in items:
            if (
                not isinstance(item, list)
                or len(item) != ROW_LENGTH
                or not isinstance(item[0], int)
                or isinstance(item[0], bool)
                or item[0] <= previous_id
                or item[0] in seen_ids
                or not isinstance(item[1], int)
                or isinstance(item[1], bool)
                or item[1] < 0
                or item[2] not in method_ids
                or not all(
                    isinstance(item[index], int) and not isinstance(item[index], bool)
                    and item[index] >= 0
                    for index in (3, 4, 6, 8)
                )
                or item[5] not in {"RETURN", "THROW"}
                or not isinstance(item[7], list)
                or not all(
                    isinstance(child, int) and not isinstance(child, bool) and child > 0
                    for child in item[7]
                )
                or item[7] != sorted(set(item[7]))
                or not isinstance(item[11], list)
                or len(item[11]) != len(item[7]) + 1
                or item[11][0] != 0
                or any(
                    not isinstance(count, int) or isinstance(count, bool) or count < 0
                    for count in item[11]
                )
            ):
                raise ValueError("invalid refinement trace row")
            arguments = item[9]
            if arguments is not None:
                if (
                    not isinstance(arguments, list)
                    or len(arguments) != 2
                    or not isinstance(arguments[0], int)
                    or isinstance(arguments[0], bool)
                    or arguments[0] < 0
                    or not isinstance(arguments[1], list)
                ):
                    raise ValueError("invalid refinement trace arguments")
                for argument in arguments[1]:
                    _validate_value(argument)
            if item[10] is not None:
                _validate_value(item[10])
            if not capture["capture_values"] and (
                item[ROW_ARGUMENTS] is not None or (
                    item[ROW_RESULT] is not None
                    and item[ROW_RESULT][0] != "throw"
                )
            ):
                raise ValueError("disabled refinement trace captured values")
            previous_id = item[0]
            seen_ids.add(item[0])
            row_by_id[item[0]] = item
            if is_call:
                call_ids.add(item[0])
    for invocation_id, item in row_by_id.items():
        if any(child not in call_ids for child in item[7]):
            raise ValueError("refinement trace child is not a call")
        if invocation_id in call_ids and item[ROW_PARENT_ID] not in row_by_id:
            raise ValueError("refinement trace call parent is absent")
        if any(
            row_by_id[child][ROW_PARENT_ID] != invocation_id
            for child in item[ROW_CHILDREN]
        ):
            raise ValueError("refinement trace child parent mismatch")
        if item[11][-1] != sum(row_by_id[child][8] for child in item[7]):
            raise ValueError("invalid refinement trace child prefix")
        expected_subtree = item[11][-1] + (1 if invocation_id in call_ids else 0)
        if item[8] != expected_subtree:
            raise ValueError("invalid refinement trace subtree count")
    if value.get("call_count") != len(value["calls"]):
        raise ValueError("inconsistent refinement trace call count")
    method_invocations = value.get("method_invocations")
    if not isinstance(method_invocations, list):
        raise ValueError("invalid refinement trace method index")
    indexed_ids = set()
    for item in method_invocations:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or item[0] not in method_ids
            or str(item[0]).startswith("B")
            or not isinstance(item[1], list)
            or item[1] != sorted(set(item[1]))
            or any(
                invocation_id not in call_ids
                or row_by_id[invocation_id][ROW_METHOD_ID] != item[0]
                or invocation_id in indexed_ids
                for invocation_id in item[1]
            )
        ):
            raise ValueError("invalid refinement trace method index")
        indexed_ids.update(item[1])
    if indexed_ids != call_ids:
        raise ValueError("incomplete refinement trace method index")
    expected_roots = [
        int(item[ROW_INVOCATION_ID]) for item in value["contexts"]
        if item[ROW_SUBTREE_CALL_COUNT] > 0
    ]
    failure = value.get("failure")
    folding = value.get("assertion_folding")
    if (
        value.get("root_ids") != expected_roots
        or not isinstance(failure, dict)
        or set(failure) != {
            "process_exit_code", "error_stack", "test_output", "events",
        }
        or not isinstance(failure["process_exit_code"], int)
        or isinstance(failure["process_exit_code"], bool)
        or not isinstance(failure["error_stack"], str)
        or not isinstance(failure["test_output"], str)
        or not isinstance(failure["events"], list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"exception_class", "message"}
            or not all(isinstance(part, str) for part in item.values())
            for item in failure["events"]
        )
        or not isinstance(folding, dict)
        or set(folding) != {
            "original_call_count", "retained_call_count", "folded_call_count",
            "assertion_interval_count", "successful_assertion_count",
            "failed_assertion_count", "unmatched_assertion_event_count",
        }
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in folding.values()
        )
        or folding["retained_call_count"] != value["call_count"]
        or folding["original_call_count"] != (
            folding["retained_call_count"] + folding["folded_call_count"]
        )
    ):
        raise ValueError("invalid refinement trace metadata")
    return value


@dataclass(frozen=True)
class RefinementTraceTopology:
    """One-open topology with scalar indexes and lazy call-object expansion."""

    trace: Dict[str, Any]
    methods: Dict[str, tuple[str, str, str]]
    rows: tuple[list[Any], ...]
    row_positions: Dict[int, int]
    call_positions: Dict[int, int]
    call_ids: tuple[int, ...]
    method_invocations: Dict[str, tuple[int, ...]]

    @classmethod
    def build(cls, trace: Dict[str, Any]) -> "RefinementTraceTopology":
        validate_refinement_trace(trace)
        methods = {
            str(item[0]): (str(item[1]), str(item[2]), str(item[3]))
            for item in trace["methods"]
        }
        rows = tuple([*trace["contexts"], *trace["calls"]])
        row_positions = {int(item[0]): index for index, item in enumerate(rows)}
        call_positions = {
            int(item[0]): row_positions[int(item[0])] for item in trace["calls"]
        }
        call_ids = tuple(int(item[0]) for item in trace["calls"])
        method_invocations = {
            str(item[0]): tuple(int(value) for value in item[1])
            for item in trace["method_invocations"]
        }
        return cls(
            trace=trace,
            methods=methods,
            rows=rows,
            row_positions=row_positions,
            call_positions=call_positions,
            call_ids=call_ids,
            method_invocations=method_invocations,
        )

    def row(self, invocation_id: int) -> list[Any]:
        position = self.row_positions.get(invocation_id)
        if position is None:
            raise ValueError(f"unknown refinement invocation: {invocation_id}")
        return self.rows[position]

    def has_call(self, invocation_id: int) -> bool:
        return invocation_id in self.call_positions

    def children(self, invocation_id: int) -> tuple[int, ...]:
        return tuple(int(item) for item in self.row(invocation_id)[ROW_CHILDREN])

    def subtree_call_count(self, invocation_id: int) -> int:
        return int(self.row(invocation_id)[ROW_SUBTREE_CALL_COUNT])

    def child_range_count(self, invocation_id: int, start: int, end: int) -> int:
        prefix = self.row(invocation_id)[ROW_CHILD_CALL_PREFIX]
        return int(prefix[end]) - int(prefix[start])

    def method_id(self, invocation_id: int) -> str:
        return str(self.row(invocation_id)[ROW_METHOD_ID])

    def invocation(self, invocation_id: int) -> Dict[str, Any]:
        item = self.row(invocation_id)
        class_name, method, descriptor = self.methods[str(item[ROW_METHOD_ID])]
        value: Dict[str, Any] = {
            "invocation_id": int(item[ROW_INVOCATION_ID]),
            "parent_id": int(item[ROW_PARENT_ID]),
            "class": class_name,
            "method": method,
            "descriptor": descriptor,
            "enter_seq": int(item[ROW_ENTER_SEQ]),
            "exit_seq": int(item[ROW_EXIT_SEQ]),
            "exit_type": str(item[ROW_OUTCOME]),
            "origin_test_line": int(item[ROW_ORIGIN_TEST_LINE]),
        }
        arguments = item[ROW_ARGUMENTS]
        if arguments is not None:
            value["arguments"] = {
                "count": len(arguments[1]) + int(arguments[0]),
                "omitted_count": int(arguments[0]),
                "items": [
                    {
                        "kind": part[0],
                        "runtime_type": part[1],
                        "text": part[2],
                        "omitted_count": part[3],
                    }
                    for part in arguments[1]
                ],
            }
        result = item[ROW_RESULT]
        if result is not None:
            if result[0] == "throw":
                value["exception_class"] = result[1]
                value["message"] = result[2]
            else:
                value["return_value"] = {
                    "kind": result[0], "runtime_type": result[1],
                    "text": result[2], "omitted_count": result[3],
                }
        return value

    def call(self, invocation_id: int) -> Dict[str, Any] | None:
        if not self.has_call(invocation_id):
            return None
        invocation = self.invocation(invocation_id)
        parent = self.invocation(int(invocation["parent_id"]))
        return {
            "caller": f"{parent['class']}.{parent['method']}",
            "callee": f"{invocation['class']}.{invocation['method']}",
            "caller_class": parent["class"],
            "callee_class": invocation["class"],
            "caller_method": parent["method"],
            "callee_method": invocation["method"],
            "caller_descriptor": parent["descriptor"],
            "callee_descriptor": invocation["descriptor"],
            "parent_invocation_id": int(invocation["parent_id"]),
            "invocation_id": invocation_id,
            "enter_seq": int(invocation["enter_seq"]),
            "exit_seq": int(invocation["exit_seq"]),
            "exit_type": str(invocation["exit_type"]),
            "origin_test_line": int(invocation["origin_test_line"]),
        }

    def node(self, invocation_id: int) -> Dict[str, Any]:
        invocation = self.invocation(invocation_id)
        call = self.call(invocation_id)
        participants = {str(invocation["class"])}
        if call is not None:
            participants.add(str(call["caller_class"]))
        return {
            "representative_invocation_id": invocation_id,
            "call": call,
            "invocation": invocation,
            "participant_classes": sorted(participants),
        }

    def caller_signature(self, invocation_id: int) -> str:
        parent_id = int(self.row(invocation_id)[ROW_PARENT_ID])
        parent_method = self.methods[self.method_id(parent_id)]
        return method_signature(parent_method)
