import hashlib
import re
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from mllmfl.domain.interaction import IMAGE_ONLY_MODE, TEXT_INDEX_MODE
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


def adaptive_graph_diagram_nodes(
    execution: Dict[str, Any],
    focus: Dict[str, Any],
    entry_reason: str,
    directory: Path,
    project: str,
    bug: str,
    trigger: str,
    plantuml_command: str,
    plantuml_jar: Path | None,
    timeout: int,
    limit_size: int,
    max_visible_units: int,
    max_participants: int,
    batch_size: int,
    interaction_mode: str = TEXT_INDEX_MODE,
) -> tuple[
    List[Dict[str, Any]], str, List[Dict[str, str]], List[Dict[str, str]]
]:
    """Plan and render uniformly viewable bounded nodes for one execution root."""
    if interaction_mode not in {TEXT_INDEX_MODE, IMAGE_ONLY_MODE}:
        raise ValueError("interaction_mode must be text_index or image_only")
    planned = plan_diagram_graph(
        focus,
        max_visible_units,
        max_participants,
        entry_reason,
    )

    numbered_items: Dict[int, Dict[str, Any]] = {}

    def collect(item: Dict[str, Any]) -> None:
        invocation_id = int(item["representative_invocation_id"])
        numbered_items.setdefault(invocation_id, item)
        for child in item.get("children") or []:
            collect(child)

    collect(focus)
    real_ids = [
        value for value, item in numbered_items.items()
        if value > 0 and item.get("call") is not None
    ]
    message_numbers = {
        invocation_id: ordinal
        for ordinal, invocation_id in enumerate(
            sorted(real_ids, key=lambda value: (
                int(numbered_items[value]["invocation"].get("enter_seq") or 0),
                value,
            )),
            1,
        )
    }
    if int(focus["representative_invocation_id"]) == 0:
        message_numbers[0] = 0
    elif int(focus["representative_invocation_id"]) not in message_numbers:
        message_numbers[int(focus["representative_invocation_id"])] = 0

    method_keys: Dict[int, tuple[str, str, str]] = {
        invocation_id: (
            str(item["invocation"]["class"]),
            str(item["invocation"]["method"]),
            str(item["invocation"].get("descriptor") or ""),
        )
        for invocation_id, item in numbered_items.items()
        if invocation_id in real_ids
    }
    ordered_method_keys = list(dict.fromkeys(
        method_keys[invocation_id]
        for invocation_id in sorted(
            real_ids,
            key=lambda value: (
                int(numbered_items[value]["invocation"].get("enter_seq") or 0),
                value,
            ),
        )
    ))
    method_id_by_key = {
        key: f"M{ordinal:03d}"
        for ordinal, key in enumerate(ordered_method_keys, 1)
    }
    invocation_method_ids = {
        invocation_id: method_id_by_key[key]
        for invocation_id, key in method_keys.items()
    }
    method_catalog = [
        {
            "method_id": method_id_by_key[(class_name, method, descriptor)],
            "function": f"{class_name}.{method}",
            "signature": (
                f"{class_name}.{readable_signature(method, descriptor)}"
            ),
            "descriptor": descriptor,
        }
        for class_name, method, descriptor in ordered_method_keys
    ]
    call_prefix = "C" if interaction_mode == IMAGE_ONLY_MODE else "M"

    def message_id(invocation_id: int) -> str:
        return f"{call_prefix}{int(message_numbers[invocation_id]):03d}"

    def invocation_signature(invocation: Dict[str, Any]) -> str:
        return (
            f"{invocation['class']}."
            f"{readable_signature(str(invocation['method']), str(invocation.get('descriptor') or ''))}"
        )

    def boundary_record(item: Dict[str, Any]) -> Dict[str, Any]:
        boundary = dict(item["invocation"])
        call = item.get("call") or {}
        caller_class = str(
            call.get("caller_class")
            or (
                str(call.get("caller") or "").rsplit(".", 1)[0]
                if call.get("caller")
                else ""
            )
        )
        if caller_class:
            boundary["caller_class"] = caller_class
        boundary["repeat_count"] = int(item.get("repeat_count") or 1)
        if item.get("call") is None:
            boundary["layout_root"] = True
        return boundary

    def page_execution(items: Sequence[Dict[str, Any]], boundary: Dict[str, Any]) -> Dict[str, Any]:
        calls: List[Dict[str, Any]] = []
        invocations: List[Dict[str, Any]] = []
        for item in items:
            call = dict(item.get("call") or {})
            if not call:
                continue
            call["count"] = int(item.get("repeat_count") or 1)
            if item.get("repeat_sequence") is not None:
                call["repeat_sequence"] = dict(item["repeat_sequence"])
            calls.append(call)
            invocation = dict(item["invocation"])
            invocation["repeat_count"] = int(item.get("repeat_count") or 1)
            invocations.append(invocation)
        if int(boundary["invocation_id"]) > 0 and all(
            int(item["invocation_id"]) != int(boundary["invocation_id"])
            for item in invocations
        ):
            invocations.append(dict(boundary))
        calls.sort(key=lambda item: (
            int(item.get("enter_seq") or 0), int(item["invocation_id"])
        ))
        invocations.sort(key=lambda item: (
            int(item.get("enter_seq") or 0), int(item["invocation_id"])
        ))
        value = dict(execution)
        value["calls"] = calls
        value["invocations"] = invocations
        value["call_count"] = len(calls)
        validate_trace(value, EXECUTION_SCHEMA)
        return value

    internal_nodes = planned["nodes"]
    by_id = {str(node["diagram_id"]): node for node in internal_nodes}
    output_nodes: List[Dict[str, Any]] = []
    puml_paths: List[Path] = []
    node_by_puml: Dict[Path, Dict[str, Any]] = {}

    for node in internal_nodes:
        diagram_id = str(node["diagram_id"])
        focus_item = node["focus"]
        focus_invocation = dict(focus_item["invocation"])
        synthetic = int(focus_item["representative_invocation_id"]) == 0
        boundary = boundary_record(focus_item)
        if node.get("sibling_group") is not None:
            boundary["repeat_focus_call"] = True
        if synthetic:
            boundary.update({
                "invocation_id": 0,
                "class": "mllmfl.synthetic.ExecutionRoot",
                "method": "executionRoot",
                "descriptor": "()V",
                "enter_seq": 0,
                "exit_seq": max(
                    [int(item["invocation"].get("exit_seq") or 0)
                     for item in node["visible_items"]] or [0]
                ) + 1,
                "exit_type": "RETURN",
                "synthetic": True,
            })
        value = page_execution(node["visible_items"], boundary)

        incoming_refs: List[Dict[str, Any]] = []
        incoming = next(
            (link for link in node["links"] if link["direction"] == "FROM"),
            None,
        )
        if incoming is not None:
            incoming_refs.append({
                **incoming,
                "invocation_id": int(boundary["invocation_id"]),
                "enter_seq": int(boundary.get("enter_seq") or 0),
                "caller_class": str(boundary.get("caller_class") or ""),
                "signature": readable_signature(
                    str(boundary["method"]), str(boundary.get("descriptor") or "")
                ),
                "repeat_count": int(boundary.get("repeat_count") or 1),
            })

        graph_folds: List[Dict[str, Any]] = []
        serialized_folds: List[Dict[str, Any]] = []
        for fold in node["folds"]:
            invocation_ids = [int(value) for value in fold["invocation_ids"]]
            first_item = numbered_items[invocation_ids[0]]
            last_item = numbered_items[invocation_ids[-1]]
            anchor_item = numbered_items[int(fold["anchor_invocation_id"])]
            anchor_class = str(anchor_item["invocation"]["class"])
            message_range = (
                message_id(invocation_ids[0])
                if len(invocation_ids) == 1
                else f"{message_id(invocation_ids[0])}-{message_id(invocation_ids[-1])}"
            )
            render_fold = {
                **fold,
                "anchor_class": anchor_class,
                "message_range": message_range,
            }
            serialized_peer_ranges = []
            if fold["kind"] == "SIBLING_BUNDLE":
                for peer_range in fold["peer_ranges"]:
                    peer_ids = [
                        int(value) for value in peer_range["invocation_ids"]
                    ]
                    serialized_peer_ranges.append({
                        **peer_range,
                        "message_ids": [message_id(value) for value in peer_ids],
                        "message_range": (
                            message_id(peer_ids[0])
                            if len(peer_ids) == 1
                            else f"{message_id(peer_ids[0])}-{message_id(peer_ids[-1])}"
                        ),
                        "first_signature": invocation_signature(
                            numbered_items[peer_ids[0]]["invocation"]
                        ),
                        "last_signature": invocation_signature(
                            numbered_items[peer_ids[-1]]["invocation"]
                        ),
                    })
                render_fold["peer_ranges"] = serialized_peer_ranges
            graph_folds.append(render_fold)
            serialized_folds.append({
                **fold,
                **(
                    {"peer_ranges": serialized_peer_ranges}
                    if fold["kind"] == "SIBLING_BUNDLE"
                    else {}
                ),
                "message_range": message_range,
                "first_signature": invocation_signature(first_item["invocation"]),
                "last_signature": invocation_signature(last_item["invocation"]),
            })

        serialized_links = []
        for link in node["links"]:
            invocation_ids = [int(value) for value in link["invocation_ids"]]
            serialized_links.append({
                **link,
                "message_ids": [message_id(value) for value in invocation_ids],
            })

        puml_path = directory / _diagram_filename(diagram_id, "puml")
        puml = make_puml(
            value,
            project,
            bug,
            trigger,
            include_test_boundary=False,
            title_suffix=diagram_id,
            boundary_invocations=[boundary],
            compress_calls=False,
            message_numbers=message_numbers,
            references=incoming_refs,
            graph_folds=graph_folds,
            message_prefix=call_prefix,
            method_ids=(
                invocation_method_ids
                if interaction_mode == IMAGE_ONLY_MODE else None
            ),
        )
        write_text(puml_path, puml)
        puml_paths.append(puml_path)

        signatures = method_signatures(value)
        if not synthetic:
            focus_signature = invocation_signature(focus_invocation)
            if focus_signature not in signatures:
                signatures.insert(0, focus_signature)
        call = focus_item.get("call") or {}
        sibling_group = node.get("sibling_group")
        serialized_sibling_group = None
        if sibling_group is not None:
            serialized_sibling_group = {
                **sibling_group,
                "visible_message_ids": [
                    message_id(value)
                    for value in sibling_group["visible_invocation_ids"]
                ],
            }
        output = {
            "diagram_id": diagram_id,
            "focus_invocation_id": int(focus_item["representative_invocation_id"]),
            "entry_signature": (
                "(synthetic execution root)"
                if synthetic else invocation_signature(focus_invocation)
            ),
            "origin_test_line": int(call.get("origin_test_line") or 0),
            "represented_call_count": int(node["represented_call_count"]),
            "visible_call_count": len(node["visible_items"]),
            "visible_unit_count": int(node["visible_unit_count"]),
            "participant_count": len(node["participant_classes"]),
            "method_signatures": signatures,
            "folds": serialized_folds,
            "links": serialized_links,
            "sibling_group": serialized_sibling_group,
            "puml": puml_path.relative_to(directory.parent).as_posix(),
            "image": puml_path.with_suffix(".png").relative_to(directory.parent).as_posix(),
        }
        if interaction_mode == IMAGE_ONLY_MODE:
            visible_invocation_ids = [
                int(call["invocation_id"])
                for call in sorted(
                    value["calls"],
                    key=lambda item: (
                        int(item.get("enter_seq") or 0),
                        int(item["invocation_id"]),
                    ),
                )
            ]
            focus_id = int(focus_item["representative_invocation_id"])
            if not synthetic and focus_id in invocation_method_ids:
                visible_invocation_ids.insert(0, focus_id)
            output["method_ids"] = list(dict.fromkeys(
                invocation_method_ids[invocation_id]
                for invocation_id in visible_invocation_ids
                if invocation_id in invocation_method_ids
            ))
        output_nodes.append(output)
        node_by_puml[puml_path] = output

    from mllmfl.infrastructure.plantuml import render_many
    _, failed_paths = render_many(
        puml_paths,
        plantuml_command,
        plantuml_jar,
        timeout,
        limit_size,
        batch_size,
    )
    failures = [
        {
            "diagram_id": str(node_by_puml[path]["diagram_id"]),
            "image": str(node_by_puml[path]["image"]),
            "error": str(error),
        }
        for path, error in sorted(failed_paths.items(), key=lambda item: str(item[0]))
    ]
    return output_nodes, str(planned["entry_diagram_id"]), failures, (
        method_catalog if interaction_mode == IMAGE_ONLY_MODE else []
    )
