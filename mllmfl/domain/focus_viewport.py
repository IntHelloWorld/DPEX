from typing import Any, Dict, Sequence


OMITTED_CALLS = "OMITTED_CALLS"


def _invocation_id(item: Dict[str, Any]) -> int:
    return int(item["representative_invocation_id"])


def _enter_seq(item: Dict[str, Any]) -> int:
    return int(item["invocation"].get("enter_seq") or 0)


def _ordered_children(item: Dict[str, Any]) -> list[Dict[str, Any]]:
    return sorted(
        list(item.get("children") or []),
        key=lambda child: (_enter_seq(child), _invocation_id(child)),
    )


def _call_participants(item: Dict[str, Any]) -> set[str]:
    call = item.get("call") or {}
    caller = str(
        call.get("caller_class")
        or (
            str(call.get("caller") or "").rsplit(".", 1)[0]
            if call.get("caller")
            else ""
        )
    )
    callee = str(
        call.get("callee_class")
        or item["invocation"].get("class")
        or ""
    )
    return {value for value in (caller, callee) if value}


def index_compressed_execution(
    compressed: Dict[str, Any],
) -> tuple[Dict[int, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    by_representative: Dict[int, Dict[str, Any]] = {}
    by_occurrence: Dict[int, Dict[str, Any]] = {}

    def visit(item: Dict[str, Any]) -> None:
        representative = _invocation_id(item)
        by_representative[representative] = item
        for invocation_id in item.get("occurrence_invocation_ids") or [representative]:
            by_occurrence[int(invocation_id)] = item
        for child in item.get("children") or []:
            visit(child)

    for group in compressed.get("root_groups") or []:
        for item in group.get("calls") or []:
            visit(item)
    return by_representative, by_occurrence


def _atomic_groups(
    children: Sequence[Dict[str, Any]],
) -> list[list[Dict[str, Any]]]:
    """Return execution-ordered calls without splitting compressed repeats."""
    ordered = sorted(
        list(children), key=lambda item: (_enter_seq(item), _invocation_id(item))
    )
    groups: list[list[Dict[str, Any]]] = []
    cursor = 0
    while cursor < len(ordered):
        item = ordered[cursor]
        sequence = item.get("repeat_sequence")
        if sequence is None:
            groups.append([item])
            cursor += 1
            continue
        length = int(sequence.get("pattern_length") or 0)
        sequence_id = str(sequence.get("sequence_id") or "")
        if length <= 1 or int(sequence.get("position") or 0) != 1:
            raise ValueError("invalid repeated sequence boundary")
        group = ordered[cursor:cursor + length]
        if len(group) != length or any(
            str((value.get("repeat_sequence") or {}).get("sequence_id") or "")
            != sequence_id
            or int((value.get("repeat_sequence") or {}).get("pattern_length") or 0)
            != length
            or int((value.get("repeat_sequence") or {}).get("position") or 0)
            != position
            or int((value.get("repeat_sequence") or {}).get("repeat_count") or 0)
            != int(sequence.get("repeat_count") or 0)
            for position, value in enumerate(group, 1)
        ):
            raise ValueError("incomplete repeated sequence")
        groups.append(group)
        cursor += length
    return groups


def _select_nearest_groups(
    groups: Sequence[Sequence[Dict[str, Any]]],
    maximum: int,
    *,
    reverse: bool,
) -> tuple[list[list[Dict[str, Any]]], bool]:
    """Select one contiguous focus-nearest window without splitting repeats."""
    selected: list[list[Dict[str, Any]]] = []
    used = 0
    ordered = list(reversed(groups)) if reverse else list(groups)
    for raw_group in ordered:
        group = list(raw_group)
        if used + len(group) > maximum:
            return selected, False
        selected.append(group)
        used += len(group)
    return selected, True


def _contains_occurrence(item: Dict[str, Any], invocation_id: int) -> bool:
    return invocation_id in {
        int(value)
        for value in item.get("occurrence_invocation_ids")
        or [_invocation_id(item)]
    }


def _siblings_around_path(
    parent: Dict[str, Any],
    path_child_invocation_id: int,
    *,
    max_upstream_calls: int,
    max_downstream_calls: int,
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]], bool, bool]:
    groups = _atomic_groups(parent.get("children") or [])
    path_group_index = next((
        index
        for index, group in enumerate(groups)
        if any(
            _contains_occurrence(child, path_child_invocation_id)
            for child in group
        )
    ), None)
    if path_group_index is None:
        raise ValueError("caller-path child is absent from compressed siblings")
    path_group = groups[path_group_index]
    if len(path_group) != 1 or path_group[0].get("repeat_sequence") is not None:
        raise ValueError("protected caller-path child was compressed into a sequence")

    prefix, prefix_exhausted = _select_nearest_groups(
        groups[:path_group_index], max_upstream_calls, reverse=True
    )
    suffix, suffix_exhausted = _select_nearest_groups(
        groups[path_group_index + 1:], max_downstream_calls, reverse=False
    )
    return (
        [item for group in reversed(prefix) for item in group],
        [item for group in suffix for item in group],
        prefix_exhausted,
        suffix_exhausted,
    )


def _internal_bfs_groups(
    focus: Dict[str, Any],
) -> list[list[Dict[str, Any]]]:
    """Return focus descendants level by level and left to right."""
    result: list[list[Dict[str, Any]]] = []
    parents = [focus]
    while parents:
        next_parents: list[Dict[str, Any]] = []
        for parent in parents:
            for group in _atomic_groups(parent.get("children") or []):
                result.append(group)
                next_parents.extend(group)
        parents = next_parents
    return result


def plan_focus_viewport(
    *,
    diagram_id: str,
    focus_invocation_id: int,
    execution: Dict[str, Any],
    compressed: Dict[str, Any],
    max_upstream_calls: int,
    max_downstream_calls: int,
    max_internal_calls: int,
) -> Dict[str, Any]:
    """Plan one focus-centered image with lossless omission accounting."""
    for name, value in (
        ("max_upstream_calls", max_upstream_calls),
        ("max_downstream_calls", max_downstream_calls),
        ("max_internal_calls", max_internal_calls),
    ):
        if value < 1:
            raise ValueError(f"{name} must be positive")

    invocations = {
        int(item["invocation_id"]): item for item in execution["invocations"]
    }
    by_representative, by_occurrence = index_compressed_execution(compressed)
    root_contexts: list[Dict[str, Any]] = []
    for group in compressed.get("root_groups") or []:
        parent_id = int(group["parent_invocation_id"])
        if parent_id not in invocations or parent_id in by_occurrence:
            continue
        invocation = invocations[parent_id]
        children = list(group.get("calls") or [])
        participants = {str(invocation.get("class") or "")}
        for child in children:
            participants.update(str(value) for value in child["participant_classes"])
        context_node = {
            "representative_invocation_id": parent_id,
            "call": None,
            "invocation": invocation,
            "repeat_count": 1,
            "occurrence_invocation_ids": [parent_id],
            "represented_call_count": sum(
                int(child["represented_call_count"]) for child in children
            ),
            "displayed_subtree_call_count": sum(
                int(child["displayed_subtree_call_count"]) for child in children
            ),
            "participant_classes": sorted(value for value in participants if value),
            "children": children,
        }
        by_representative[parent_id] = context_node
        by_occurrence[parent_id] = context_node
        root_contexts.append(context_node)

    anchor_id = int(focus_invocation_id)
    anchor_node = by_occurrence.get(anchor_id)
    if anchor_id not in invocations or anchor_node is None:
        raise ValueError(f"focus invocation is unavailable: {anchor_id}")

    selected: Dict[int, Dict[str, Any]] = {
        _invocation_id(anchor_node): anchor_node
    }
    structural_context_ids: set[int] = set()
    upstream_calls = 0
    downstream_calls = 0

    remaining_upstream = max_upstream_calls
    remaining_downstream = max_downstream_calls
    current_invocation = invocations[anchor_id]
    boundary = anchor_node
    while (
        remaining_upstream > 0
        and current_invocation.get("parent_id") in invocations
    ):
        parent_id = int(current_invocation["parent_id"])
        parent_node = by_occurrence.get(parent_id)
        if parent_node is None:
            raise ValueError("focus caller is absent from compressed execution")
        parent_rep = _invocation_id(parent_node)
        selected[parent_rep] = parent_node
        structural_context_ids.add(parent_rep)
        boundary = parent_node
        upstream_calls += 1
        remaining_upstream -= 1

        (
            prefix,
            suffix,
            prefix_exhausted,
            suffix_exhausted,
        ) = _siblings_around_path(
            parent_node,
            int(current_invocation["invocation_id"]),
            max_upstream_calls=remaining_upstream,
            max_downstream_calls=remaining_downstream,
        )
        for item in (*prefix, *suffix):
            selected[_invocation_id(item)] = item
        upstream_calls += len(prefix)
        downstream_calls += len(suffix)
        remaining_upstream -= len(prefix)
        remaining_downstream -= len(suffix)
        if not prefix_exhausted:
            remaining_upstream = 0
        if not suffix_exhausted:
            remaining_downstream = 0
        current_invocation = invocations[parent_id]

    internal_groups, _ = _select_nearest_groups(
        _internal_bfs_groups(anchor_node),
        max_internal_calls,
        reverse=False,
    )
    direct_internal = [item for group in internal_groups for item in group]
    for item in direct_internal:
        selected[_invocation_id(item)] = item

    selected_ids = set(selected)
    folds: list[Dict[str, Any]] = []
    child_coverage: list[Dict[str, Any]] = []
    visited: set[int] = set()

    def cover(parent: Dict[str, Any], parent_multiplier: int) -> None:
        parent_rep = _invocation_id(parent)
        if parent_rep in visited:
            return
        visited.add(parent_rep)
        children = _ordered_children(parent)
        visible_ids: list[int] = []
        omitted_ranges: list[list[int]] = []
        hidden: list[Dict[str, Any]] = []

        def flush_hidden() -> None:
            if not hidden:
                return
            hidden_ids = [_invocation_id(item) for item in hidden]
            omitted_ranges.append(hidden_ids)
            folds.append({
                "kind": OMITTED_CALLS,
                "anchor_invocation_id": parent_rep,
                "invocation_ids": hidden_ids,
                "enter_seq": min(_enter_seq(item) for item in hidden),
                "exit_seq": max(
                    int(item["invocation"].get("exit_seq") or 0)
                    for item in hidden
                ),
                "represented_call_count": parent_multiplier * sum(
                    int(item["represented_call_count"]) for item in hidden
                ),
                "represented_participant_count": len({
                    value
                    for item in hidden
                    for value in item["participant_classes"]
                }),
            })
            hidden.clear()

        for child in children:
            child_rep = _invocation_id(child)
            if child_rep not in selected_ids:
                hidden.append(child)
                continue
            flush_hidden()
            visible_ids.append(child_rep)
            cover(
                child,
                parent_multiplier * int(child.get("repeat_count") or 1),
            )
        flush_hidden()
        expected = [_invocation_id(child) for child in children]
        covered = [
            *visible_ids,
            *(value for values in omitted_ranges for value in values),
        ]
        if len(covered) != len(set(covered)) or set(covered) != set(expected):
            raise ValueError(
                f"semantic child coverage is incomplete for invocation {parent_rep}"
            )
        child_coverage.append({
            "parent_invocation_id": parent_rep,
            "child_invocation_ids": expected,
            "visible_child_invocation_ids": visible_ids,
            "omitted_child_ranges": omitted_ranges,
            "omitted_call_count": sum(
                fold["represented_call_count"]
                for fold in folds
                if fold["anchor_invocation_id"] == parent_rep
            ),
        })

    cover(boundary, int(boundary.get("repeat_count") or 1))
    if visited != selected_ids:
        raise ValueError("selected focus context is disconnected")

    boundary_rep = _invocation_id(boundary)
    top_invocation = invocations[anchor_id]
    while top_invocation.get("parent_id") in invocations:
        top_invocation = invocations[int(top_invocation["parent_id"])]
    main_root = by_occurrence.get(int(top_invocation["invocation_id"]))
    if main_root is None:
        raise ValueError("focus execution root is absent from compressed execution")
    main_root_rep = _invocation_id(main_root)

    outside_markers: list[Dict[str, Any]] = []
    outer_context_id: int | None = None
    boundary_invocation = boundary["invocation"]
    if boundary_invocation.get("parent_id") in invocations:
        outer_parent = by_occurrence.get(int(boundary_invocation["parent_id"]))
        if outer_parent is None:
            raise ValueError("truncated focus caller is absent from compressed execution")
        outside_markers.append(outer_parent)
        outer_context_id = _invocation_id(outer_parent)
    for root_context in root_contexts:
        root_rep = _invocation_id(root_context)
        if root_rep == main_root_rep:
            continue
        children = _ordered_children(root_context)
        child_ids = [_invocation_id(child) for child in children]
        outside_markers.extend(children)
        child_coverage.append({
            "parent_invocation_id": root_rep,
            "child_invocation_ids": child_ids,
            "visible_child_invocation_ids": [],
            "omitted_child_ranges": [child_ids] if child_ids else [],
            "omitted_call_count": sum(
                int(child["represented_call_count"]) for child in children
            ),
        })
    outside_markers.sort(
        key=lambda item: (_enter_seq(item), _invocation_id(item))
    )
    represented_call_count = int(compressed.get("represented_call_count") or 0)
    boundary_call_count = int(boundary.get("represented_call_count") or 0)
    outside_call_count = represented_call_count - boundary_call_count
    if outside_call_count < 0:
        raise ValueError("focus boundary exceeds represented execution")
    if outside_call_count and not outside_markers:
        raise ValueError("omitted outer focus context has no marker")
    boundary_subtree_ids: set[int] = set()
    raw_children: Dict[int, list[int]] = {}
    for invocation in invocations.values():
        parent_id = invocation.get("parent_id")
        if parent_id in invocations:
            raw_children.setdefault(int(parent_id), []).append(
                int(invocation["invocation_id"])
            )
    pending_ids = [int(boundary_invocation["invocation_id"])]
    while pending_ids:
        invocation_id = pending_ids.pop()
        if invocation_id in boundary_subtree_ids:
            continue
        boundary_subtree_ids.add(invocation_id)
        pending_ids.extend(raw_children.get(invocation_id, []))
    boundary_enter_seq = int(boundary_invocation.get("enter_seq") or 0)
    leading_outside_call_count = 0
    trailing_outside_call_count = 0
    for call in execution["calls"]:
        invocation_id = int(call["invocation_id"])
        if invocation_id in boundary_subtree_ids:
            continue
        count = int(call.get("count") or 1)
        if int(call.get("enter_seq") or 0) < boundary_enter_seq:
            leading_outside_call_count += count
        else:
            trailing_outside_call_count += count
    if (
        leading_outside_call_count + trailing_outside_call_count
        != outside_call_count
    ):
        raise ValueError("outer focus context counts do not partition execution")
    if outside_call_count:
        folds.append({
            "kind": OMITTED_CALLS,
            "boundary_context": outer_context_id is not None,
            "context_invocation_id": outer_context_id,
            "leading_call_count": leading_outside_call_count,
            "trailing_call_count": trailing_outside_call_count,
            "anchor_invocation_id": boundary_rep,
            "invocation_ids": [
                _invocation_id(item) for item in outside_markers
            ],
            "enter_seq": min(
                _enter_seq(item) for item in outside_markers
            ),
            "exit_seq": max(
                int(item["invocation"].get("exit_seq") or 0)
                for item in outside_markers
            ),
            "represented_call_count": outside_call_count,
            "represented_participant_count": len({
                value
                for item in outside_markers
                for value in item["participant_classes"]
            }),
        })

    visible_items = sorted(
        (
            item for invocation_id, item in selected.items()
            if invocation_id != _invocation_id(boundary)
        ),
        key=lambda item: (_enter_seq(item), _invocation_id(item)),
    )
    participants: set[str] = {
        str(boundary["invocation"].get("class") or "")
    }
    if outer_context_id is not None:
        participants.add(
            str(
                by_representative[outer_context_id]["invocation"].get("class")
                or ""
            )
        )
    for item in selected.values():
        participants.update(_call_participants(item))

    numbered_ids = set(selected)
    for fold in folds:
        numbered_ids.add(int(fold["anchor_invocation_id"]))
        numbered_ids.update(int(value) for value in fold["invocation_ids"])
    omitted_call_count = sum(
        int(fold["represented_call_count"]) for fold in folds
    )
    accounted_call_count = boundary_call_count + outside_call_count
    if accounted_call_count != represented_call_count:
        raise ValueError("focus viewport does not account for every trace root")
    if omitted_call_count > represented_call_count:
        raise ValueError("omitted calls exceed the represented focus execution")
    if (
        upstream_calls > max_upstream_calls
        or downstream_calls > max_downstream_calls
        or len(direct_internal) > max_internal_calls
        or len(selected) > (
            1
            + max_upstream_calls
            + max_downstream_calls
            + max_internal_calls
        )
    ):
        raise ValueError("focus viewport exceeds its configured call windows")

    return {
        "diagram_id": diagram_id,
        "focus": boundary,
        "selected_focus": anchor_node,
        "visible_roots": [],
        "visible_items": visible_items,
        "folds": sorted(
            folds,
            key=lambda item: (
                int(item["enter_seq"]), int(item["anchor_invocation_id"])
            ),
        ),
        "links": [],
        "sibling_group": None,
        "visible_unit_count": len(selected),
        "participant_classes": sorted(value for value in participants if value),
        "represented_call_count": represented_call_count,
        "visible_represented_call_count": represented_call_count - omitted_call_count,
        "omitted_call_count": omitted_call_count,
        "suppress_boundary_caller": True,
        "upstream_visible_call_count": upstream_calls,
        "downstream_visible_call_count": downstream_calls,
        "internal_visible_call_count": len(direct_internal),
        "structural_context_call_count": len(structural_context_ids),
        "_child_coverage": sorted(
            child_coverage, key=lambda item: int(item["parent_invocation_id"])
        ),
        "_numbered_items": {
            invocation_id: by_representative[invocation_id]
            for invocation_id in numbered_ids
        },
    }
