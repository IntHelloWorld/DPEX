import re
from pathlib import Path
from typing import Any, Dict

from .uml_support import artifact_path as _artifact_path


def validate_recursive_uml_index(
    value: Dict[str, Any], base_dir: Path | None
) -> Dict[str, Any]:
    schema_version = int(value["schema_version"])
    if value.get("source_schema") != "fullchain-execution":
        raise ValueError("invalid UML index source schema")
    strategy = value.get("strategy")
    if strategy not in {
        "test-root-recursive-invocation-pages",
        "complete-trace-recursive-pages",
    }:
        raise ValueError("invalid recursive UML index strategy")
    if not isinstance(value.get("slice_applied"), bool):
        raise ValueError("invalid UML index slice_applied")
    test = value.get("test")
    if not isinstance(test, dict) or not all(
        isinstance(test.get(field), str) and test[field].strip()
        for field in ("class", "method")
    ):
        raise ValueError("invalid UML index test")
    root_invocation_id = value.get("root_invocation_id")
    if not isinstance(root_invocation_id, int) or (
        strategy == "test-root-recursive-invocation-pages" and root_invocation_id <= 0
    ) or (
        strategy == "complete-trace-recursive-pages" and root_invocation_id != 0
    ):
        raise ValueError("invalid UML index root invocation")
    max_calls = value.get("max_calls_per_image")
    max_participants = value.get("max_participants_per_image")
    if not isinstance(max_calls, int) or max_calls <= 0:
        raise ValueError("invalid UML max_calls_per_image")
    if not isinstance(max_participants, int) or max_participants < 2:
        raise ValueError("invalid UML max_participants_per_image")

    nodes = value.get("nodes")
    roots = value.get("root_ids")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("recursive UML nodes must be a non-empty array")
    if not isinstance(roots, list) or not roots or not all(
        isinstance(item, str) and item for item in roots
    ):
        raise ValueError("recursive UML root_ids must be a non-empty string array")
    by_id: Dict[str, Dict[str, Any]] = {}
    page_count = group_count = 0
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError(f"invalid UML node at index {index}")
        diagram_id = node.get("diagram_id")
        if not isinstance(diagram_id, str) or not diagram_id.strip():
            raise ValueError(f"invalid UML node diagram_id at index {index}")
        if diagram_id in by_id:
            raise ValueError(f"duplicate UML node diagram_id: {diagram_id}")
        by_id[diagram_id] = node
        if node.get("node_type") not in {"GROUP", "PAGE"}:
            raise ValueError(f"invalid UML node type: {diagram_id}")
        parent_id = node.get("parent_id")
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id):
            raise ValueError(f"invalid UML node parent: {diagram_id}")
        children = node.get("children")
        if not isinstance(children, list) or not all(
            isinstance(item, str) and item for item in children
        ) or len(children) != len(set(children)):
            raise ValueError(f"invalid UML node children: {diagram_id}")
        if not isinstance(node.get("entry_signature"), str) or not node["entry_signature"]:
            raise ValueError(f"invalid UML node entry signature: {diagram_id}")
        if not isinstance(node.get("invocation_id"), int) or node["invocation_id"] <= 0:
            raise ValueError(f"invalid UML node invocation: {diagram_id}")
        raw_calls = node.get("call_count")
        displayed = node.get("displayed_call_count")
        participants = node.get("participant_count")
        if not isinstance(raw_calls, int) or raw_calls < 0:
            raise ValueError(f"invalid UML node raw calls: {diagram_id}")
        if not isinstance(displayed, int) or displayed <= 0:
            raise ValueError(f"invalid UML node displayed calls: {diagram_id}")
        if not isinstance(participants, int) or participants <= 0:
            raise ValueError(f"invalid UML node participants: {diagram_id}")
        if node["node_type"] == "GROUP":
            group_count += 1
            if not children or "image" in node or "puml" in node:
                raise ValueError(f"invalid UML GROUP node: {diagram_id}")
        else:
            page_count += 1
            if children or displayed > max_calls or participants > max_participants:
                raise ValueError(f"unbounded UML PAGE node: {diagram_id}")
            signatures = node.get("method_signatures")
            if not isinstance(signatures, list) or not signatures or not all(
                isinstance(item, str) and item for item in signatures
            ) or len(signatures) != len(set(signatures)):
                raise ValueError(f"invalid UML PAGE signatures: {diagram_id}")
            puml = _artifact_path(node.get("puml"), "puml", base_dir)
            image = _artifact_path(node.get("image"), "image", base_dir)
            if not puml.endswith(".puml") or not image.endswith(".png"):
                raise ValueError(f"invalid UML PAGE file extension: {diagram_id}")
            if schema_version == 4:
                references = node.get("references")
                if not isinstance(references, list):
                    raise ValueError(f"invalid UML PAGE references: {diagram_id}")
                seen_references = set()
                for reference in references:
                    if not isinstance(reference, dict):
                        raise ValueError(f"invalid UML PAGE reference: {diagram_id}")
                    direction = reference.get("direction")
                    target_id = reference.get("diagram_id")
                    invocation_id = reference.get("invocation_id")
                    message_id = reference.get("message_id")
                    relation = reference.get("relation")
                    if (
                        direction not in {"FROM", "TO"}
                        or not isinstance(target_id, str) or not target_id
                        or not isinstance(invocation_id, int) or invocation_id <= 0
                        or not isinstance(message_id, str)
                        or re.fullmatch(r"M\d{3,}", message_id) is None
                        or relation not in {"CHILD", "NEXT_SIBLING"}
                    ):
                        raise ValueError(f"invalid UML PAGE reference: {diagram_id}")
                    key = (direction, target_id, invocation_id, message_id, relation)
                    if key in seen_references:
                        raise ValueError(f"duplicate UML PAGE reference: {diagram_id}")
                    seen_references.add(key)

    if len(set(roots)) != len(roots) or any(root not in by_id for root in roots):
        raise ValueError("invalid recursive UML roots")
    if any(by_id[root].get("parent_id") is not None for root in roots):
        raise ValueError("recursive UML root has a parent")
    for diagram_id, node in by_id.items():
        parent_id = node.get("parent_id")
        if parent_id is not None and (
            parent_id not in by_id or diagram_id not in by_id[parent_id]["children"]
        ):
            raise ValueError(f"inconsistent UML parent link: {diagram_id}")
        for child_id in node["children"]:
            if child_id not in by_id or by_id[child_id].get("parent_id") != diagram_id:
                raise ValueError(f"inconsistent UML child link: {diagram_id}")
        if schema_version == 4 and node["node_type"] == "PAGE":
            for reference in node["references"]:
                target_id = reference["diagram_id"]
                if target_id not in by_id or by_id[target_id]["node_type"] != "PAGE":
                    raise ValueError(f"invalid UML reference target: {diagram_id}")
                reverse_direction = "FROM" if reference["direction"] == "TO" else "TO"
                reverse = {
                    "direction": reverse_direction,
                    "diagram_id": diagram_id,
                    "invocation_id": reference["invocation_id"],
                    "message_id": reference["message_id"],
                    "relation": reference["relation"],
                }
                if reverse not in by_id[target_id]["references"]:
                    raise ValueError(f"unpaired UML PAGE reference: {diagram_id}")
    visited, pending = set(), list(reversed(roots))
    while pending:
        diagram_id = pending.pop()
        if diagram_id in visited:
            raise ValueError(f"cycle or duplicate reachability in UML tree: {diagram_id}")
        visited.add(diagram_id)
        pending.extend(reversed(by_id[diagram_id]["children"]))
    if visited != set(by_id):
        raise ValueError("unreachable UML nodes")
    if value.get("node_count") != len(nodes):
        raise ValueError("inconsistent UML node count")
    if value.get("page_count") != page_count or value.get("group_count") != group_count:
        raise ValueError("inconsistent UML node type counts")
    source = value.get("source_call_count")
    partitioned = value.get("partitioned_call_count")
    excluded = value.get("excluded_call_count")
    if not all(isinstance(item, int) and item >= 0 for item in (source, partitioned, excluded)):
        raise ValueError("invalid UML index call counts")
    if source != partitioned + excluded:
        raise ValueError("inconsistent UML index call counts")
    if strategy == "complete-trace-recursive-pages" and (
        value["slice_applied"] or excluded != 0
    ):
        raise ValueError("invalid complete-trace recursive UML index")
    return value
