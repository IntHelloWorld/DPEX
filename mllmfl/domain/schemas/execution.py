from typing import Any, Dict


def validate_compressed_execution(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("compressed execution must be a JSON object")
    schema_version = value.get("schema_version")
    if (
        value.get("schema") != "fullchain-compressed-execution"
        or schema_version not in {1, 2, 3}
        or value.get("source_schema") != "fullchain-execution"
    ):
        raise ValueError("unsupported compressed execution schema")
    source_count = value.get("source_call_count")
    represented = value.get("represented_call_count")
    displayed = value.get("displayed_call_count")
    if not all(isinstance(item, int) and item >= 0 for item in (
        source_count, represented, displayed
    )) or represented != source_count or displayed > represented:
        raise ValueError("invalid compressed execution call counts")
    groups = value.get("root_groups")
    if not isinstance(groups, list):
        raise ValueError("compressed execution root_groups must be an array")
    value_capture_enabled = False
    if schema_version == 3:
        if not isinstance(value.get("value_capture_enabled"), bool):
            raise ValueError("invalid compressed execution value capture metadata")
        value_capture_enabled = value["value_capture_enabled"]
    seen_representatives = set()
    seen_occurrence_roots = set()
    seen_sequence_ids = set()

    def validate_node(node: Any) -> tuple[int, int, set[str]]:
        if not isinstance(node, dict):
            raise ValueError("invalid compressed call node")
        invocation_id = node.get("representative_invocation_id")
        if not isinstance(invocation_id, int) or invocation_id <= 0:
            raise ValueError("invalid compressed representative invocation")
        if invocation_id in seen_representatives:
            raise ValueError(f"duplicate compressed representative: {invocation_id}")
        seen_representatives.add(invocation_id)
        if not isinstance(node.get("call"), dict) or not isinstance(
            node.get("invocation"), dict
        ):
            raise ValueError(f"invalid compressed call records: {invocation_id}")
        repeat_count = node.get("repeat_count")
        represented_count = node.get("represented_call_count")
        displayed_count = node.get("displayed_subtree_call_count")
        if not isinstance(repeat_count, int) or repeat_count <= 0:
            raise ValueError(f"invalid compressed repeat count: {invocation_id}")
        if value_capture_enabled and repeat_count != 1:
            raise ValueError(f"captured values cannot be repeated: {invocation_id}")
        occurrences = node.get("occurrence_invocation_ids")
        if schema_version >= 2 and (
            not isinstance(occurrences, list)
            or len(occurrences) != repeat_count
            or not all(isinstance(item, int) and item > 0 for item in occurrences)
            or len(occurrences) != len(set(occurrences))
            or occurrences[0] != invocation_id
            or any(item in seen_occurrence_roots for item in occurrences)
        ):
            raise ValueError(f"invalid compressed occurrences: {invocation_id}")
        if schema_version >= 2:
            seen_occurrence_roots.update(occurrences)
        if not isinstance(represented_count, int) or represented_count <= 0:
            raise ValueError(f"invalid compressed represented count: {invocation_id}")
        if not isinstance(displayed_count, int) or displayed_count <= 0:
            raise ValueError(f"invalid compressed displayed count: {invocation_id}")
        participants = node.get("participant_classes")
        if not isinstance(participants, list) or not participants or not all(
            isinstance(item, str) and item for item in participants
        ) or participants != sorted(set(participants)):
            raise ValueError(f"invalid compressed participants: {invocation_id}")
        fingerprint = node.get("subtree_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError(f"invalid compressed fingerprint: {invocation_id}")
        children = node.get("children")
        if not isinstance(children, list):
            raise ValueError(f"invalid compressed children: {invocation_id}")
        child_values = [validate_node(child) for child in children]
        expected_displayed = 1 + sum(item[1] for item in child_values)
        if displayed_count != expected_displayed:
            raise ValueError(f"inconsistent compressed displayed count: {invocation_id}")
        child_participants = set().union(*(item[2] for item in child_values)) if child_values else set()
        if not child_participants.issubset(set(participants)):
            raise ValueError(f"inconsistent compressed participants: {invocation_id}")
        expected_represented = repeat_count * (
            1 + sum(item[0] for item in child_values)
        )
        if (
            schema_version >= 2 and represented_count != expected_represented
        ) or (
            schema_version == 1 and represented_count < expected_represented
        ):
            raise ValueError(f"inconsistent compressed represented count: {invocation_id}")
        validate_sequence_list(children)
        return represented_count, displayed_count, set(participants)

    def validate_sequence_list(nodes: Any) -> None:
        index = 0
        while index < len(nodes):
            sequence = nodes[index].get("repeat_sequence")
            if sequence is None:
                index += 1
                continue
            if value_capture_enabled:
                raise ValueError("captured values cannot use repeat sequences")
            if not isinstance(sequence, dict):
                raise ValueError("invalid compressed repeat sequence")
            sequence_id = sequence.get("sequence_id")
            pattern_length = sequence.get("pattern_length")
            repeat_count = sequence.get("repeat_count")
            if (
                not isinstance(sequence_id, str) or not sequence_id
                or sequence_id in seen_sequence_ids
                or not isinstance(pattern_length, int) or pattern_length <= 1
                or not isinstance(repeat_count, int) or repeat_count <= 1
                or sequence.get("position") != 1
            ):
                raise ValueError("invalid compressed repeat sequence start")
            pattern = nodes[index:index + pattern_length]
            if len(pattern) != pattern_length or any(
                not isinstance(item.get("repeat_sequence"), dict)
                or item["repeat_sequence"].get("sequence_id") != sequence_id
                or item["repeat_sequence"].get("pattern_length") != pattern_length
                or item["repeat_sequence"].get("repeat_count") != repeat_count
                or item["repeat_sequence"].get("position") != position
                or item.get("repeat_count") != repeat_count
                for position, item in enumerate(pattern, 1)
            ):
                raise ValueError(f"incomplete compressed repeat sequence: {sequence_id}")
            seen_sequence_ids.add(sequence_id)
            index += pattern_length

    totals = []
    parent_ids = set()
    for group in groups:
        if not isinstance(group, dict) or not isinstance(
            group.get("parent_invocation_id"), int
        ) or not isinstance(group.get("calls"), list):
            raise ValueError("invalid compressed root group")
        parent_id = group["parent_invocation_id"]
        if parent_id in parent_ids:
            raise ValueError(f"duplicate compressed root parent: {parent_id}")
        parent_ids.add(parent_id)
        validate_sequence_list(group["calls"])
        totals.extend(validate_node(node) for node in group["calls"])
    if sum(item[0] for item in totals) != represented:
        raise ValueError("compressed root coverage does not match source calls")
    if sum(item[1] for item in totals) != displayed:
        raise ValueError("compressed root display count is inconsistent")
    if value_capture_enabled and displayed != represented:
        raise ValueError("captured values must remain fully expanded")
    return value
