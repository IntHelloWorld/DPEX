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
    _diagram_filename,
    make_puml,
    method_signatures,
    readable_signature,
)



def recursive_compressed_diagram_nodes(
    execution: Dict[str, Any],
    compressed: Dict[str, Any],
    roots: Sequence[Tuple[str, Dict[str, Any]]],
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
    batch_size: int,
) -> tuple[List[Dict[str, Any]], List[str], List[Dict[str, str]]]:
    """Partition and render a precompressed execution tree without recompression."""
    if max_calls <= 0:
        raise ValueError("max_calls_per_image must be positive")
    if max_participants < 2:
        raise ValueError("max_participants_per_image must be at least 2")

    numbered_items: Dict[int, Dict[str, Any]] = {}

    def collect_numbered_items(item: Dict[str, Any]) -> None:
        invocation_id = int(item["representative_invocation_id"])
        numbered_items.setdefault(invocation_id, item)
        for child in item["children"]:
            collect_numbered_items(child)

    for _, root_item in roots:
        collect_numbered_items(root_item)
    message_numbers = {
        invocation_id: ordinal
        for ordinal, invocation_id in enumerate(
            sorted(numbered_items, key=lambda value: (
                int(numbered_items[value]["invocation"].get("enter_seq") or 0), value
            )),
            1,
        )
    }

    def message_id(item: Dict[str, Any]) -> str:
        invocation_id = int(item["representative_invocation_id"])
        return f"M{message_numbers[invocation_id]:03d}"

    def invocation_signature(invocation: Dict[str, Any]) -> str:
        return (
            f"{invocation['class']}."
            f"{readable_signature(str(invocation['method']), str(invocation.get('descriptor') or ''))}"
        )

    def node_participants(items: Sequence[Dict[str, Any]]) -> set[str]:
        result: set[str] = set()
        for item in items:
            result.update(str(value) for value in item["participant_classes"])
        return result

    def boundary_record(item: Dict[str, Any]) -> Dict[str, Any]:
        boundary = dict(item["invocation"])
        call = item.get("call") or {}
        caller_class = str(
            call.get("caller_class")
            or (str(call.get("caller") or "").rsplit(".", 1)[0] if call.get("caller") else "")
        )
        if caller_class:
            boundary["caller_class"] = caller_class
        boundary["repeat_count"] = int(item.get("repeat_count") or 1)
        return boundary

    def boundary_participants(boundary: Dict[str, Any]) -> set[str]:
        result = {str(boundary["class"])}
        if boundary.get("caller_class"):
            result.add(str(boundary["caller_class"]))
        return result

    def flatten(items: Sequence[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        calls: List[Dict[str, Any]] = []
        invocations: List[Dict[str, Any]] = []

        def visit(item: Dict[str, Any]) -> None:
            call = dict(item["call"])
            call["count"] = int(item["repeat_count"])
            if item.get("repeat_sequence") is not None:
                call["repeat_sequence"] = dict(item["repeat_sequence"])
            calls.append(call)
            invocation = dict(item["invocation"])
            invocation["repeat_count"] = int(item["repeat_count"])
            invocations.append(invocation)
            for child in item["children"]:
                visit(child)

        for root in items:
            visit(root)
        calls.sort(key=lambda item: (
            int(item.get("enter_seq") or 0), int(item["invocation_id"])
        ))
        invocations.sort(key=lambda item: (
            int(item.get("enter_seq") or 0), int(item["invocation_id"])
        ))
        return calls, invocations

    def page_execution(
        items: Sequence[Dict[str, Any]], boundary: Dict[str, Any] | None
    ) -> Dict[str, Any]:
        calls, invocations = flatten(items)
        if boundary is not None and all(
            int(item["invocation_id"]) != int(boundary["invocation_id"])
            for item in invocations
        ):
            invocations.append(dict(boundary))
            invocations.sort(key=lambda item: (
                int(item.get("enter_seq") or 0), int(item["invocation_id"])
            ))
        value = dict(execution)
        value["calls"] = calls
        value["invocations"] = invocations
        value["call_count"] = len(calls)
        validate_trace(value, EXECUTION_SCHEMA)
        return value

    nodes: List[Dict[str, Any]] = []
    root_ids: List[str] = []
    page_render_inputs: Dict[str, tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}

    def common_fields(item: Dict[str, Any]) -> Dict[str, Any]:
        invocation = item["invocation"]
        call = item.get("call") or {}
        entry_signature = invocation_signature(invocation)
        repeat_count = int(item.get("repeat_count") or 1)
        return {
            "invocation_id": int(invocation["invocation_id"]),
            "function": entry_signature.rsplit("(", 1)[0],
            "signature": readable_signature(
                str(invocation["method"]), str(invocation.get("descriptor") or "")
            ),
            "entry_signature": entry_signature,
            "repeat_count": repeat_count,
            "origin_test_line": int(call.get("origin_test_line") or 0),
            "enter_seq": int(invocation.get("enter_seq") or 0),
            "exit_seq": int(invocation.get("exit_seq") or invocation.get("enter_seq") or 0),
            "exit_type": str(invocation.get("exit_type") or "RETURN"),
        }

    def add_page(
        node_id: str,
        parent_id: str | None,
        items: Sequence[Dict[str, Any]],
        entry: Dict[str, Any],
        boundary: Dict[str, Any] | None,
        boundary_repeat_count: int = 1,
    ) -> str:
        displayed = sum(int(item["displayed_subtree_call_count"]) for item in items)
        participants = node_participants(items)
        boundaries: List[Dict[str, Any]] = []
        if boundary is not None:
            boundary = dict(boundary)
            boundary["repeat_count"] = boundary_repeat_count
            boundaries = [boundary]
            displayed += 1
            participants.update(boundary_participants(boundary))
        if displayed > max_calls or len(participants) > max_participants:
            raise ValueError(
                f"unbounded compressed PAGE {node_id}: "
                f"calls={displayed}, participants={len(participants)}"
            )
        value = page_execution(items, boundary)
        puml_path = directory / _diagram_filename(node_id, "puml")
        page_render_inputs[node_id] = (value, boundaries)
        png_path = puml_path.with_suffix(".png")
        signatures = method_signatures(value)
        if boundary is not None:
            signature = invocation_signature(boundary)
            if signature not in signatures:
                signatures.insert(0, signature)
        node = {
            "node_type": "PAGE",
            "diagram_id": node_id,
            "parent_id": parent_id,
            "children": [],
            **common_fields(entry),
            "call_count": sum(int(item["represented_call_count"]) for item in items),
            "displayed_call_count": displayed,
            "participant_count": len(participants),
            "method_signatures": signatures,
            "references": [],
            "puml": puml_path.relative_to(directory.parent).as_posix(),
            "image": png_path.relative_to(directory.parent).as_posix(),
        }
        nodes.append(node)
        return node_id

    def build(
        node_id: str,
        parent_id: str | None,
        level: int,
        item: Dict[str, Any],
    ) -> str:
        node_id = re.sub(r"-inv-\d+$", f"-{message_id(item)}", node_id)
        displayed = int(item["displayed_subtree_call_count"])
        participants = len(item["participant_classes"])
        if displayed <= max_calls and participants <= max_participants:
            if item.get("call") is None:
                return add_page(
                    node_id, parent_id, item["children"], item,
                    boundary_record(item), int(item.get("repeat_count") or 1),
                )
            return add_page(node_id, parent_id, [item], item, None)

        group = {
            "node_type": "GROUP",
            "diagram_id": node_id,
            "parent_id": parent_id,
            "children": [],
            **common_fields(item),
            "call_count": int(item["represented_call_count"]),
            "displayed_call_count": displayed,
            "participant_count": participants,
        }
        nodes.append(group)
        direct = item["children"]
        if not direct:
            raise ValueError(
                f"cannot split oversized compressed leaf invocation "
                f"{item['representative_invocation_id']}"
            )
        batch: List[Dict[str, Any]] = []
        page_ordinal = 0
        boundary = boundary_record(item)
        boundary_rendered = False

        def flush_batch() -> None:
            nonlocal batch, page_ordinal, boundary_rendered
            if not batch:
                return
            page_ordinal += 1
            page_id = f"{node_id}.P{page_ordinal:03d}"
            group["children"].append(add_page(
                page_id, node_id, batch, item, boundary, int(item["repeat_count"])
            ))
            boundary_rendered = True
            batch = []

        for child in direct:
            candidate = batch + [child]
            candidate_calls = 1 + sum(
                int(value["displayed_subtree_call_count"]) for value in candidate
            )
            candidate_participants = node_participants(candidate)
            candidate_participants.update(boundary_participants(boundary))
            if candidate_calls <= max_calls and len(candidate_participants) <= max_participants:
                batch = candidate
                continue
            flush_batch()
            child_id = f"{node_id}.L{level + 1}-{message_id(child)}"
            if (
                int(child["displayed_subtree_call_count"]) <= max_calls
                and len(child["participant_classes"]) <= max_participants
            ):
                group["children"].append(add_page(
                    child_id, node_id, [child], child, None
                ))
            else:
                group["children"].append(build(child_id, node_id, level + 1, child))
        flush_batch()
        if not boundary_rendered:
            page_ordinal += 1
            page_id = f"{node_id}.P{page_ordinal:03d}"
            group["children"].insert(0, add_page(
                page_id, node_id, [], item, boundary, int(item["repeat_count"])
            ))
        return node_id

    for node_id, item in roots:
        root_ids.append(build(node_id, None, 1, item))

    nodes_by_id = {str(node["diagram_id"]): node for node in nodes}

    def boundary_pages(node_id: str) -> List[str]:
        node = nodes_by_id[node_id]
        if node["node_type"] == "PAGE":
            return [node_id]
        result = [
            child_id for child_id in node["children"]
            if nodes_by_id[child_id]["node_type"] == "PAGE"
            and int(nodes_by_id[child_id]["invocation_id"]) == int(node["invocation_id"])
        ]
        if not result:
            raise ValueError(f"GROUP has no boundary PAGE: {node_id}")
        return result

    def add_reference_pair(
        source_page_id: str,
        target_page_id: str,
        target_invocation_id: int,
        relation: str,
    ) -> None:
        if source_page_id == target_page_id:
            raise ValueError(f"self-referencing UML PAGE: {source_page_id}")
        item = numbered_items[target_invocation_id]
        shared = {
            "invocation_id": target_invocation_id,
            "message_id": message_id(item),
            "relation": relation,
        }
        nodes_by_id[source_page_id]["references"].append({
            "direction": "TO", "diagram_id": target_page_id, **shared,
        })
        nodes_by_id[target_page_id]["references"].append({
            "direction": "FROM", "diagram_id": source_page_id, **shared,
        })

    for group in (node for node in nodes if node["node_type"] == "GROUP"):
        group_id = str(group["diagram_id"])
        overview_page = boundary_pages(group_id)[0]
        for child_id in group["children"]:
            child = nodes_by_id[child_id]
            if (
                child["node_type"] == "PAGE"
                and int(child["invocation_id"]) == int(group["invocation_id"])
            ):
                continue
            target_page = boundary_pages(child_id)[0]
            add_reference_pair(
                overview_page,
                target_page,
                int(child["invocation_id"]),
                "CHILD",
            )

    for source_root_id, target_root_id in zip(root_ids, root_ids[1:]):
        source_page = boundary_pages(source_root_id)[-1]
        target_page = boundary_pages(target_root_id)[0]
        add_reference_pair(
            source_page,
            target_page,
            int(nodes_by_id[target_root_id]["invocation_id"]),
            "NEXT_SIBLING",
        )

    for node in nodes:
        if node["node_type"] != "PAGE":
            continue
        node["references"].sort(key=lambda ref: (
            0 if ref["direction"] == "FROM" else 1,
            int(numbered_items[int(ref["invocation_id"])]
                ["invocation"].get("enter_seq") or 0),
            str(ref["diagram_id"]),
        ))
        render_refs = []
        for ref in node["references"]:
            item = numbered_items[int(ref["invocation_id"])]
            boundary = boundary_record(item)
            render_refs.append({
                **ref,
                "enter_seq": int(item["invocation"].get("enter_seq") or 0),
                "caller_class": str(boundary.get("caller_class") or ""),
                "signature": readable_signature(
                    str(item["invocation"]["method"]),
                    str(item["invocation"].get("descriptor") or ""),
                ),
                "repeat_count": int(item.get("repeat_count") or 1),
            })
        value, boundaries = page_render_inputs[str(node["diagram_id"])]
        puml_path = directory.parent / Path(*Path(node["puml"]).parts)
        puml = make_puml(
            value, project, bug, trigger,
            include_test_boundary=False,
            title_suffix=str(node["diagram_id"]),
            boundary_invocations=boundaries,
            compress_calls=False,
            message_numbers=message_numbers,
            references=render_refs,
        )
        write_text(puml_path, puml)

    from mllmfl.infrastructure.plantuml import render_many
    page_nodes = [node for node in nodes if node["node_type"] == "PAGE"]
    puml_paths = [
        directory.parent / Path(*Path(node["puml"]).parts)
        for node in page_nodes
    ]
    _, failed_paths = render_many(
        puml_paths, plantuml_command, plantuml_jar, timeout,
        limit_size, batch_size,
    )
    node_by_puml = {
        directory.parent / Path(*Path(node["puml"]).parts): node
        for node in page_nodes
    }
    failures = [
        {
            "diagram_id": str(node_by_puml[path]["diagram_id"]),
            "puml": str(node_by_puml[path]["puml"]),
            "image": str(node_by_puml[path]["image"]),
            "error": error,
        }
        for path, error in failed_paths.items()
    ]
    return nodes, root_ids, failures
