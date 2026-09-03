from bisect import bisect_left
from typing import Any, Dict

from .refinement_trace import (
    ROW_ENTER_SEQ,
    ROW_EXIT_SEQ,
    ROW_PARENT_ID,
    RefinementTraceTopology,
)


OMITTED_CALLS = "OMITTED_CALLS"


def _enter_seq(topology: RefinementTraceTopology, invocation_id: int) -> int:
    return int(topology.row(invocation_id)[ROW_ENTER_SEQ])


def _siblings_around_path(
    topology: RefinementTraceTopology,
    parent_id: int,
    path_child_id: int,
    *,
    max_upstream_calls: int,
    max_downstream_calls: int,
) -> tuple[list[int], list[int], bool, bool]:
    children = topology.children(parent_id)
    path_index = bisect_left(children, path_child_id)
    if path_index >= len(children) or children[path_index] != path_child_id:
        raise ValueError("caller-path child is absent from execution siblings")
    prefix_start = max(0, path_index - max_upstream_calls)
    suffix_end = min(
        len(children), path_index + 1 + max_downstream_calls
    )
    prefix = list(children[prefix_start:path_index])
    suffix = list(children[path_index + 1:suffix_end])
    prefix_exhausted = prefix_start == 0
    suffix_exhausted = suffix_end == len(children)
    return prefix, suffix, prefix_exhausted, suffix_exhausted


def _internal_bfs(
    topology: RefinementTraceTopology, focus_id: int, maximum: int,
) -> list[int]:
    selected: list[int] = []
    current_level: tuple[int, ...] = topology.children(focus_id)
    while current_level and len(selected) < maximum:
        remaining = maximum - len(selected)
        chosen = current_level[:remaining]
        selected.extend(chosen)
        if len(chosen) < len(current_level):
            break
        next_level: list[int] = []
        next_limit = maximum - len(selected)
        for invocation_id in chosen:
            if len(next_level) >= next_limit:
                break
            children = topology.children(invocation_id)
            next_level.extend(children[:next_limit - len(next_level)])
        current_level = tuple(next_level)
    return selected


def _omission(
    topology: RefinementTraceTopology,
    *,
    anchor_invocation_id: int,
    first_invocation_id: int,
    last_invocation_id: int,
    represented_call_count: int,
    scope: str,
) -> Dict[str, Any]:
    anchor = topology.invocation(anchor_invocation_id)
    return {
        "kind": OMITTED_CALLS,
        "scope": scope,
        "anchor_invocation_id": anchor_invocation_id,
        "anchor_class": str(anchor["class"]),
        "first_invocation_id": first_invocation_id,
        "last_invocation_id": last_invocation_id,
        "enter_seq": _enter_seq(topology, first_invocation_id),
        "exit_seq": int(topology.row(last_invocation_id)[ROW_EXIT_SEQ]),
        "represented_call_count": represented_call_count,
    }


def plan_focus_viewport(
    *,
    diagram_id: str,
    focus_invocation_id: int,
    topology: RefinementTraceTopology,
    max_upstream_calls: int,
    max_downstream_calls: int,
    max_internal_calls: int,
) -> Dict[str, Any]:
    """Plan a bounded viewport using only precomputed topology/count indexes."""
    for name, value in (
        ("max_upstream_calls", max_upstream_calls),
        ("max_downstream_calls", max_downstream_calls),
        ("max_internal_calls", max_internal_calls),
    ):
        if value < 1:
            raise ValueError(f"{name} must be positive")
    anchor_id = int(focus_invocation_id)
    if not topology.has_call(anchor_id):
        raise ValueError(f"focus invocation is unavailable: {anchor_id}")

    selected_ids = {anchor_id}
    structural_context_ids: set[int] = set()
    upstream_calls = 0
    downstream_calls = 0
    remaining_upstream = max_upstream_calls
    remaining_downstream = max_downstream_calls
    current_id = anchor_id
    boundary_id = anchor_id

    while remaining_upstream > 0:
        parent_id = int(topology.row(current_id)[ROW_PARENT_ID])
        if parent_id not in topology.row_positions:
            break
        selected_ids.add(parent_id)
        structural_context_ids.add(parent_id)
        boundary_id = parent_id
        upstream_calls += 1
        remaining_upstream -= 1
        prefix, suffix, prefix_exhausted, suffix_exhausted = (
            _siblings_around_path(
                topology,
                parent_id,
                current_id,
                max_upstream_calls=remaining_upstream,
                max_downstream_calls=remaining_downstream,
            )
        )
        selected_ids.update(prefix)
        selected_ids.update(suffix)
        upstream_calls += len(prefix)
        downstream_calls += len(suffix)
        remaining_upstream -= len(prefix)
        remaining_downstream -= len(suffix)
        if not prefix_exhausted:
            remaining_upstream = 0
        if not suffix_exhausted:
            remaining_downstream = 0
        current_id = parent_id

    internal_ids = _internal_bfs(topology, anchor_id, max_internal_calls)
    selected_ids.update(internal_ids)

    folds: list[Dict[str, Any]] = []
    visited: set[int] = set()

    def cover(parent_id: int) -> None:
        if parent_id in visited:
            return
        visited.add(parent_id)
        children = topology.children(parent_id)
        selected_children = sorted(
            (
                child_id for child_id in selected_ids
                if int(topology.row(child_id)[ROW_PARENT_ID]) == parent_id
            ),
            key=lambda child_id: _enter_seq(topology, child_id),
        )
        selected_positions = []
        for child_id in selected_children:
            position = bisect_left(children, child_id)
            if position >= len(children) or children[position] != child_id:
                raise ValueError("selected child is absent from topology")
            selected_positions.append(position)
        cursor = 0
        for position, child_id in zip(selected_positions, selected_children):
            if cursor < position:
                folds.append(_omission(
                    topology,
                    anchor_invocation_id=parent_id,
                    first_invocation_id=children[cursor],
                    last_invocation_id=children[position - 1],
                    represented_call_count=topology.child_range_count(
                        parent_id, cursor, position
                    ),
                    scope="CHILDREN",
                ))
            cover(child_id)
            cursor = position + 1
        if cursor < len(children):
            folds.append(_omission(
                topology,
                anchor_invocation_id=parent_id,
                first_invocation_id=children[cursor],
                last_invocation_id=children[-1],
                represented_call_count=topology.child_range_count(
                    parent_id, cursor, len(children)
                ),
                scope="CHILDREN",
            ))

    cover(boundary_id)
    if visited != selected_ids:
        raise ValueError("selected focus context is disconnected")

    visible_call_count = sum(topology.has_call(item) for item in selected_ids)
    child_omitted_count = sum(
        int(item["represented_call_count"]) for item in folds
    )
    outer_omitted_count = (
        int(topology.trace["call_count"])
        - visible_call_count
        - child_omitted_count
    )
    if outer_omitted_count < 0:
        raise ValueError("focus omission accounting exceeds trace call count")
    if outer_omitted_count:
        outer_fold = _omission(
            topology,
            anchor_invocation_id=boundary_id,
            first_invocation_id=boundary_id,
            last_invocation_id=boundary_id,
            represented_call_count=outer_omitted_count,
            scope="OUTER_CONTEXT",
        )
        parent_id = int(topology.row(boundary_id)[ROW_PARENT_ID])
        if parent_id in topology.row_positions:
            position = bisect_left(topology.call_ids, boundary_id)
            if topology.has_call(boundary_id):
                leading = position
            else:
                children = topology.children(boundary_id)
                leading = (
                    bisect_left(topology.call_ids, children[0]) if children else 0
                )
            trailing = (
                int(topology.trace["call_count"])
                - leading
                - topology.subtree_call_count(boundary_id)
            )
            if leading + trailing == outer_omitted_count:
                outer_fold.update({
                    "boundary_context": True,
                    "context_invocation_id": parent_id,
                    "leading_call_count": leading,
                    "trailing_call_count": trailing,
                })
        folds.append(outer_fold)

    nodes = {item: topology.node(item) for item in selected_ids}
    for fold in folds:
        context_id = fold.get("context_invocation_id")
        if context_id is not None:
            nodes.setdefault(int(context_id), topology.node(int(context_id)))
    boundary = nodes[boundary_id]
    anchor = nodes[anchor_id]
    visible_items = sorted(
        (
            nodes[invocation_id] for invocation_id in selected_ids
            if invocation_id != boundary_id
        ),
        key=lambda item: (
            int(item["invocation"].get("enter_seq") or 0),
            int(item["representative_invocation_id"]),
        ),
    )
    participants = {
        value for item in nodes.values() for value in item["participant_classes"]
    }
    if (
        upstream_calls > max_upstream_calls
        or downstream_calls > max_downstream_calls
        or len(internal_ids) > max_internal_calls
        or len(selected_ids) > (
            1 + max_upstream_calls + max_downstream_calls + max_internal_calls
        )
    ):
        raise ValueError("focus viewport exceeds its configured call windows")

    omitted_call_count = sum(
        int(item["represented_call_count"]) for item in folds
    )
    if visible_call_count + omitted_call_count != topology.trace["call_count"]:
        raise ValueError("focus viewport does not account for every trace call")
    return {
        "diagram_id": diagram_id,
        "focus": boundary,
        "selected_focus": anchor,
        "visible_roots": [],
        "visible_items": visible_items,
        "folds": sorted(
            folds,
            key=lambda item: (
                int(item["enter_seq"]), int(item["anchor_invocation_id"]),
            ),
        ),
        "links": [],
        "sibling_group": None,
        "visible_unit_count": len(selected_ids),
        "participant_classes": sorted(value for value in participants if value),
        "represented_call_count": int(topology.trace["call_count"]),
        "visible_represented_call_count": visible_call_count,
        "omitted_call_count": omitted_call_count,
        "has_omitted_calls": bool(folds),
        "omitted_region_count": len(folds),
        "suppress_boundary_caller": True,
        "upstream_visible_call_count": upstream_calls,
        "downstream_visible_call_count": downstream_calls,
        "internal_visible_call_count": len(internal_ids),
        "structural_context_call_count": len(structural_context_ids),
        "_numbered_items": nodes,
    }
