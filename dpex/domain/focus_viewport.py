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
    try:
        path_index = children.index(path_child_id)
    except ValueError:
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


def _stored_subgraph_call_count(
    topology: RefinementTraceTopology, root_id: int,
) -> int:
    """Count stored call nodes reachable from one boundary in a tree or DAG."""
    indexed_count = getattr(topology, "stored_subgraph_call_count", None)
    if callable(indexed_count):
        return int(indexed_count(root_id))
    visited: set[int] = set()
    pending = [root_id]
    call_count = 0
    while pending:
        invocation_id = pending.pop()
        if invocation_id in visited:
            continue
        visited.add(invocation_id)
        call_count += int(topology.has_call(invocation_id))
        pending.extend(topology.children(invocation_id))
    return call_count


def _stored_outer_context_sides(
    topology: RefinementTraceTopology, root_id: int,
) -> tuple[bool, bool]:
    """Detect stored calls before and after a degraded viewport boundary."""
    indexed_sides = getattr(topology, "stored_outer_context_sides", None)
    leading = trailing = False
    if callable(indexed_sides):
        leading, trailing = indexed_sides(root_id)

    # A reused canonical subtree can have an old source sequence number.  Walk
    # the representative caller path as well, so later sibling edges remain
    # visible as trailing context even when their nodes were interned earlier.
    current_id = root_id
    visited: set[int] = set()
    while current_id not in visited:
        visited.add(current_id)
        parent_id = int(topology.row(current_id)[ROW_PARENT_ID])
        if parent_id not in topology.row_positions:
            break
        children = topology.children(parent_id)
        try:
            position = children.index(current_id)
        except ValueError:
            break
        # Every retained topology node is either a call or contains one, so an
        # adjacent edge is sufficient evidence of omitted calls on that side.
        leading = leading or position > 0
        trailing = trailing or position + 1 < len(children)
        current_id = parent_id
    return bool(leading), bool(trailing)


def _omission(
    topology: RefinementTraceTopology,
    *,
    anchor_invocation_id: int,
    first_invocation_id: int,
    last_invocation_id: int,
    represented_call_count: int | None,
    scope: str,
) -> Dict[str, Any]:
    anchor = topology.invocation(anchor_invocation_id)
    value = {
        "kind": OMITTED_CALLS,
        "scope": scope,
        "anchor_invocation_id": anchor_invocation_id,
        "anchor_class": str(anchor["class"]),
        "first_invocation_id": first_invocation_id,
        "last_invocation_id": last_invocation_id,
        "enter_seq": _enter_seq(topology, first_invocation_id),
        "exit_seq": int(topology.row(last_invocation_id)[ROW_EXIT_SEQ]),
    }
    if represented_call_count is not None:
        value["represented_call_count"] = represented_call_count
    return value


def _apply_degraded_display_sequence(
    *,
    topology: RefinementTraceTopology,
    boundary_id: int,
    selected_ids: set[int],
    display_parents: dict[int, int],
    nodes: dict[int, Dict[str, Any]],
    folds: list[Dict[str, Any]],
) -> None:
    """Project canonical DAG nodes onto one well-nested local timeline.

    Degraded storage interns equal subgraphs.  The canonical node therefore
    keeps the sequence numbers of its first materialized occurrence, even when
    the viewport reaches it through a much later edge.  Those source sequence
    numbers remain untouched for audit; display_* fields describe only the
    selected viewport occurrence used by the renderer.
    """
    child_folds: dict[int, list[Dict[str, Any]]] = {}
    outer_folds: list[Dict[str, Any]] = []
    for fold in folds:
        if fold.get("kind") != OMITTED_CALLS:
            continue
        if fold.get("scope") == "CHILDREN":
            child_folds.setdefault(
                int(fold["anchor_invocation_id"]), []
            ).append(fold)
        elif fold.get("scope") == "OUTER_CONTEXT":
            outer_folds.append(fold)

    next_seq = 1

    def allocate() -> int:
        nonlocal next_seq
        value = next_seq
        next_seq += 1
        return value

    def set_fold_position(fold: Dict[str, Any]) -> None:
        position = allocate()
        fold["display_enter_seq"] = position
        fold["display_exit_seq"] = position

    def omission_ranges(
        parent_id: int,
    ) -> dict[int, tuple[int, Dict[str, Any]]]:
        children = topology.children(parent_id)
        ranges: dict[int, tuple[int, Dict[str, Any]]] = {}
        cursor = 0
        for fold in child_folds.get(parent_id, []):
            first_id = int(fold["first_invocation_id"])
            last_id = int(fold["last_invocation_id"])
            try:
                start = children.index(first_id, cursor)
                end = children.index(last_id, start) + 1
            except ValueError as error:
                raise ValueError(
                    "omitted child range is absent from display topology"
                ) from error
            ranges[start] = (end, fold)
            cursor = end
        return ranges

    visited: set[int] = set()

    def visit(invocation_id: int) -> None:
        if invocation_id in visited:
            return
        visited.add(invocation_id)
        node = nodes[invocation_id]
        enter_seq = allocate()
        node["invocation"]["display_enter_seq"] = enter_seq
        if node.get("call") is not None:
            node["call"]["display_enter_seq"] = enter_seq

        if invocation_id == boundary_id:
            for fold in outer_folds:
                set_fold_position(fold)

        children = topology.children(invocation_id)
        ranges = omission_ranges(invocation_id)
        position = 0
        while position < len(children):
            omitted = ranges.get(position)
            if omitted is not None:
                end, fold = omitted
                set_fold_position(fold)
                position = end
                continue
            child_id = children[position]
            if (
                child_id in selected_ids
                and display_parents.get(child_id) == invocation_id
            ):
                visit(child_id)
            position += 1

        exit_seq = allocate()
        node["invocation"]["display_exit_seq"] = exit_seq
        if node.get("call") is not None:
            node["call"]["display_exit_seq"] = exit_seq

    visit(boundary_id)
    if visited != selected_ids:
        raise ValueError("degraded display sequence is disconnected")

    for fold in folds:
        if fold.get("kind") != "REPEATED_SEQUENCE":
            continue
        first = nodes[int(fold["first_invocation_id"])]["invocation"]
        last = nodes[int(fold["last_invocation_id"])]["invocation"]
        fold["display_enter_seq"] = min(
            int(first["display_enter_seq"]),
            int(last["display_enter_seq"]),
        )
        fold["display_exit_seq"] = max(
            int(first["display_exit_seq"]),
            int(last["display_exit_seq"]),
        )


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
        # A degraded SQLite trace is a DAG: one canonical child subtree may be
        # referenced by several parents, while its stored parent_id names only
        # one representative caller.  Edge membership, rather than that single
        # representative pointer, therefore defines the visible children here.
        selected_children = [
            child_id for child_id in children if child_id in selected_ids
        ]
        selected_positions = []
        for child_id in selected_children:
            try:
                position = children.index(child_id)
            except ValueError:
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
                    represented_call_count=(
                        topology.child_range_count(parent_id, cursor, position)
                        if getattr(topology, "exact_omission_counts", True)
                        else None
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
                represented_call_count=(
                    topology.child_range_count(parent_id, cursor, len(children))
                    if getattr(topology, "exact_omission_counts", True)
                    else None
                ),
                scope="CHILDREN",
            ))

    cover(boundary_id)
    if visited != selected_ids:
        raise ValueError("selected focus context is disconnected")

    visible_call_count = sum(topology.has_call(item) for item in selected_ids)
    exact_counts = getattr(topology, "exact_omission_counts", True)
    child_omitted_count = (
        sum(int(item["represented_call_count"]) for item in folds)
        if exact_counts else None
    )
    outer_omitted_count = (
        int(topology.trace["call_count"]) - visible_call_count - child_omitted_count
        if child_omitted_count is not None else None
    )
    if outer_omitted_count is not None and outer_omitted_count < 0:
        raise ValueError("focus omission accounting exceeds trace call count")
    stored_count = int(
        getattr(topology, "stored_call_count", topology.trace["call_count"])
    )
    needs_outer_fold = (
        bool(outer_omitted_count)
        if exact_counts
        else _stored_subgraph_call_count(topology, boundary_id) < stored_count
    )
    if needs_outer_fold:
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
            if exact_counts:
                position = topology.calls_before(boundary_id)
                if topology.has_call(boundary_id):
                    leading = position
                else:
                    children = topology.children(boundary_id)
                    leading = (
                        topology.calls_before(children[0]) if children else 0
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
            else:
                leading, trailing = _stored_outer_context_sides(
                    topology, boundary_id
                )
                if leading or trailing:
                    outer_fold.update({
                        "boundary_context": True,
                        "context_invocation_id": parent_id,
                        "leading_calls_omitted": leading,
                        "trailing_calls_omitted": trailing,
                    })
        folds.append(outer_fold)

    # Give the selected portion of a canonical DAG one coherent display tree.
    # query_edges is authoritative; a shared node's persisted parent_id is only
    # the caller from its first materialized occurrence.
    display_parents: dict[int, int] = {}
    display_seen = {boundary_id}
    display_queue = [boundary_id]
    while display_queue:
        parent_id = display_queue.pop(0)
        for child_id in topology.children(parent_id):
            if child_id not in selected_ids or child_id in display_seen:
                continue
            display_seen.add(child_id)
            display_parents[child_id] = parent_id
            display_queue.append(child_id)
    if display_seen != selected_ids:
        raise ValueError("selected focus context is disconnected")

    def display_node(invocation_id: int) -> Dict[str, Any]:
        value = topology.node(invocation_id)
        parent_id = display_parents.get(invocation_id)
        if parent_id is None:
            return value
        invocation = dict(value["invocation"])
        invocation["parent_id"] = parent_id
        value = {**value, "invocation": invocation}
        call = value.get("call")
        if call is not None:
            parent = topology.invocation(parent_id)
            call = dict(call)
            call.update({
                "caller": f"{parent['class']}.{parent['method']}",
                "caller_class": parent["class"],
                "caller_method": parent["method"],
                "caller_descriptor": parent["descriptor"],
                "parent_invocation_id": parent_id,
            })
            value["call"] = call
        return value

    nodes = {item: display_node(item) for item in selected_ids}
    if hasattr(topology, "repetition_groups"):
        for parent_id in sorted(selected_ids):
            child_ids = topology.children(parent_id)
            for group in topology.repetition_groups(parent_id):
                start = int(group["start"])
                end = start + int(group["pattern_length"])
                pattern = child_ids[start:end]
                if pattern and all(child in selected_ids for child in pattern):
                    first, last = pattern[0], pattern[-1]
                    folds.append({
                        "kind": "REPEATED_SEQUENCE",
                        "anchor_invocation_id": parent_id,
                        "anchor_class": str(topology.invocation(parent_id)["class"]),
                        "first_invocation_id": first,
                        "last_invocation_id": last,
                        "enter_seq": _enter_seq(topology, first),
                        "exit_seq": int(topology.row(last)[ROW_EXIT_SEQ]),
                        "repeat_count": int(group["repeat_count"]),
                    })
    if not exact_counts:
        _apply_degraded_display_sequence(
            topology=topology,
            boundary_id=boundary_id,
            selected_ids=selected_ids,
            display_parents=display_parents,
            nodes=nodes,
            folds=folds,
        )
    for fold in folds:
        context_id = fold.get("context_invocation_id")
        if context_id is not None:
            nodes.setdefault(int(context_id), topology.node(int(context_id)))
            if not exact_counts:
                context = nodes[int(context_id)]
                context["invocation"]["display_enter_seq"] = 0
                context["invocation"]["display_exit_seq"] = max(
                    int(item["invocation"]["display_exit_seq"])
                    for invocation_id, item in nodes.items()
                    if invocation_id != int(context_id)
                ) + 1
    boundary = nodes[boundary_id]
    anchor = nodes[anchor_id]
    visible_items = sorted(
        (
            nodes[invocation_id] for invocation_id in selected_ids
            if invocation_id != boundary_id
        ),
        key=lambda item: (
            int(
                item["invocation"].get("display_enter_seq")
                or item["invocation"].get("enter_seq")
                or 0
            ),
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

    omission_folds = [item for item in folds if item["kind"] == OMITTED_CALLS]
    omitted_call_count = (
        sum(int(item["represented_call_count"]) for item in omission_folds)
        if exact_counts else None
    )
    if exact_counts and visible_call_count + omitted_call_count != topology.trace["call_count"]:
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
                int(item.get("display_enter_seq") or item["enter_seq"]),
                int(item["anchor_invocation_id"]),
            ),
        ),
        "links": [],
        "sibling_group": None,
        "visible_unit_count": len(selected_ids),
        "participant_classes": sorted(value for value in participants if value),
        "represented_call_count": (
            int(topology.trace["call_count"]) if exact_counts else None
        ),
        "visible_represented_call_count": visible_call_count,
        "omitted_call_count": omitted_call_count,
        "has_omitted_calls": bool(folds),
        "omitted_region_count": len(omission_folds),
        "suppress_boundary_caller": True,
        "upstream_visible_call_count": upstream_calls,
        "downstream_visible_call_count": downstream_calls,
        "internal_visible_call_count": len(internal_ids),
        "structural_context_call_count": len(structural_context_ids),
        "_numbered_items": nodes,
    }
