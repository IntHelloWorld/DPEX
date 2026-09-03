from pathlib import Path
from typing import Any, Dict, List, Sequence

from mllmfl.infrastructure.io import write_text

from mllmfl.infrastructure.sequence_diagram import (
    _diagram_filename,
    make_puml,
    method_signatures,
    readable_signature,
)


def focus_graph_diagram_nodes(
    directory: Path,
    planned_graph: Dict[str, Any],
    global_method_ids: Dict[tuple[str, str, str], str],
    invocation_id_prefix: str,
    diagram_title: str,
) -> tuple[
    List[Dict[str, Any]], str, List[Dict[str, str]], List[Dict[str, str]]
]:
    """Render a refinement focus viewport as one self-contained diagram."""
    planned = planned_graph

    numbered_items: Dict[int, Dict[str, Any]] = {}

    for node in planned["nodes"]:
        for item in (node.get("_numbered_items") or {}).values():
            invocation_id = int(item["representative_invocation_id"])
            numbered_items.setdefault(invocation_id, item)
    real_ids = [
        value for value, item in numbered_items.items()
        if value > 0 and item.get("call") is not None
    ]
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
    method_id_by_key = dict(global_method_ids)
    missing_method_keys = [
        key for key in ordered_method_keys if key not in method_id_by_key
    ]
    if missing_method_keys:
        raise ValueError("global method catalog is missing visible runtime methods")
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
    def call_id(invocation_id: int) -> str:
        prefix = f"{invocation_id_prefix}-" if invocation_id_prefix else ""
        return f"{prefix}C{invocation_id}"

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
            calls.append(call)
            invocation = dict(item["invocation"])
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
        return {
            "schema": "fullchain-execution",
            "schema_version": 3,
            "test": {},
            "original_call_count": len(calls),
            "filtered_call_count": len(calls),
            "call_count": len(calls),
            "test_start": None,
            "test_end": None,
            "test_failures": [],
            "invocations": invocations,
            "calls": calls,
        }

    internal_nodes = planned["nodes"]
    by_id = {str(node["diagram_id"]): node for node in internal_nodes}
    output_nodes: List[Dict[str, Any]] = []
    for node in internal_nodes:
        diagram_id = str(node["diagram_id"])
        focus_item = node["focus"]
        selected_focus_item = node.get("selected_focus") or focus_item
        focus_invocation = dict(selected_focus_item["invocation"])
        synthetic = int(focus_item["representative_invocation_id"]) == 0
        visible_items = list(node["visible_items"])
        boundary_context_fold = next(
            (
                fold for fold in node["folds"]
                if fold.get("kind") == "OMITTED_CALLS"
                and fold.get("boundary_context") is True
            ),
            None,
        )
        context_invocation_id = (
            boundary_context_fold.get("context_invocation_id")
            if boundary_context_fold is not None else None
        )
        if context_invocation_id is not None:
            context_item = numbered_items[int(context_invocation_id)]
            boundary = boundary_record(context_item)
            if focus_item.get("call") is not None:
                visible_items.append(focus_item)
        else:
            boundary = boundary_record(focus_item)
        if boundary_context_fold is not None:
            boundary["omitted_context_enter_count"] = int(
                boundary_context_fold["leading_call_count"]
            )
            boundary["omitted_context_exit_count"] = int(
                boundary_context_fold["trailing_call_count"]
            )
        if node.get("suppress_boundary_caller"):
            boundary.pop("caller_class", None)
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
        value = page_execution(visible_items, boundary)

        graph_folds: List[Dict[str, Any]] = []
        serialized_folds: List[Dict[str, Any]] = []
        for fold in node["folds"]:
            graph_folds.append(dict(fold))
            serialized_folds.append(dict(fold))

        puml_path = directory / _diagram_filename(diagram_id, "puml")
        puml = make_puml(
            value,
            title=diagram_title,
            boundary_invocations=[boundary],
            graph_folds=graph_folds,
            invocation_labels={
                invocation_id: call_id(invocation_id)
                for invocation_id in invocation_method_ids
            },
            highlighted_invocation_ids={
                int(selected_focus_item["representative_invocation_id"])
            },
        )
        write_text(puml_path, puml)

        signatures = method_signatures(value)
        if not synthetic:
            focus_signature = invocation_signature(focus_invocation)
            if focus_signature not in signatures:
                signatures.insert(0, focus_signature)
            if (
                focus_item.get("call") is not None
                and boundary_context_fold is None
            ):
                boundary_signature = invocation_signature(boundary)
                if boundary_signature not in signatures:
                    signatures.insert(0, boundary_signature)
        call = focus_item.get("call") or {}
        output = {
            "diagram_id": diagram_id,
            "focus_invocation_id": int(
                selected_focus_item["representative_invocation_id"]
            ),
            "entry_signature": (
                "(synthetic execution root)"
                if synthetic else invocation_signature(focus_invocation)
            ),
            "origin_test_line": int(call.get("origin_test_line") or 0),
            "visible_call_count": int(node["visible_represented_call_count"]),
            "visible_unit_count": int(node["visible_unit_count"]),
            "participant_count": len(node["participant_classes"]),
            "represented_call_count": int(node["represented_call_count"]),
            "visible_represented_call_count": int(
                node["visible_represented_call_count"]
            ),
            "omitted_call_count": int(node["omitted_call_count"]),
            "has_omitted_calls": bool(node["has_omitted_calls"]),
            "omitted_region_count": int(node["omitted_region_count"]),
            "method_signatures": signatures,
            "folds": serialized_folds,
            "links": [],
            "puml": puml_path.relative_to(directory.parent).as_posix(),
            "image": puml_path.with_suffix(".png").relative_to(directory.parent).as_posix(),
        }
        for field in (
            "scope",
            "upstream_visible_call_count",
            "downstream_visible_call_count",
            "internal_visible_call_count",
            "structural_context_call_count",
        ):
            if field in node:
                output[field] = node[field]
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
        focus_id = int(selected_focus_item["representative_invocation_id"])
        if not synthetic and focus_id in invocation_method_ids:
            visible_invocation_ids.insert(0, focus_id)
        output["method_ids"] = list(dict.fromkeys(
            invocation_method_ids[invocation_id]
            for invocation_id in visible_invocation_ids
            if invocation_id in invocation_method_ids
        ))
        output_nodes.append(output)
    return output_nodes, str(planned["entry_diagram_id"]), [], method_catalog
