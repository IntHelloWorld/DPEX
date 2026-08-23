import hashlib
import re
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace
from mllmfl.domain.diagram_graph import (
    EXPAND_CALL,
    plan_diagram_graph,
)
from mllmfl.domain.schemas import validate_uml_index
from mllmfl.domain.test_slice import validate_slice_metadata
from mllmfl.infrastructure.io import read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.plantuml import render


def minimal_class_labels(classes: Iterable[str]) -> Dict[str, str]:
    values = list(dict.fromkeys(classes))
    split = {value: value.split(".") for value in values}
    result = {}
    for value in values:
        parts = split[value]
        label = parts[-1]
        for width in range(1, len(parts) + 1):
            candidate = ".".join(parts[-width:])
            if sum(".".join(other[-width:]) == candidate for other in split.values()) == 1:
                label = candidate
                break
        result[value] = label
    return result


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', "'").replace("\n", " ")


def _alias(class_name: str) -> str:
    return "p_" + hashlib.sha1(class_name.encode("utf-8")).hexdigest()[:10]


def _diagram_filename(diagram_id: str, extension: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", str(diagram_id).strip())
    safe = safe.strip("._-")
    if not safe:
        safe = "diagram"
    return f"{safe}.{extension}"


def readable_signature(method: str, descriptor: str) -> str:
    primitives = {"V": "void", "Z": "boolean", "B": "byte", "C": "char", "S": "short",
                  "I": "int", "J": "long", "F": "float", "D": "double"}
    if not descriptor.startswith("("):
        return method + "()"

    def parse(index: int) -> tuple[str, int]:
        arrays = 0
        while descriptor[index] == "[":
            arrays += 1
            index += 1
        if descriptor[index] == "L":
            end = descriptor.index(";", index)
            value = descriptor[index + 1 : end].replace("/", ".").rsplit(".", 1)[-1]
            index = end + 1
        else:
            value, index = primitives.get(descriptor[index], descriptor[index]), index + 1
        return value + "[]" * arrays, index

    args, index = [], 1
    try:
        while descriptor[index] != ")":
            value, index = parse(index)
            args.append(value)
    except (IndexError, ValueError):
        return method + "()"
    return f"{method}({', '.join(args)})"


def method_signatures(execution: Dict[str, Any]) -> List[str]:
    """List unique fully-qualified callee signatures in execution order."""
    result = []
    for call in sorted(
        execution["calls"],
        key=lambda item: (int(item.get("enter_seq") or 0), int(item["invocation_id"])),
    ):
        class_name = str(
            call.get("callee_class") or str(call["callee"]).rsplit(".", 1)[0]
        )
        method = str(
            call.get("callee_method") or str(call["callee"]).rsplit(".", 1)[-1]
        )
        signature = f"{class_name}.{readable_signature(method, str(call.get('callee_descriptor') or ''))}"
        if signature not in result:
            result.append(signature)
    return result


def _root_test_invocation(execution: Dict[str, Any]) -> Dict[str, Any] | None:
    test = execution.get("test") or {}
    matches = [
        invocation for invocation in execution.get("invocations") or []
        if invocation.get("class") == test.get("class")
        and invocation.get("method") == test.get("method")
    ]
    if not matches:
        return None
    return min(matches, key=lambda item: (int(item.get("parent_id") or 0) != 0,
                                           int(item.get("enter_seq") or 0)))


def _top_level_invocations(execution: Dict[str, Any]) -> List[Dict[str, Any]]:
    return sorted(
        [
            dict(invocation) for invocation in execution.get("invocations") or []
            if int(invocation.get("parent_id") or 0) == 0
        ],
        key=lambda item: (int(item.get("enter_seq") or 0), int(item["invocation_id"])),
    )


def _compress_repeated_subtrees(execution: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Merge adjacent identical sibling subtrees, keeping their first occurrence."""
    calls = [dict(call) for call in execution["calls"]]
    by_id = {int(call["invocation_id"]): call for call in calls}
    invocations = {
        int(invocation["invocation_id"]): invocation
        for invocation in execution.get("invocations") or []
    }
    children: Dict[int, List[int]] = {}
    for call in calls:
        children.setdefault(int(call["parent_invocation_id"]), []).append(
            int(call["invocation_id"])
        )

    @lru_cache(maxsize=None)
    def signature(invocation_id: int) -> Tuple[Any, ...]:
        call = by_id[invocation_id]
        invocation = invocations.get(invocation_id) or {}
        return (
            call.get("caller_class"),
            call.get("caller_method"),
            call.get("caller_descriptor"),
            call.get("callee_class"),
            call.get("callee_method"),
            call.get("callee_descriptor"),
            call.get("thread_id"),
            call.get("exit_type"),
            invocation.get("exception_class", ""),
            invocation.get("message", ""),
            tuple(signature(child_id) for child_id in children.get(invocation_id, [])),
        )

    result: List[Dict[str, Any]] = []

    def keep_children(parent_id: int) -> None:
        sibling_ids = children.get(parent_id, [])
        index = 0
        while index < len(sibling_ids):
            first_id = sibling_ids[index]
            first_signature = signature(first_id)
            end = index + 1
            while end < len(sibling_ids) and signature(sibling_ids[end]) == first_signature:
                end += 1
            kept = dict(by_id[first_id])
            kept["count"] = sum(
                int(by_id[invocation_id].get("count", 1))
                for invocation_id in sibling_ids[index:end]
            )
            result.append(kept)
            keep_children(first_id)
            index = end

    call_ids = set(by_id)
    root_parents = []
    for call in calls:
        parent_id = int(call["parent_invocation_id"])
        if parent_id not in call_ids and parent_id not in root_parents:
            root_parents.append(parent_id)
    for parent_id in root_parents:
        keep_children(parent_id)
    return result


def make_puml(
    execution: Dict[str, Any],
    project: str = "",
    bug: str = "",
    trigger: str = "",
    include_test_boundary: bool = True,
    include_all_root_boundaries: bool = False,
    title_suffix: str = "",
    boundary_invocations: Sequence[Dict[str, Any]] | None = None,
    compress_calls: bool = True,
    message_numbers: Dict[int, int] | None = None,
    references: Sequence[Dict[str, Any]] | None = None,
    graph_folds: Sequence[Dict[str, Any]] | None = None,
    message_prefix: str = "M",
    method_ids: Dict[int, str] | None = None,
) -> str:
    validate_trace(execution, EXECUTION_SCHEMA)
    if message_prefix not in {"M", "C"}:
        raise ValueError("message_prefix must be M or C")
    refs = [dict(item) for item in (references or [])]
    incoming_by_id: Dict[int, Dict[str, Any]] = {}
    for ref in refs:
        direction = str(ref.get("direction") or "")
        invocation_id = int(ref.get("invocation_id") or 0)
        diagram_id = str(ref.get("diagram_id") or "")
        if (
            direction not in {"FROM", "TO"}
            or invocation_id < 0
            or invocation_id <= 0
            or not diagram_id
        ):
            raise ValueError("invalid sequence-diagram reference")
        if direction == "FROM":
            if invocation_id in incoming_by_id:
                raise ValueError(f"duplicate incoming reference: {invocation_id}")
            incoming_by_id[invocation_id] = ref

    def is_next_sibling_reference(invocation_id: int) -> bool:
        incoming = incoming_by_id.get(invocation_id)
        return incoming is not None and incoming.get("relation") == "NEXT_SIBLING"

    calls = (
        _compress_repeated_subtrees(execution)
        if compress_calls else [dict(call) for call in execution["calls"]]
    )
    classes = []
    for call in calls:
        invocation_id = int(call["invocation_id"])
        if invocation_id not in incoming_by_id or is_next_sibling_reference(invocation_id):
            classes.append(
                call.get("caller_class") or call["caller"].rsplit(".", 1)[0]
            )
        classes.append(
            call.get("callee_class") or call["callee"].rsplit(".", 1)[0]
        )
    boundary_roots = (
        list(boundary_invocations)
        if boundary_invocations is not None
        else (
            _top_level_invocations(execution)
            if include_all_root_boundaries
            else ([_root_test_invocation(execution)] if include_test_boundary else [])
        )
    )
    boundary_roots = [root for root in boundary_roots if root is not None]
    classes.extend(str(root["class"]) for root in boundary_roots)
    classes.extend(
        str(root["caller_class"])
        for root in boundary_roots
        if root.get("caller_class")
        and (
            int(root["invocation_id"]) not in incoming_by_id
            or is_next_sibling_reference(int(root["invocation_id"]))
            or root.get("repeat_focus_call")
        )
    )
    classes.extend(
        str(ref["caller_class"])
        for ref in refs
        if ref["direction"] == "TO" and ref.get("caller_class")
    )
    labels = minimal_class_labels(classes)
    test_class = str((execution.get("test") or {}).get("class") or "")
    ordered = ([test_class] if test_class in labels else []) + [
        value for value in labels if value != test_class
    ]
    title = (
        f"Diagram ID: {title_suffix}"
        if title_suffix else f"{project} bug {bug} trigger {trigger}"
    )
    lines = [
        "@startuml",
        "hide footbox",
        "skinparam sequenceMessageAlign center",
        "skinparam NoteBackgroundColor #DCEFF8",
        "skinparam NoteBorderColor #5B7F95",
        f"title {_escape(title)}",
    ]
    for class_name in ordered:
        lines.append(f'participant "{_escape(labels[class_name])}" as {_alias(class_name)}')
    events = []
    for root_order, root in enumerate(boundary_roots):
        events.append((int(root.get("enter_seq") or 0), -1, root_order, "root_enter", root))
        events.append((int(root.get("exit_seq") or 0), 2, root_order, "root_exit", root))
    for order, call in enumerate(calls):
        events.append((int(call.get("enter_seq") or 0), 0, order, "enter", call))
        events.append(
            (
                int(call.get("exit_seq") or call.get("enter_seq") or 0),
                1,
                order,
                "exit",
                call,
            )
        )
    for order, ref in enumerate(item for item in refs if item["direction"] == "TO"):
        events.append((int(ref.get("enter_seq") or 0), 0, order, "reference_to", ref))
    for order, fold in enumerate(graph_folds or []):
        events.append((int(fold.get("enter_seq") or 0), 1, order, "graph_fold", fold))
    local_message_number = 0
    active_continuation_callers: List[str] = []

    def assigned_message_number(item: Dict[str, Any]) -> int:
        nonlocal local_message_number
        invocation_id = int(item["invocation_id"])
        if message_numbers is not None:
            if invocation_id not in message_numbers:
                raise ValueError(f"missing global message number for invocation {invocation_id}")
            return int(message_numbers[invocation_id])
        local_message_number += 1
        return local_message_number

    def call_label(item: Dict[str, Any], signature: str) -> str:
        number = assigned_message_number(item)
        invocation_id = int(item["invocation_id"])
        parts = [f"{message_prefix}{number:03d}"]
        if method_ids is not None:
            method_id = method_ids.get(invocation_id)
            if method_id is None:
                raise ValueError(
                    f"missing method ID for invocation {invocation_id}"
                )
            parts.append(method_id)
        parts.append(signature)
        return " ".join(parts)

    def render_next_sibling_from(incoming: Dict[str, Any], caller_class: str) -> None:
        if not caller_class:
            raise ValueError("NEXT_SIBLING reference is missing caller_class")
        lines.extend([
            f"rnote left of {_alias(caller_class)} #DCEFF8",
            f"FROM {_escape(incoming['diagram_id'])}",
            "endrnote",
            f"[--> {_alias(caller_class)}: return",
        ])
        if caller_class not in active_continuation_callers:
            lines.append(f"activate {_alias(caller_class)}")
            active_continuation_callers.append(caller_class)

    for _, _, _, event_type, call in sorted(events):
        if event_type == "graph_fold":
            anchor_class = str(call["anchor_class"])
            if call["kind"] == "SIBLING_BUNDLE":
                anchor_alias = _alias(anchor_class)
                lines.extend([
                    f"{anchor_alias} -> {anchor_alias}: SIBLING VIEWS",
                    "note right #DCEFF8",
                ])
                for peer_range in call["peer_ranges"]:
                    lines.append(
                        f"{_escape(peer_range['message_range'])}: "
                        f"{int(peer_range['represented_call_count'])} calls / "
                        f"VIEW {_escape(peer_range['diagram_id'])}"
                    )
                lines.append("end note")
            else:
                lines.extend([
                    f"rnote right of {_alias(anchor_class)} #DCEFF8",
                    f"TO {_escape(call['target_diagram_id'])}",
                    "endrnote",
                ])
            continue
        if event_type == "reference_to":
            label = f"{call['message_id']} {call['signature']}"
            repeat_count = int(call.get("repeat_count") or 1)
            if repeat_count > 1:
                label += f" ×{repeat_count}"
            caller_class = str(call.get("caller_class") or "")
            if caller_class:
                lines.append(f"{_alias(caller_class)} ->]: {_escape(label)}")
            else:
                lines.append(f"note over {_alias(ordered[0])}: {_escape(label)}")
            lines.extend([
                f"rnote right of {_alias(ordered[-1])} #DCEFF8",
                f"TO {_escape(call['diagram_id'])}",
                "endrnote",
            ])
            continue
        if event_type == "root_enter":
            class_name = str(call["class"])
            signature = readable_signature(str(call["method"]), str(call.get("descriptor") or ""))
            label = (
                "execution root"
                if call.get("synthetic")
                else (
                    f"TEST {signature}"
                    if call.get("layout_root")
                    else call_label(call, signature)
                )
            )
            repeat_count = int(call.get("repeat_count") or call.get("count") or 1)
            if repeat_count > 1:
                label += f" ×{repeat_count}"
            caller_class = str(call.get("caller_class") or "")
            incoming = incoming_by_id.get(int(call["invocation_id"]))
            if call.get("repeat_focus_call"):
                if incoming is not None:
                    lines.extend([
                        f"rnote left of {_alias(class_name)} #DCEFF8",
                        f"FROM {_escape(incoming['diagram_id'])}",
                        "endrnote",
                    ])
                if caller_class:
                    lines.append(
                        f"{_alias(caller_class)} -> {_alias(class_name)}: {_escape(label)}"
                    )
                else:
                    lines.append(f"[-> {_alias(class_name)}: {_escape(label)}")
            elif incoming is not None and incoming.get("relation") == "NEXT_SIBLING":
                render_next_sibling_from(incoming, caller_class)
                lines.append(
                    f"{_alias(caller_class)} -> {_alias(class_name)}: {_escape(label)}"
                )
            elif incoming is not None:
                lines.extend([
                    f"rnote left of {_alias(class_name)} #DCEFF8",
                    f"FROM {_escape(incoming['diagram_id'])}",
                    "endrnote",
                ])
                lines.append(f"[-> {_alias(class_name)}: {_escape(label)}")
            elif caller_class:
                lines.append(
                    f"{_alias(caller_class)} -> {_alias(class_name)}: {_escape(label)}"
                )
            else:
                lines.append(f"[-> {_alias(class_name)}: {_escape(label)}")
            lines.append(f"activate {_alias(class_name)}")
            continue
        if event_type == "root_exit":
            class_name = str(call["class"])
            result = "throws" if call.get("exit_type") == "THROW" else "return"
            caller_class = str(call.get("caller_class") or "")
            incoming = incoming_by_id.get(int(call["invocation_id"]))
            if call.get("repeat_focus_call"):
                if caller_class:
                    lines.append(
                        f"{_alias(class_name)} --> {_alias(caller_class)}: {result}"
                    )
                else:
                    lines.append(f"{_alias(class_name)} -->]: {result}")
            elif incoming is not None and incoming.get("relation") == "NEXT_SIBLING":
                lines.append(
                    f"{_alias(class_name)} --> {_alias(caller_class)}: {result}"
                )
            elif incoming is not None:
                lines.append(f"[<-- {_alias(class_name)}: {result}")
            elif caller_class:
                lines.append(
                    f"{_alias(class_name)} --> {_alias(caller_class)}: {result}"
                )
            else:
                lines.append(f"{_alias(class_name)} -->]: {result}")
            lines.append(f"deactivate {_alias(class_name)}")
            continue
        caller_class = call.get("caller_class") or call["caller"].rsplit(".", 1)[0]
        callee_class = call.get("callee_class") or call["callee"].rsplit(".", 1)[0]
        repeat_sequence = call.get("repeat_sequence") or {}
        if event_type == "enter":
            if int(repeat_sequence.get("position") or 0) == 1:
                lines.append(
                    "loop repeated sequence "
                    f"×{int(repeat_sequence['repeat_count'])}"
                )
            method = call.get("callee_method") or call["callee"].rsplit(".", 1)[-1]
            signature = readable_signature(method, str(call.get("callee_descriptor") or ""))
            label = call_label(call, signature)
            if int(call.get("count", 1)) > 1 and not repeat_sequence:
                label += f" ×{int(call['count'])}"
            incoming = incoming_by_id.get(int(call["invocation_id"]))
            if incoming is not None and incoming.get("relation") == "NEXT_SIBLING":
                render_next_sibling_from(incoming, str(caller_class))
                lines.append(
                    f"{_alias(caller_class)} -> {_alias(callee_class)}: {_escape(label)}"
                )
            elif incoming is not None:
                lines.extend([
                    f"rnote left of {_alias(callee_class)} #DCEFF8",
                    f"FROM {_escape(incoming['diagram_id'])}",
                    "endrnote",
                ])
                lines.append(f"[-> {_alias(callee_class)}: {_escape(label)}")
            else:
                lines.append(
                    f"{_alias(caller_class)} -> {_alias(callee_class)}: {_escape(label)}"
                )
            lines.append(f"activate {_alias(callee_class)}")
        else:
            result = "throws" if call.get("exit_type") == "THROW" else "return"
            incoming = incoming_by_id.get(int(call["invocation_id"]))
            if incoming is not None and incoming.get("relation") == "NEXT_SIBLING":
                lines.append(
                    f"{_alias(callee_class)} --> {_alias(caller_class)}: {result}"
                )
            elif incoming is not None:
                lines.append(f"[<-- {_alias(callee_class)}: {result}")
            else:
                lines.append(f"{_alias(callee_class)} --> {_alias(caller_class)}: {result}")
            lines.append(f"deactivate {_alias(callee_class)}")
            if (
                repeat_sequence
                and int(repeat_sequence.get("position") or 0)
                == int(repeat_sequence.get("pattern_length") or 0)
            ):
                lines.append("end")
    for caller_class in reversed(active_continuation_callers):
        lines.append(f"deactivate {_alias(caller_class)}")
    lines.extend(["@enduml", ""])
    return "\n".join(lines)
