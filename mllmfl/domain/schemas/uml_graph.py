import re
from pathlib import Path
from typing import Any, Dict

from mllmfl.domain.interaction import IMAGE_ONLY_MODE

from .uml_support import artifact_path as _artifact_path


def validate_adaptive_uml_graph(
    value: Dict[str, Any], base_dir: Path | None
) -> Dict[str, Any]:
    schema_version = value.get("schema_version")
    if schema_version not in {1, 2, 3}:
        raise ValueError("unsupported UML graph schema")
    image_only = schema_version in {2, 3}
    if image_only:
        if schema_version == 2 and value.get("interaction_mode") != IMAGE_ONLY_MODE:
            raise ValueError("invalid image-only UML graph interaction mode")
        if schema_version == 3 and "interaction_mode" in value:
            raise ValueError("UML graph v3 is image-only and has no interaction mode")
        catalog = value.get("method_catalog")
        if not isinstance(catalog, list):
            raise ValueError("invalid UML graph method catalog")
        catalog_by_id: Dict[str, Dict[str, str]] = {}
        catalog_keys = set()
        for index, item in enumerate(catalog):
            if not isinstance(item, dict):
                raise ValueError(f"invalid UML graph method at index {index}")
            method_id = item.get("method_id")
            function = item.get("function")
            signature = item.get("signature")
            descriptor = item.get("descriptor")
            if (
                not isinstance(method_id, str)
                or re.fullmatch(r"M\d{3,}", method_id) is None
                or method_id in catalog_by_id
                or not isinstance(function, str)
                or not function.strip()
                or not isinstance(signature, str)
                or not signature.strip()
                or signature.rsplit("(", 1)[0] != function
                or not isinstance(descriptor, str)
            ):
                raise ValueError(f"invalid UML graph method at index {index}")
            key = (function, descriptor)
            if key in catalog_keys:
                raise ValueError(f"duplicate UML graph method: {function}{descriptor}")
            catalog_keys.add(key)
            catalog_by_id[method_id] = item
    else:
        if "interaction_mode" in value or "method_catalog" in value:
            raise ValueError("legacy UML graph must not contain image-only metadata")
        catalog_by_id = {}
    message_id_pattern = r"C\d{3,}" if image_only else r"M\d{3,}"
    if value.get("source_schema") != "fullchain-execution":
        raise ValueError("invalid UML graph source schema")
    if value.get("strategy") not in {
        "test-root-adaptive-graph",
        "synthetic-root-adaptive-graph",
    }:
        raise ValueError("invalid UML graph strategy")
    if value.get("entry_reason") not in {
        "test_invocation",
        "synthetic_execution_root",
    }:
        raise ValueError("invalid UML graph entry reason")
    if (
        value["strategy"] == "test-root-adaptive-graph"
        and value["entry_reason"] != "test_invocation"
    ) or (
        value["strategy"] == "synthetic-root-adaptive-graph"
        and value["entry_reason"] != "synthetic_execution_root"
    ):
        raise ValueError("inconsistent UML graph entry reason")
    if not isinstance(value.get("slice_applied"), bool):
        raise ValueError("invalid UML graph slice_applied")
    test = value.get("test")
    if not isinstance(test, dict) or not all(
        isinstance(test.get(field), str) and test[field].strip()
        for field in ("class", "method")
    ):
        raise ValueError("invalid UML graph test")
    root_invocation_id = value.get("root_invocation_id")
    if not isinstance(root_invocation_id, int) or root_invocation_id < 0:
        raise ValueError("invalid UML graph root invocation")
    max_units = value.get("max_visible_units")
    max_participants = value.get("max_participants_per_image")
    if not isinstance(max_units, int) or max_units <= 1:
        raise ValueError("invalid UML graph max_visible_units")
    if not isinstance(max_participants, int) or max_participants < 2:
        raise ValueError("invalid UML graph max_participants_per_image")
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("UML graph nodes must be a non-empty array")
    entry_id = value.get("entry_diagram_id")
    if not isinstance(entry_id, str) or not entry_id:
        raise ValueError("invalid UML graph entry_diagram_id")
    if schema_version == 3:
        test_id = value.get("test_id")
        fingerprint = value.get("method_catalog_fingerprint")
        if (
            not isinstance(test_id, str)
            or re.fullmatch(r"T\d{3,}", test_id) is None
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            or re.fullmatch(rf"{re.escape(test_id)}-D\d{{3,}}", entry_id) is None
        ):
            raise ValueError("invalid UML graph v3 identity")

    by_id: Dict[str, Dict[str, Any]] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError(f"invalid UML graph node at index {index}")
        if "node_type" in node or "children" in node:
            raise ValueError("legacy GROUP/PAGE fields are not allowed in UML graph")
        diagram_id = node.get("diagram_id")
        if not isinstance(diagram_id, str) or not diagram_id:
            raise ValueError(f"invalid UML graph diagram_id at index {index}")
        if schema_version == 3 and re.fullmatch(
            rf"{re.escape(value['test_id'])}-D\d{{3,}}", diagram_id
        ) is None:
            raise ValueError(f"invalid UML graph v3 diagram_id: {diagram_id}")
        if diagram_id in by_id:
            raise ValueError(f"duplicate UML graph diagram_id: {diagram_id}")
        by_id[diagram_id] = node
        focus_id = node.get("focus_invocation_id")
        if not isinstance(focus_id, int) or focus_id < 0:
            raise ValueError(f"invalid UML graph focus invocation: {diagram_id}")
        if not isinstance(node.get("entry_signature"), str) or not node["entry_signature"]:
            raise ValueError(f"invalid UML graph entry signature: {diagram_id}")
        origin_line = node.get("origin_test_line")
        represented = node.get("represented_call_count")
        visible_calls = node.get("visible_call_count")
        visible_units = node.get("visible_unit_count")
        participants = node.get("participant_count")
        if not isinstance(origin_line, int) or origin_line < 0:
            raise ValueError(f"invalid UML graph origin line: {diagram_id}")
        if not isinstance(represented, int) or represented < 0:
            raise ValueError(f"invalid UML graph represented calls: {diagram_id}")
        if not isinstance(visible_calls, int) or visible_calls < 0:
            raise ValueError(f"invalid UML graph visible calls: {diagram_id}")
        if (
            not isinstance(visible_units, int)
            or visible_units < visible_calls
            or visible_units > max_units
        ):
            raise ValueError(f"invalid UML graph visible units: {diagram_id}")
        if (
            not isinstance(participants, int)
            or participants <= 0
            or participants > max_participants
        ):
            raise ValueError(f"invalid UML graph participants: {diagram_id}")
        signatures = node.get("method_signatures")
        if (
            not isinstance(signatures, list)
            or not signatures
            or not all(isinstance(item, str) and item for item in signatures)
            or len(signatures) != len(set(signatures))
        ):
            raise ValueError(f"invalid UML graph method signatures: {diagram_id}")
        if image_only:
            method_ids = node.get("method_ids")
            if (
                not isinstance(method_ids, list)
                or not all(
                    isinstance(item, str) and item in catalog_by_id
                    for item in method_ids
                )
                or len(method_ids) != len(set(method_ids))
                or any(
                    catalog_by_id[item]["signature"] not in signatures
                    for item in method_ids
                )
            ):
                raise ValueError(f"invalid UML graph method IDs: {diagram_id}")
        elif "method_ids" in node:
            raise ValueError(f"legacy UML graph has method IDs: {diagram_id}")
        puml = _artifact_path(node.get("puml"), "puml", base_dir)
        image = _artifact_path(node.get("image"), "image", base_dir)
        if not puml.endswith(".puml") or not image.endswith(".png"):
            raise ValueError(f"invalid UML graph file extension: {diagram_id}")

        folds = node.get("folds")
        links = node.get("links")
        if not isinstance(folds, list) or not isinstance(links, list):
            raise ValueError(f"invalid UML graph navigation arrays: {diagram_id}")
        sibling_group = node.get("sibling_group")
        if sibling_group is not None:
            if not isinstance(sibling_group, dict):
                raise ValueError(f"invalid UML graph sibling group: {diagram_id}")
            group_id = sibling_group.get("group_id")
            ordinal = sibling_group.get("ordinal")
            count = sibling_group.get("count")
            visible_ids = sibling_group.get("visible_invocation_ids")
            visible_message_ids = sibling_group.get("visible_message_ids")
            peer_ids = sibling_group.get("peer_diagram_ids")
            if (
                not isinstance(group_id, str) or not group_id
                or not isinstance(ordinal, int) or ordinal <= 0
                or not isinstance(count, int) or count <= 1 or ordinal > count
                or not isinstance(visible_ids, list) or not visible_ids
                or not all(isinstance(item, int) and item > 0 for item in visible_ids)
                or len(visible_ids) != len(set(visible_ids))
                or not isinstance(visible_message_ids, list)
                or len(visible_message_ids) != len(visible_ids)
                or not all(
                    isinstance(item, str) and re.fullmatch(message_id_pattern, item)
                    for item in visible_message_ids
                )
                or not isinstance(peer_ids, list) or len(peer_ids) != count - 1
                or not all(isinstance(item, str) and item for item in peer_ids)
                or len(peer_ids) != len(set(peer_ids))
                or diagram_id in peer_ids
            ):
                raise ValueError(f"invalid UML graph sibling group: {diagram_id}")
        seen_folds = set()
        for fold in folds:
            if not isinstance(fold, dict):
                raise ValueError(f"invalid UML graph fold: {diagram_id}")
            kind = fold.get("kind")
            target_id = fold.get("target_diagram_id")
            invocation_ids = fold.get("invocation_ids")
            if (
                kind not in {"CALL_INTERNAL", "SIBLING_BUNDLE"}
                or not isinstance(invocation_ids, list) or not invocation_ids
                or not all(isinstance(item, int) and item > 0 for item in invocation_ids)
                or len(invocation_ids) != len(set(invocation_ids))
            ):
                raise ValueError(f"invalid UML graph fold: {diagram_id}")
            if kind == "CALL_INTERNAL":
                if (
                    not isinstance(target_id, str) or not target_id
                    or len(invocation_ids) != 1
                ):
                    raise ValueError(f"invalid CALL_INTERNAL fold: {diagram_id}")
                if fold.get("anchor_invocation_id") != invocation_ids[0]:
                    raise ValueError(f"invalid CALL_INTERNAL anchor: {diagram_id}")
            else:
                if target_id is not None or fold.get("position") not in {"PREFIX", "SUFFIX"}:
                    raise ValueError(f"invalid SIBLING_BUNDLE fold: {diagram_id}")
                peer_ranges = fold.get("peer_ranges")
                if not isinstance(peer_ranges, list) or not peer_ranges:
                    raise ValueError(f"invalid sibling peer ranges: {diagram_id}")
                flattened_ids = []
                for peer_range in peer_ranges:
                    if not isinstance(peer_range, dict):
                        raise ValueError(f"invalid sibling peer range: {diagram_id}")
                    peer_invocation_ids = peer_range.get("invocation_ids")
                    peer_message_ids = peer_range.get("message_ids")
                    if (
                        not isinstance(peer_range.get("diagram_id"), str)
                        or not peer_range["diagram_id"]
                        or not isinstance(peer_invocation_ids, list)
                        or not peer_invocation_ids
                        or not all(
                            isinstance(item, int) and item > 0
                            for item in peer_invocation_ids
                        )
                        or not isinstance(peer_message_ids, list)
                        or len(peer_message_ids) != len(peer_invocation_ids)
                        or not all(
                            isinstance(item, str) and re.fullmatch(message_id_pattern, item)
                            for item in peer_message_ids
                        )
                        or not isinstance(peer_range.get("message_range"), str)
                        or not peer_range["message_range"]
                        or not all(
                            isinstance(peer_range.get(field), int)
                            and peer_range[field] >= minimum
                            for field, minimum in (
                                ("enter_seq", 0), ("exit_seq", 0),
                                ("represented_call_count", 1),
                                ("represented_participant_count", 1),
                            )
                        )
                        or peer_range["exit_seq"] < peer_range["enter_seq"]
                        or not all(
                            isinstance(peer_range.get(field), str) and peer_range[field]
                            for field in ("first_signature", "last_signature")
                        )
                    ):
                        raise ValueError(f"invalid sibling peer range: {diagram_id}")
                    flattened_ids.extend(peer_invocation_ids)
                if flattened_ids != invocation_ids:
                    raise ValueError(f"inconsistent sibling peer ranges: {diagram_id}")
            fold_key = (kind, fold.get("position"), target_id, tuple(invocation_ids))
            if fold_key in seen_folds:
                raise ValueError(f"duplicate UML graph fold: {diagram_id}")
            seen_folds.add(fold_key)
            if not all(
                isinstance(fold.get(field), int) and fold[field] >= minimum
                for field, minimum in (
                    ("anchor_invocation_id", 0),
                    ("enter_seq", 0),
                    ("exit_seq", 0),
                    ("represented_call_count", 1),
                    ("represented_participant_count", 1),
                )
            ) or fold["exit_seq"] < fold["enter_seq"]:
                raise ValueError(f"invalid UML graph fold counts: {diagram_id}")
            if not isinstance(fold.get("message_range"), str) or not fold["message_range"]:
                raise ValueError(f"invalid UML graph fold message range: {diagram_id}")
            if not all(
                isinstance(fold.get(field), str) and fold[field]
                for field in ("first_signature", "last_signature")
            ):
                raise ValueError(f"invalid UML graph fold signatures: {diagram_id}")

        if visible_units != visible_calls + sum(
            fold["kind"] == "SIBLING_BUNDLE" for fold in folds
        ):
            raise ValueError(f"inconsistent UML graph visible units: {diagram_id}")

        seen_links = set()
        for link in links:
            if not isinstance(link, dict):
                raise ValueError(f"invalid UML graph link: {diagram_id}")
            direction = link.get("direction")
            target_id = link.get("diagram_id")
            relation = link.get("relation")
            invocation_ids = link.get("invocation_ids")
            message_ids = link.get("message_ids")
            if (
                direction not in {"FROM", "TO", "PEER"}
                or not isinstance(target_id, str) or not target_id
                or relation not in {"EXPAND_CALL", "SIBLING_PEER"}
                or not isinstance(invocation_ids, list) or not invocation_ids
                or not all(isinstance(item, int) and item > 0 for item in invocation_ids)
                or not isinstance(message_ids, list)
                or len(message_ids) != len(invocation_ids)
                or not all(
                    isinstance(item, str) and re.fullmatch(message_id_pattern, item)
                    for item in message_ids
                )
            ):
                raise ValueError(f"invalid UML graph link: {diagram_id}")
            if (
                (direction == "PEER") != (relation == "SIBLING_PEER")
            ):
                raise ValueError(f"inconsistent UML graph link: {diagram_id}")
            key = (direction, target_id, relation, tuple(invocation_ids))
            if key in seen_links:
                raise ValueError(f"duplicate UML graph link: {diagram_id}")
            seen_links.add(key)

    if entry_id not in by_id:
        raise ValueError("UML graph entry node does not exist")
    for diagram_id, node in by_id.items():
        for link in node["links"]:
            target_id = link["diagram_id"]
            if target_id not in by_id:
                raise ValueError(f"invalid UML graph link target: {diagram_id}")
            if link["direction"] == "PEER":
                if not any(
                    candidate["direction"] == "PEER"
                    and candidate["relation"] == "SIBLING_PEER"
                    and candidate["diagram_id"] == diagram_id
                    for candidate in by_id[target_id]["links"]
                ):
                    raise ValueError(f"unpaired UML graph peer link: {diagram_id}")
                continue
            reverse = {
                "direction": "FROM" if link["direction"] == "TO" else "TO",
                "diagram_id": diagram_id,
                "relation": link["relation"],
                "invocation_ids": link["invocation_ids"],
                "message_ids": link["message_ids"],
            }
            if reverse not in by_id[target_id]["links"]:
                raise ValueError(f"unpaired UML graph link: {diagram_id}")
        for fold in node["folds"]:
            if fold["kind"] == "CALL_INTERNAL":
                if not any(
                    link["direction"] == "TO"
                    and link["diagram_id"] == fold["target_diagram_id"]
                    and link["relation"] == "EXPAND_CALL"
                    and link["invocation_ids"] == fold["invocation_ids"]
                    for link in node["links"]
                ):
                    raise ValueError(f"UML graph fold has no matching link: {diagram_id}")
            else:
                for peer_range in fold["peer_ranges"]:
                    if not any(
                        link["direction"] == "PEER"
                        and link["diagram_id"] == peer_range["diagram_id"]
                        and link["relation"] == "SIBLING_PEER"
                        and link["invocation_ids"] == peer_range["invocation_ids"]
                        for link in node["links"]
                    ):
                        raise ValueError(
                            f"sibling fold has no matching peer link: {diagram_id}"
                        )
        for link in node["links"]:
            if link["direction"] == "TO" and not any(
                fold["kind"] == "CALL_INTERNAL"
                and fold["target_diagram_id"] == link["diagram_id"]
                and link["relation"] == "EXPAND_CALL"
                and fold["invocation_ids"] == link["invocation_ids"]
                for fold in node["folds"]
            ):
                raise ValueError(f"UML graph link has no matching fold: {diagram_id}")

    sibling_groups: Dict[str, List[Dict[str, Any]]] = {}
    for node in nodes:
        if node.get("sibling_group") is not None:
            sibling_groups.setdefault(
                node["sibling_group"]["group_id"], []
            ).append(node)
    for group_id, members in sibling_groups.items():
        member_ids = {node["diagram_id"] for node in members}
        expected_count = len(members)
        ordinals = {node["sibling_group"]["ordinal"] for node in members}
        focus_ids = {node["focus_invocation_id"] for node in members}
        signatures = {node["entry_signature"] for node in members}
        if (
            expected_count <= 1
            or ordinals != set(range(1, expected_count + 1))
            or len(focus_ids) != 1
            or len(signatures) != 1
        ):
            raise ValueError(f"invalid UML graph sibling group: {group_id}")
        visible_ids = []
        for node in members:
            group = node["sibling_group"]
            if (
                group["count"] != expected_count
                or set(group["peer_diagram_ids"])
                != member_ids - {node["diagram_id"]}
            ):
                raise ValueError(f"incomplete UML graph sibling peers: {group_id}")
            visible_ids.extend(group["visible_invocation_ids"])
            peer_links = {
                link["diagram_id"]: link
                for link in node["links"] if link["direction"] == "PEER"
            }
            if set(peer_links) != member_ids - {node["diagram_id"]}:
                raise ValueError(f"incomplete UML graph peer links: {group_id}")
            for target_id, link in peer_links.items():
                if link["invocation_ids"] != by_id[target_id]["sibling_group"]["visible_invocation_ids"]:
                    raise ValueError(f"invalid UML graph peer range: {group_id}")
        if len(visible_ids) != len(set(visible_ids)):
            raise ValueError(f"overlapping UML graph sibling views: {group_id}")

    visited, pending = set(), [entry_id]
    while pending:
        diagram_id = pending.pop()
        if diagram_id in visited:
            continue
        visited.add(diagram_id)
        pending.extend(
            link["diagram_id"]
            for link in by_id[diagram_id]["links"]
            if link["direction"] in {"TO", "PEER"}
        )
    if visited != set(by_id):
        raise ValueError("unreachable UML graph nodes")
    if image_only:
        referenced_method_ids = {
            method_id for node in nodes for method_id in node["method_ids"]
        }
        if referenced_method_ids != set(catalog_by_id):
            raise ValueError("UML graph method catalog does not match visible methods")
    if value.get("node_count") != len(nodes) or value.get("diagram_count") != len(nodes):
        raise ValueError("inconsistent UML graph node count")
    source = value.get("source_call_count")
    trace_calls = value.get("trace_call_count")
    layout_root_calls = value.get("layout_root_call_count")
    partitioned = value.get("partitioned_call_count")
    excluded = value.get("excluded_call_count")
    if not all(
        isinstance(item, int) and item >= 0
        for item in (
            source, trace_calls, layout_root_calls, partitioned, excluded
        )
    ):
        raise ValueError("invalid UML graph call counts")
    if source != trace_calls + layout_root_calls:
        raise ValueError("inconsistent UML graph source call counts")
    if source != partitioned + excluded:
        raise ValueError("inconsistent UML graph call counts")
    if sum(int(node["represented_call_count"]) for node in nodes) != partitioned:
        raise ValueError("UML graph does not partition represented calls")
    if value["strategy"] == "synthetic-root-adaptive-graph" and (
        value["slice_applied"] or value.get("root_invocation_id") != 0
        or layout_root_calls <= 0
    ):
        raise ValueError("invalid synthetic-root UML graph")
    if value["strategy"] == "test-root-adaptive-graph" and (
        root_invocation_id <= 0
        or layout_root_calls != 0
        or by_id[entry_id]["focus_invocation_id"] != root_invocation_id
    ):
        raise ValueError("invalid test-root UML graph")
    if value["strategy"] == "synthetic-root-adaptive-graph" and (
        by_id[entry_id]["focus_invocation_id"] != 0
    ):
        raise ValueError("invalid synthetic-root UML graph entry")
    return value
