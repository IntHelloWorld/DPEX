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

from .rendering import (
    _compress_repeated_subtrees,
    _diagram_filename,
    _root_test_invocation,
    make_puml,
    method_signatures,
    readable_signature,
)


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


def recursive_diagram_nodes(
    execution: Dict[str, Any],
    roots: Sequence[Tuple[str, Dict[str, Any] | None, Dict[str, Any]]],
    directory: Path,
    project: str,
    bug: str,
    trigger: str,
    plantuml_command: str,
    plantuml_jar: Path | None,
    timeout: int,
    limit_size: int,
    max_calls: int,
    max_participants: int,
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Recursively partition call trees into bounded GROUP and PAGE nodes."""
    if max_calls <= 0:
        raise ValueError("max_calls_per_image must be positive")
    if max_participants < 2:
        raise ValueError("max_participants_per_image must be at least 2")

    calls_by_id = {
        int(call["invocation_id"]): dict(call) for call in execution["calls"]
    }
    invocations = {
        int(invocation["invocation_id"]): dict(invocation)
        for invocation in execution["invocations"]
    }
    children: Dict[int, List[int]] = {}
    for call in execution["calls"]:
        children.setdefault(int(call["parent_invocation_id"]), []).append(
            int(call["invocation_id"])
        )
    for values in children.values():
        values.sort(key=lambda invocation_id: (
            int(calls_by_id[invocation_id].get("enter_seq") or 0), invocation_id
        ))

    @lru_cache(maxsize=None)
    def subtree_ids(invocation_id: int) -> Tuple[int, ...]:
        result = [invocation_id]
        for child_id in children.get(invocation_id, []):
            result.extend(subtree_ids(child_id))
        return tuple(result)

    def fragment(ids: Sequence[int], boundary: Dict[str, Any] | None = None) -> Dict[str, Any]:
        selected = set(ids)
        value = dict(execution)
        records = [
            dict(invocations[invocation_id])
            for invocation_id in selected
            if invocation_id in invocations
        ]
        if boundary is not None and int(boundary["invocation_id"]) not in selected:
            records.append(dict(boundary))
        records.sort(key=lambda item: (
            int(item.get("enter_seq") or 0), int(item["invocation_id"])
        ))
        value["invocations"] = records
        value["calls"] = sorted(
            [dict(calls_by_id[invocation_id]) for invocation_id in selected],
            key=lambda item: (int(item.get("enter_seq") or 0), int(item["invocation_id"])),
        )
        value["call_count"] = len(value["calls"])
        validate_trace(value, EXECUTION_SCHEMA)
        return value

    def full_signature(invocation: Dict[str, Any]) -> str:
        return (
            f"{invocation['class']}."
            f"{readable_signature(str(invocation['method']), str(invocation.get('descriptor') or ''))}"
        )

    def visible_stats(
        value: Dict[str, Any], boundaries: Sequence[Dict[str, Any]] = ()
    ) -> tuple[int, int]:
        displayed = _compress_repeated_subtrees(value)
        classes = set()
        for call in displayed:
            classes.add(str(call.get("caller_class") or call["caller"].rsplit(".", 1)[0]))
            classes.add(str(call.get("callee_class") or call["callee"].rsplit(".", 1)[0]))
        classes.update(str(item["class"]) for item in boundaries)
        return len(displayed) + len(boundaries), len(classes)

    nodes: List[Dict[str, Any]] = []
    root_ids: List[str] = []

    def entry_fields(
        invocation: Dict[str, Any], root_call: Dict[str, Any] | None
    ) -> Dict[str, Any]:
        signature = full_signature(invocation)
        return {
            "invocation_id": int(invocation["invocation_id"]),
            "function": signature.rsplit("(", 1)[0],
            "signature": readable_signature(
                str(invocation["method"]), str(invocation.get("descriptor") or "")
            ),
            "entry_signature": signature,
            "origin_test_line": int((root_call or {}).get("origin_test_line") or 0),
            "enter_seq": int(invocation.get("enter_seq") or 0),
            "exit_seq": int(invocation.get("exit_seq") or invocation.get("enter_seq") or 0),
            "exit_type": str(invocation.get("exit_type") or "RETURN"),
        }

    def add_page(
        node_id: str,
        parent_id: str | None,
        value: Dict[str, Any],
        entry: Dict[str, Any],
        root_call: Dict[str, Any] | None,
        boundaries: Sequence[Dict[str, Any]],
    ) -> str:
        displayed, participants = visible_stats(value, boundaries)
        if displayed > max_calls or participants > max_participants:
            raise ValueError(f"unbounded PAGE {node_id}: calls={displayed}, participants={participants}")
        puml_path = directory / _diagram_filename(node_id, "puml")
        puml = make_puml(
            value, project, bug, trigger,
            include_test_boundary=False,
            title_suffix=node_id,
            boundary_invocations=boundaries,
        )
        write_text(puml_path, puml)
        png_path = render(puml_path, plantuml_command, plantuml_jar, timeout, limit_size)
        signatures = method_signatures(value)
        for boundary in reversed(boundaries):
            signature = full_signature(boundary)
            if signature not in signatures:
                signatures.insert(0, signature)
        node = {
            "node_type": "PAGE",
            "diagram_id": node_id,
            "parent_id": parent_id,
            "children": [],
            **entry_fields(entry, root_call),
            "call_count": len(value["calls"]),
            "displayed_call_count": displayed,
            "participant_count": participants,
            "method_signatures": signatures,
            "puml": puml_path.relative_to(directory.parent).as_posix(),
            "image": png_path.relative_to(directory.parent).as_posix(),
        }
        nodes.append(node)
        return node_id

    def build(
        node_id: str,
        parent_id: str | None,
        level: int,
        root_call: Dict[str, Any] | None,
        entry: Dict[str, Any],
        ids: Sequence[int],
        boundary_leaf: bool,
    ) -> str:
        boundaries = [entry] if boundary_leaf else []
        value = fragment(ids, entry if boundary_leaf else None)
        displayed, participants = visible_stats(value, boundaries)
        if displayed <= max_calls and participants <= max_participants:
            return add_page(node_id, parent_id, value, entry, root_call, boundaries)

        group = {
            "node_type": "GROUP",
            "diagram_id": node_id,
            "parent_id": parent_id,
            "children": [],
            **entry_fields(entry, root_call),
            "call_count": len(value["calls"]),
            "displayed_call_count": displayed,
            "participant_count": participants,
        }
        nodes.append(group)
        direct_ids = children.get(int(entry["invocation_id"]), [])
        if not direct_ids:
            raise ValueError(f"cannot split oversized leaf invocation {entry['invocation_id']}")

        batch: List[int] = []
        page_ordinal = 0
        child_ordinal = 0

        def flush_batch() -> None:
            nonlocal batch, page_ordinal
            if not batch:
                return
            page_ordinal += 1
            selected = [item for root_id in batch for item in subtree_ids(root_id)]
            page_value = fragment(selected, entry)
            page_id = f"{node_id}.P{page_ordinal:03d}"
            group["children"].append(add_page(
                page_id, node_id, page_value, entry, None, [entry]
            ))
            batch = []

        for child_id in direct_ids:
            candidate = batch + [child_id]
            selected = [item for candidate_id in candidate for item in subtree_ids(candidate_id)]
            candidate_value = fragment(selected, entry)
            candidate_calls, candidate_participants = visible_stats(candidate_value, [entry])
            if candidate_calls <= max_calls and candidate_participants <= max_participants:
                batch = candidate
                continue
            flush_batch()
            child_selected = subtree_ids(child_id)
            child_value = fragment(child_selected)
            child_calls, child_participants = visible_stats(child_value)
            child_ordinal += 1
            child_id_value = f"{node_id}.N{child_ordinal:03d}-L{level + 1}-inv-{child_id}"
            child_call = calls_by_id[child_id]
            child_entry = invocations[child_id]
            if child_calls <= max_calls and child_participants <= max_participants:
                group["children"].append(add_page(
                    child_id_value, node_id, child_value, child_entry, child_call, []
                ))
            else:
                group["children"].append(build(
                    child_id_value, node_id, level + 1,
                    child_call, child_entry, child_selected, False,
                ))
        flush_batch()
        return node_id

    for node_id, root_call, entry in roots:
        if root_call is None:
            ids = tuple(
                item
                for child_id in children.get(int(entry["invocation_id"]), [])
                for item in subtree_ids(child_id)
            )
            root_ids.append(build(node_id, None, 1, None, entry, ids, True))
        else:
            root_ids.append(build(
                node_id, None, 1, root_call, entry,
                subtree_ids(int(root_call["invocation_id"])), False,
            ))
    return nodes, root_ids
