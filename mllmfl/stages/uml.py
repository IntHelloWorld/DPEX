import hashlib
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace
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
    title_suffix: str = "",
) -> str:
    validate_trace(execution, EXECUTION_SCHEMA)
    calls = _compress_repeated_subtrees(execution)
    classes = []
    for call in calls:
        classes.extend([call.get("caller_class") or call["caller"].rsplit(".", 1)[0],
                        call.get("callee_class") or call["callee"].rsplit(".", 1)[0]])
    labels = minimal_class_labels(classes)
    test_class = str((execution.get("test") or {}).get("class") or "")
    ordered = ([test_class] if test_class in labels else []) + [
        value for value in labels if value != test_class
    ]
    title = f"{project} bug {bug} trigger {trigger}"
    if title_suffix:
        title += f" {title_suffix}"
    lines = ["@startuml", "hide footbox", "skinparam sequenceMessageAlign center",
             f"title {_escape(title)}"]
    for class_name in ordered:
        lines.append(f'participant "{_escape(labels[class_name])}" as {_alias(class_name)}')
    events = []
    root_test = _root_test_invocation(execution)
    if include_test_boundary and root_test is not None:
        events.append((int(root_test.get("enter_seq") or 0), -1, -1, "root_enter", root_test))
        events.append((int(root_test.get("exit_seq") or 0), 2, -1, "root_exit", root_test))
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
    message_number = 0
    for _, _, _, event_type, call in sorted(events):
        if event_type == "root_enter":
            message_number += 1
            class_name = str(call["class"])
            signature = readable_signature(str(call["method"]), str(call.get("descriptor") or ""))
            label = f"M{message_number:03d} {signature}"
            lines.append(f"[-> {_alias(class_name)}: {_escape(label)}")
            lines.append(f"activate {_alias(class_name)}")
            continue
        if event_type == "root_exit":
            class_name = str(call["class"])
            result = "throws" if call.get("exit_type") == "THROW" else "return"
            lines.append(f"{_alias(class_name)} -->]: {result}")
            lines.append(f"deactivate {_alias(class_name)}")
            continue
        caller_class = call.get("caller_class") or call["caller"].rsplit(".", 1)[0]
        callee_class = call.get("callee_class") or call["callee"].rsplit(".", 1)[0]
        if event_type == "enter":
            message_number += 1
            method = call.get("callee_method") or call["callee"].rsplit(".", 1)[-1]
            signature = readable_signature(method, str(call.get("callee_descriptor") or ""))
            label = f"M{message_number:03d} {signature}"
            if int(call.get("count", 1)) > 1:
                label += f" ×{int(call['count'])}"
            lines.append(f"{_alias(caller_class)} -> {_alias(callee_class)}: {_escape(label)}")
            lines.append(f"activate {_alias(callee_class)}")
        else:
            result = "throws" if call.get("exit_type") == "THROW" else "return"
            lines.append(f"{_alias(callee_class)} --> {_alias(caller_class)}: {result}")
            lines.append(f"deactivate {_alias(callee_class)}")
    lines.extend(["@enduml", ""])
    return "\n".join(lines)


def first_level_segments(
    execution: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Tuple[Dict[str, Any], Dict[str, Any]]], int]:
    """Partition the test-root call tree into direct runtime invocation subtrees."""
    validate_trace(execution, EXECUTION_SCHEMA)
    if execution.get("slice") is not None:
        validate_slice_metadata(execution["slice"])
    root = _root_test_invocation(execution)
    if root is None:
        raise ValueError("test root invocation was not found")
    root_id = int(root["invocation_id"])
    calls = [dict(call) for call in execution["calls"]]
    call_ids = [int(call["invocation_id"]) for call in calls]
    if len(call_ids) != len(set(call_ids)):
        raise ValueError("duplicate call invocation_id in execution")
    raw_invocation_ids = [
        int(invocation["invocation_id"]) for invocation in execution["invocations"]
    ]
    if len(raw_invocation_ids) != len(set(raw_invocation_ids)):
        raise ValueError("duplicate invocation_id in execution")
    children: Dict[int, List[Dict[str, Any]]] = {}
    for call in calls:
        children.setdefault(int(call["parent_invocation_id"]), []).append(call)
    for values in children.values():
        values.sort(key=lambda item: (int(item.get("enter_seq") or 0),
                                      int(item["invocation_id"])))
    direct = children.get(root_id, [])
    if not direct:
        raise ValueError("test root has no direct application calls")
    invocations = {
        int(invocation["invocation_id"]): dict(invocation)
        for invocation in execution["invocations"]
    }
    for call in calls:
        invocation_id = int(call["invocation_id"])
        invocation = invocations.get(invocation_id)
        if invocation is None:
            raise ValueError(f"missing invocation record: {invocation_id}")
        if int(invocation.get("parent_id") or 0) != int(call["parent_invocation_id"]):
            raise ValueError(f"inconsistent parent for invocation {invocation_id}")
    result: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    partitioned_ids = set()
    for root_call in direct:
        subtree_ids, pending = set(), [int(root_call["invocation_id"])]
        while pending:
            invocation_id = pending.pop()
            if invocation_id in subtree_ids:
                raise ValueError(f"cycle in invocation tree at {invocation_id}")
            subtree_ids.add(invocation_id)
            pending.extend(
                int(child["invocation_id"])
                for child in reversed(children.get(invocation_id, []))
            )
        if partitioned_ids.intersection(subtree_ids):
            raise ValueError("overlapping first-level invocation subtrees")
        partitioned_ids.update(subtree_ids)
        fragment_calls = sorted([
            call for call in calls if int(call["invocation_id"]) in subtree_ids
        ], key=lambda item: (int(item.get("enter_seq") or 0), int(item["invocation_id"])))
        missing = sorted(subtree_ids.difference(invocations))
        if missing:
            raise ValueError(f"missing invocation records: {missing[:20]}")
        fragment = dict(execution)
        fragment.update({
            "invocations": [dict(root)] + sorted([
                invocations[int(invocation["invocation_id"])]
                for invocation in execution["invocations"]
                if int(invocation["invocation_id"]) in subtree_ids
            ], key=lambda item: (int(item.get("enter_seq") or 0),
                                  int(item["invocation_id"]))),
            "calls": fragment_calls,
            "call_count": len(fragment_calls),
        })
        validate_trace(fragment, EXECUTION_SCHEMA)
        result.append((root_call, fragment))
    return root, result, len(calls) - sum(len(fragment["calls"]) for _, fragment in result)


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
    plantuml_command: str,
    plantuml_jar: Path | None,
    timeout: int,
    force: bool = False,
    limit_size: int = 32768,
) -> List[Dict[str, object]]:
    rows = []
    for project, bug, number, directory in layout.discover_triggers(projects, bugs, trigger):
        index_path = directory / "uml.json"
        if index_path.exists() and not force:
            try:
                existing_index = validate_uml_index(read_json(index_path), directory)
                rows.append({"project": project, "bug": bug, "trigger": number,
                             "status": "SKIPPED",
                             "segment_count": existing_index["segment_count"]})
            except Exception as error:
                write_text(layout.stage_log_dir("uml", project, bug, number) / "error.log",
                           str(error) + "\n")
                rows.append({"project": project, "bug": bug, "trigger": number,
                             "status": "ERROR", "segment_count": 0})
            continue
        try:
            sliced_path = directory / "execution_sliced.json"
            execution_path = sliced_path if sliced_path.exists() else directory / "execution.json"
            execution = read_json(execution_path)
            root, fragments, excluded_count = first_level_segments(execution)
            index_path.unlink(missing_ok=True)
            segment_dir = directory / "sequence_diagrams"
            if segment_dir.exists():
                shutil.rmtree(segment_dir)
            segment_dir.mkdir(parents=True)
            (directory / "sequence.puml").unlink(missing_ok=True)
            (directory / "sequence.png").unlink(missing_ok=True)
            segments = []
            for ordinal, (root_call, fragment) in enumerate(fragments, 1):
                invocation_id = int(root_call["invocation_id"])
                diagram_id = f"L1-{ordinal:03d}-inv-{invocation_id}"
                basename = f"segment_{ordinal:03d}_invocation_{invocation_id}"
                puml_path = segment_dir / f"{basename}.puml"
                puml = make_puml(
                    fragment, project, bug, number, include_test_boundary=False,
                    title_suffix=diagram_id,
                )
                displayed_calls = _compress_repeated_subtrees(fragment)
                write_text(puml_path, puml)
                png_path = render(
                    puml_path, plantuml_command, plantuml_jar, timeout, limit_size
                )
                method = str(root_call.get("callee_method") or root_call["callee"].rsplit(".", 1)[-1])
                descriptor = str(root_call.get("callee_descriptor") or "")
                segments.append({
                    "diagram_id": diagram_id,
                    "ordinal": ordinal,
                    "invocation_id": invocation_id,
                    "function": str(root_call["callee"]),
                    "descriptor": descriptor,
                    "signature": readable_signature(method, descriptor),
                    "method_signatures": method_signatures(fragment),
                    "origin_test_line": int(root_call.get("origin_test_line") or 0),
                    "enter_seq": int(root_call.get("enter_seq") or 0),
                    "exit_seq": int(root_call.get("exit_seq") or 0),
                    "exit_type": str(root_call.get("exit_type") or ""),
                    "call_count": len(fragment["calls"]),
                    "displayed_call_count": len(displayed_calls),
                    "puml": puml_path.relative_to(directory).as_posix(),
                    "image": png_path.relative_to(directory).as_posix(),
                })
            partitioned_count = sum(int(item["call_count"]) for item in segments)
            index = {
                "schema": "execution-uml-index", "schema_version": 2,
                "source_schema": execution["schema"],
                "source_file": execution_path.name,
                "test": dict(execution.get("test") or {}),
                "root_invocation_id": int(root["invocation_id"]),
                "strategy": "test-root-direct-invocation-subtrees",
                "slice_applied": bool((execution.get("slice") or {}).get("applied")),
                "source_call_count": len(execution["calls"]),
                "partitioned_call_count": partitioned_count,
                "excluded_call_count": excluded_count,
                "segment_count": len(segments),
                "segments": segments,
            }
            validate_uml_index(index, directory)
            write_json(index_path, index)
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "OK", "segment_count": len(segments)})
        except Exception as error:
            write_text(layout.stage_log_dir("uml", project, bug, number) / "error.log",
                       str(error) + "\n")
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "ERROR", "segment_count": 0})
    write_csv(layout.logs / "uml.csv", rows,
              ["project", "bug", "trigger", "status", "segment_count"])
    return rows
