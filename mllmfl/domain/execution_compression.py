import hashlib
import json
from typing import Any, Dict, List, Sequence, Tuple

from .schemas.execution import validate_compressed_execution
from .trace import EXECUTION_SCHEMA, validate_trace



def compress_execution(
    execution: Dict[str, Any],
    max_sequence_pattern_length: int = 22,
    max_sequence_participants: int = 8,
    protected_invocation_ids: frozenset[int] = frozenset(),
) -> Dict[str, Any]:
    """Losslessly compress adjacent repeated sibling-subtree sequences.

    Subtrees are assigned collision-safe structural IDs bottom-up.  Repetition is
    detected only between consecutive blocks of those IDs, so every pattern slot
    represents a recursively identical complete subtree.
    """
    validate_trace(execution, EXECUTION_SCHEMA)
    value_capture_enabled = bool(
        execution.get("schema_version") == 4
        and ((execution.get("test_start") or {}).get("value_capture") or {}).get(
            "capture_values"
        )
    )
    if max_sequence_pattern_length <= 0:
        raise ValueError("max_sequence_pattern_length must be positive")
    if max_sequence_participants < 2:
        raise ValueError("max_sequence_participants must be at least 2")
    calls_by_id = {
        int(call["invocation_id"]): dict(call) for call in execution["calls"]
    }
    invocations = {
        int(invocation["invocation_id"]): dict(invocation)
        for invocation in execution["invocations"]
    }
    children: Dict[int, List[int]] = {}
    for call in execution["calls"]:
        children.setdefault(int(call["parent_invocation_id"]), []).append(
            int(call["invocation_id"])
        )
    for values in children.values():
        values.sort(key=lambda invocation_id: (
            int(calls_by_id[invocation_id].get("enter_seq") or 0), invocation_id
        ))

    def digest(parts: Sequence[str]) -> str:
        value = hashlib.sha256()
        for part in parts:
            encoded = part.encode("utf-8", errors="replace")
            value.update(len(encoded).to_bytes(8, "big"))
            value.update(encoded)
        return value.hexdigest()

    structural_ids: Dict[Tuple[Any, ...], int] = {}

    def structural_id(key: Tuple[Any, ...]) -> int:
        existing = structural_ids.get(key)
        if existing is not None:
            return existing
        value = len(structural_ids) + 1
        structural_ids[key] = value
        return value

    def compress_candidates(
        candidates: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Choose a deterministic minimum-size tiling of tandem repeats."""
        values = list(candidates)
        if value_capture_enabled:
            return [dict(item) for item in values]
        size = len(values)
        if not values:
            return []
        structure = [int(item["_structure_id"]) for item in values]
        best_cost = [0] * (size + 1)
        choices: List[Tuple[int, int]] = [(1, 1)] * size

        for start in range(size - 1, -1, -1):
            best_key = (1 + best_cost[start + 1], -1, 1, -1)
            best_choice = (1, 1)
            upper = min(max_sequence_pattern_length, (size - start) // 2)
            for period in range(1, upper + 1):
                pattern_participants = set()
                for item in values[start:start + period]:
                    call = item["call"]
                    pattern_participants.add(str(
                        call.get("caller_class")
                        or str(call["caller"]).rsplit(".", 1)[0]
                    ))
                    pattern_participants.add(str(
                        call.get("callee_class")
                        or str(call["callee"]).rsplit(".", 1)[0]
                    ))
                if len(pattern_participants) > max_sequence_participants:
                    continue
                repeat_count = 1
                while start + (repeat_count + 1) * period <= size:
                    left = start
                    right = start + repeat_count * period
                    if any(
                        structure[left + offset] != structure[right + offset]
                        for offset in range(period)
                    ):
                        break
                    repeat_count += 1
                for count in range(2, repeat_count + 1):
                    end = start + period * count
                    key = (
                        period + best_cost[end],
                        -(period * count),
                        period,
                        -count,
                    )
                    if key < best_key:
                        best_key = key
                        best_choice = (period, count)
            best_cost[start] = best_key[0]
            choices[start] = best_choice

        result: List[Dict[str, Any]] = []
        start = 0
        while start < size:
            period, repeat_count = choices[start]
            if repeat_count == 1:
                result.append(dict(values[start]))
                start += 1
                continue
            sequence_id = f"RS-{int(values[start]['representative_invocation_id'])}"
            for position in range(period):
                occurrences = [
                    values[start + cycle * period + position]
                    for cycle in range(repeat_count)
                ]
                representative = dict(occurrences[0])
                representative["repeat_count"] = repeat_count
                representative["represented_call_count"] = sum(
                    int(item["represented_call_count"]) for item in occurrences
                )
                representative["occurrence_invocation_ids"] = [
                    int(item["representative_invocation_id"])
                    for item in occurrences
                ]
                if period > 1:
                    representative["repeat_sequence"] = {
                        "sequence_id": sequence_id,
                        "pattern_length": period,
                        "position": position + 1,
                        "repeat_count": repeat_count,
                    }
                result.append(representative)
            start += period * repeat_count
        return result

    def compress_siblings(invocation_ids: Sequence[int]) -> List[Dict[str, Any]]:
        return compress_candidates([
            compress_node(invocation_id) for invocation_id in invocation_ids
        ])

    def compress_node(invocation_id: int) -> Dict[str, Any]:
        call = calls_by_id[invocation_id]
        invocation = invocations[invocation_id]
        child_candidates = [
            compress_node(child_id) for child_id in children.get(invocation_id, [])
        ]
        compressed_children = compress_candidates(child_candidates)
        caller_class = str(call.get("caller_class") or call["caller"].rsplit(".", 1)[0])
        callee_class = str(call.get("callee_class") or call["callee"].rsplit(".", 1)[0])
        participants = {caller_class, callee_class}
        for child in compressed_children:
            participants.update(child["participant_classes"])
        metadata = (
            caller_class,
            str(call.get("caller_method") or ""),
            str(call.get("caller_descriptor") or ""),
            callee_class,
            str(call.get("callee_method") or ""),
            str(call.get("callee_descriptor") or ""),
            str(call.get("thread_id") or ""),
            str(call.get("exit_type") or ""),
            str(call.get("origin_test_line") or ""),
            str(invocation.get("exception_class") or ""),
            str(invocation.get("message") or ""),
            json.dumps(invocation.get("arguments"), ensure_ascii=False, sort_keys=True),
            json.dumps(invocation.get("return_value"), ensure_ascii=False, sort_keys=True),
            str(
                invocation_id
                if invocation_id in protected_invocation_ids
                else 0
            ),
        )
        logical_child_ids = tuple(
            int(child["_structure_id"]) for child in child_candidates
        )
        node_structure_id = structural_id((*metadata, logical_child_ids))
        fingerprint = digest([
            *metadata,
            *(str(child["subtree_fingerprint"]) for child in child_candidates),
        ])
        return {
            "_structure_id": node_structure_id,
            "representative_invocation_id": invocation_id,
            "call": dict(call),
            "invocation": dict(invocation),
            "repeat_count": 1,
            "occurrence_invocation_ids": [invocation_id],
            "represented_call_count": 1 + sum(
                int(child["represented_call_count"])
                for child in compressed_children
            ),
            "displayed_subtree_call_count": 1 + sum(
                int(child["displayed_subtree_call_count"])
                for child in compressed_children
            ),
            "participant_classes": sorted(participants),
            "subtree_fingerprint": fingerprint,
            "children": compressed_children,
        }

    call_ids = set(calls_by_id)
    root_parents: List[int] = []
    for call in execution["calls"]:
        parent_id = int(call["parent_invocation_id"])
        if parent_id not in call_ids and parent_id not in root_parents:
            root_parents.append(parent_id)
    root_groups = [
        {
            "parent_invocation_id": parent_id,
            "calls": compress_siblings(children.get(parent_id, [])),
        }
        for parent_id in root_parents
    ]
    displayed = sum(
        int(node["displayed_subtree_call_count"])
        for group in root_groups for node in group["calls"]
    )
    represented = sum(
        int(node["represented_call_count"])
        for group in root_groups for node in group["calls"]
    )
    result = {
        "schema": "fullchain-compressed-execution",
        "schema_version": 3,
        "source_schema": execution["schema"],
        "source_schema_version": execution["schema_version"],
        "test": dict(execution.get("test") or {}),
        "source_call_count": len(execution["calls"]),
        "represented_call_count": represented,
        "displayed_call_count": displayed,
        "value_capture_enabled": value_capture_enabled,
        "root_groups": root_groups,
    }

    def remove_internal_ids(items: Sequence[Dict[str, Any]]) -> None:
        for item in items:
            item.pop("_structure_id", None)
            remove_internal_ids(item["children"])

    for group in root_groups:
        remove_internal_ids(group["calls"])
    validate_compressed_execution(result)
    return result
