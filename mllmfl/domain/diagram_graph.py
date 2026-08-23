from collections import deque
from typing import Any, Deque, Dict, List, Sequence


EXPAND_CALL = "EXPAND_CALL"
SIBLING_PEER = "SIBLING_PEER"


def _invocation_id(item: Dict[str, Any]) -> int:
    return int(item["representative_invocation_id"])


def _enter_seq(item: Dict[str, Any]) -> int:
    return int(item["invocation"].get("enter_seq") or 0)


def _exit_seq(item: Dict[str, Any]) -> int:
    invocation = item["invocation"]
    return int(invocation.get("exit_seq") or invocation.get("enter_seq") or 0)


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


def _focus_participants(focus: Dict[str, Any]) -> set[str]:
    invocation = focus["invocation"]
    result = {str(invocation.get("class") or "")}
    call = focus.get("call") or {}
    caller = str(
        call.get("caller_class")
        or (
            str(call.get("caller") or "").rsplit(".", 1)[0]
            if call.get("caller")
            else ""
        )
    )
    if caller:
        result.add(caller)
    return {value for value in result if value}


def _represented_participants(items: Sequence[Dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for item in items:
        result.update(str(value) for value in item["participant_classes"])
    return result


def _visible_record(item: Dict[str, Any]) -> Dict[str, Any]:
    return {"item": item, "children": []}


def _flatten_visible(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []

    def visit(record: Dict[str, Any]) -> None:
        result.append(record["item"])
        for child in record["children"]:
            visit(child)

    for record in records:
        visit(record)
    return result


def plan_diagram_graph(
    focus: Dict[str, Any],
    max_visible_units: int,
    max_participants: int,
    entry_reason: str,
) -> Dict[str, Any]:
    """Plan a graph of uniformly renderable diagrams using bounded BFS expansion.

    The focal invocation is fixed context and does not consume a visible unit. Direct
    siblings that do not fit are partitioned into symmetric views of the same focal
    invocation. Every view repeats that invocation boundary and summarizes the hidden
    prefix/suffix ranges. Hidden call internals remain directional EXPAND_CALL nodes.
    """
    if max_visible_units <= 1:
        raise ValueError("max_visible_units must be at least 2")
    if max_participants < 2:
        raise ValueError("max_participants must be at least 2")

    pending: Deque[Dict[str, Any]] = deque()
    planned: List[Dict[str, Any]] = []
    next_ordinal = 1
    next_sibling_group = 1
    effective_self_counts: Dict[int, int] = {}
    effective_subtree_counts: Dict[int, int] = {}

    def index_effective_counts(item: Dict[str, Any], parent_multiplier: int) -> int:
        invocation_id = _invocation_id(item)
        effective_self = parent_multiplier * int(item.get("repeat_count") or 1)
        effective_self_counts[invocation_id] = effective_self
        subtree = effective_self
        for child in item.get("children") or []:
            subtree += index_effective_counts(child, effective_self)
        effective_subtree_counts[invocation_id] = subtree
        return subtree

    expected_represented_calls = sum(
        index_effective_counts(item, 1)
        for item in focus.get("children") or []
    )

    def represented_subtrees(items: Sequence[Dict[str, Any]]) -> int:
        return sum(effective_subtree_counts[_invocation_id(item)] for item in items)

    def reserve_id() -> str:
        nonlocal next_ordinal
        diagram_id = f"D-{next_ordinal:03d}"
        next_ordinal += 1
        return diagram_id

    def reserve_sibling_group_id() -> str:
        nonlocal next_sibling_group
        group_id = f"SG-{next_sibling_group:03d}"
        next_sibling_group += 1
        return group_id

    def partition_siblings(
        children: Sequence[Dict[str, Any]],
        scope_focus: Dict[str, Any],
        scope_id: str,
    ) -> List[List[Dict[str, Any]]]:
        ordered = sorted(
            list(children),
            key=lambda item: (_enter_seq(item), _invocation_id(item)),
        )
        if not ordered:
            return [[]]
        atomic_groups: List[List[Dict[str, Any]]] = []
        cursor = 0
        while cursor < len(ordered):
            item = ordered[cursor]
            sequence = item.get("repeat_sequence")
            if sequence is None:
                atomic_groups.append([item])
                cursor += 1
                continue
            length = int(sequence.get("pattern_length") or 0)
            sequence_id = str(sequence.get("sequence_id") or "")
            if length <= 1 or int(sequence.get("position") or 0) != 1:
                raise ValueError(f"invalid repeat sequence start: {scope_id}")
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
                raise ValueError(f"incomplete repeat sequence: {scope_id}")
            atomic_groups.append(group)
            cursor += length
        base_participants = _focus_participants(scope_focus)
        if len(base_participants) > max_participants:
            raise ValueError(f"focus requires too many participants: {scope_id}")
        all_participants = set(base_participants)
        for item in ordered:
            all_participants.update(_call_participants(item))
        if (
            len(ordered) <= max_visible_units
            and len(all_participants) <= max_participants
        ):
            return [ordered]

        chunks: List[List[Dict[str, Any]]] = []
        start = 0
        while start < len(atomic_groups):
            prefix_units = 1 if chunks else 0
            chunk: List[Dict[str, Any]] = []
            participants = set(base_participants)
            cursor = start
            while cursor < len(atomic_groups):
                group = atomic_groups[cursor]
                candidate_participants = set(participants)
                for item in group:
                    candidate_participants.update(_call_participants(item))
                suffix_units = 1 if cursor + 1 < len(atomic_groups) else 0
                candidate_units = (
                    prefix_units + len(chunk) + len(group) + suffix_units
                )
                if (
                    candidate_units > max_visible_units
                    or len(candidate_participants) > max_participants
                ):
                    break
                chunk.extend(group)
                participants = candidate_participants
                cursor += 1
            if not chunk:
                group = atomic_groups[start]
                required_participants = set(base_participants)
                for item in group:
                    required_participants.update(_call_participants(item))
                raise ValueError(
                    "sibling view cannot fit one atomic call group with its global "
                    f"prefix/suffix context: {scope_id}, "
                    f"invocation={_invocation_id(group[0])}, "
                    f"units={prefix_units + len(group) + 1}, "
                    f"participants={len(required_participants)}"
                )
            chunks.append(chunk)
            start = cursor
        return chunks

    entry_id = reserve_id()
    pending.append({
        "diagram_id": entry_id,
        "focus": focus,
        "children": list(focus.get("children") or []),
        "incoming": None,
    })

    while pending:
        scope = pending.popleft()
        scope_focus = scope["focus"]
        first_id = str(scope["diagram_id"])
        chunks = partition_siblings(
            scope["children"], scope_focus, first_id
        )
        view_ids = [first_id] + [reserve_id() for _ in chunks[1:]]
        group_id = reserve_sibling_group_id() if len(chunks) > 1 else None
        ranges = [
            {
                "diagram_id": view_id,
                "items": chunk,
                "invocation_ids": [_invocation_id(item) for item in chunk],
                "enter_seq": min((_enter_seq(item) for item in chunk), default=0),
                "exit_seq": max((_exit_seq(item) for item in chunk), default=0),
                "represented_call_count": represented_subtrees(chunk),
                "represented_participant_count": len(
                    _represented_participants(chunk)
                ),
            }
            for view_id, chunk in zip(view_ids, chunks)
        ]

        for view_index, (diagram_id, direct_children) in enumerate(
            zip(view_ids, chunks)
        ):
            visible_participants = _focus_participants(scope_focus)
            visible_roots = [_visible_record(item) for item in direct_children]
            bfs: Deque[Dict[str, Any]] = deque(visible_roots)
            folds: List[Dict[str, Any]] = []
            visible_units = len(direct_children)
            for item in direct_children:
                visible_participants.update(_call_participants(item))

            def peer_fold(position: str, selected: Sequence[Dict[str, Any]]) -> None:
                nonlocal visible_units
                selected_ranges = list(selected)
                if not selected_ranges:
                    return
                hidden_items = [
                    item for value in selected_ranges for item in value["items"]
                ]
                folds.append({
                    "kind": "SIBLING_BUNDLE",
                    "position": position,
                    "anchor_invocation_id": _invocation_id(scope_focus),
                    "invocation_ids": [
                        _invocation_id(item) for item in hidden_items
                    ],
                    "enter_seq": min(_enter_seq(item) for item in hidden_items),
                    "exit_seq": max(_exit_seq(item) for item in hidden_items),
                    "represented_call_count": represented_subtrees(hidden_items),
                    "represented_participant_count": len(
                        _represented_participants(hidden_items)
                    ),
                    "peer_ranges": [
                        {
                            key: value
                            for key, value in peer_range.items()
                            if key != "items"
                        }
                        for peer_range in selected_ranges
                    ],
                })
                visible_units += 1

            if group_id is not None:
                peer_fold("PREFIX", ranges[:view_index])
                peer_fold("SUFFIX", ranges[view_index + 1:])

            links: List[Dict[str, Any]] = []
            if view_index == 0 and scope.get("incoming") is not None:
                links.append(dict(scope["incoming"]))
            if group_id is not None:
                for peer_range in ranges:
                    if peer_range["diagram_id"] == diagram_id:
                        continue
                    links.append({
                        "direction": "PEER",
                        "diagram_id": peer_range["diagram_id"],
                        "relation": SIBLING_PEER,
                        "invocation_ids": list(peer_range["invocation_ids"]),
                    })

            def enqueue_call_fold(
                anchor: Dict[str, Any], hidden: Sequence[Dict[str, Any]]
            ) -> None:
                hidden_items = list(hidden)
                target_id = reserve_id()
                invocation_ids = [_invocation_id(anchor)]
                folds.append({
                    "kind": "CALL_INTERNAL",
                    "target_diagram_id": target_id,
                    "anchor_invocation_id": _invocation_id(anchor),
                    "invocation_ids": invocation_ids,
                    "enter_seq": min(_enter_seq(item) for item in hidden_items),
                    "exit_seq": max(_exit_seq(item) for item in hidden_items),
                    "represented_call_count": represented_subtrees(hidden_items),
                    "represented_participant_count": len(
                        _represented_participants(hidden_items)
                    ),
                })
                links.append({
                    "direction": "TO",
                    "diagram_id": target_id,
                    "relation": EXPAND_CALL,
                    "invocation_ids": invocation_ids,
                })
                pending.append({
                    "diagram_id": target_id,
                    "focus": anchor,
                    "children": list(anchor.get("children") or []),
                    "incoming": {
                        "direction": "FROM",
                        "diagram_id": diagram_id,
                        "relation": EXPAND_CALL,
                        "invocation_ids": invocation_ids,
                    },
                })

            while bfs:
                record = bfs.popleft()
                item = record["item"]
                descendants = sorted(
                    list(item.get("children") or []),
                    key=lambda child: (_enter_seq(child), _invocation_id(child)),
                )
                if not descendants:
                    continue
                descendant_participants = set(visible_participants)
                for descendant in descendants:
                    descendant_participants.update(_call_participants(descendant))
                can_expand_whole_level = (
                    visible_units + len(descendants) <= max_visible_units
                    and len(descendant_participants) <= max_participants
                )
                if not can_expand_whole_level:
                    enqueue_call_fold(item, descendants)
                    continue
                for descendant in descendants:
                    child_record = _visible_record(descendant)
                    record["children"].append(child_record)
                    bfs.append(child_record)
                visible_units += len(descendants)
                visible_participants = descendant_participants

            visible_items = _flatten_visible(visible_roots)
            sibling_group = None
            if group_id is not None:
                sibling_group = {
                    "group_id": group_id,
                    "ordinal": view_index + 1,
                    "count": len(chunks),
                    "visible_invocation_ids": [
                        _invocation_id(item) for item in direct_children
                    ],
                    "peer_diagram_ids": [
                        value for value in view_ids if value != diagram_id
                    ],
                }
            planned.append({
                "diagram_id": diagram_id,
                "focus": scope_focus,
                "visible_roots": visible_roots,
                "visible_items": visible_items,
                "folds": folds,
                "links": links,
                "sibling_group": sibling_group,
                "visible_unit_count": visible_units,
                "participant_classes": sorted(visible_participants),
                "represented_call_count": sum(
                    effective_self_counts[_invocation_id(item)]
                    for item in visible_items
                ),
            })

    by_id = {node["diagram_id"]: node for node in planned}
    for node in planned:
        for link in list(node["links"]):
            if link["direction"] != "TO":
                continue
            target = by_id[link["diagram_id"]]
            reverse = {
                "direction": "FROM",
                "diagram_id": node["diagram_id"],
                "relation": link["relation"],
                "invocation_ids": list(link["invocation_ids"]),
            }
            if reverse not in target["links"]:
                target["links"].insert(0, reverse)

    owned_ids = [
        _invocation_id(item)
        for node in planned for item in node["visible_items"]
    ]
    expected_ids: List[int] = []

    def collect(items: Sequence[Dict[str, Any]]) -> None:
        for item in items:
            expected_ids.append(_invocation_id(item))
            collect(item.get("children") or [])

    collect(focus.get("children") or [])
    if len(owned_ids) != len(set(owned_ids)):
        raise ValueError("diagram graph assigns an invocation more than once")
    if set(owned_ids) != set(expected_ids):
        raise ValueError("diagram graph does not cover every invocation")
    if sum(
        int(node["represented_call_count"]) for node in planned
    ) != expected_represented_calls:
        raise ValueError("diagram graph does not partition represented calls")
    if any(
        node["visible_unit_count"] > max_visible_units
        or len(node["participant_classes"]) > max_participants
        for node in planned
    ):
        raise ValueError("diagram graph exceeds a display budget")

    return {
        "entry_diagram_id": entry_id,
        "entry_reason": entry_reason,
        "nodes": planned,
    }
