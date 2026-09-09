import re
from typing import Any, Dict

from .trace import EXECUTION_SCHEMA, validate_trace


FOLDING_SCHEMA = "assertion-trace-folding"
FOLDING_SCHEMA_VERSION = 1


def _successful_assertion_intervals(
    execution: Dict[str, Any],
) -> tuple[list[Dict[str, Any]], int]:
    active: Dict[int, Dict[str, Any]] = {}
    occurrences: Dict[str, int] = {}
    intervals = []
    unmatched = 0
    for event in execution.get("assertions") or []:
        thread_id = int(event.get("thread_id") or 0)
        event_type = str(event["type"])
        if event_type == "ASSERT_START":
            if thread_id in active:
                unmatched += 1
            active[thread_id] = event
            continue
        start = active.pop(thread_id, None)
        if (
            start is None
            or start["assertion_id"] != event["assertion_id"]
        ):
            unmatched += 1
            continue
        assertion_id = str(event["assertion_id"])
        occurrences[assertion_id] = occurrences.get(assertion_id, 0) + 1
        intervals.append({
            "assertion_id": assertion_id,
            "occurrence": occurrences[assertion_id],
            "outcome": "PASS" if event_type == "ASSERT_PASS" else "FAIL",
            "thread_id": thread_id,
            "source_start_line": int(start["source_start_line"]),
            "source_end_line": int(start["source_end_line"]),
            "start_seq": int(start["seq"]),
            "end_seq": int(event["seq"]),
        })
    unmatched += len(active)
    return intervals, unmatched


def fold_successful_assertions(
    execution: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Hide complete normally-returning call subtrees owned by passed assertions.

    The input execution remains unchanged. The returned execution contains only the
    default visible calls, while metadata retains every folded invocation ID so a
    consumer can recover it from the full execution artifact.
    """
    validate_trace(execution, EXECUTION_SCHEMA)
    intervals, unmatched = _successful_assertion_intervals(execution)
    calls_by_id = {
        int(call["invocation_id"]): call for call in execution["calls"]
    }
    children: Dict[int, list[int]] = {}
    for call in execution["calls"]:
        children.setdefault(int(call["parent_invocation_id"]), []).append(
            int(call["invocation_id"])
        )
    for values in children.values():
        values.sort(key=lambda invocation_id: (
            int(calls_by_id[invocation_id].get("enter_seq") or 0),
            invocation_id,
        ))

    folded_ids: set[int] = set()
    folds = []
    for interval in intervals:
        if interval["outcome"] != "PASS":
            continue
        candidates = {
            invocation_id
            for invocation_id, call in calls_by_id.items()
            if (
                invocation_id not in folded_ids
                and int(call.get("thread_id") or 0) == interval["thread_id"]
                and int(call.get("enter_seq") or 0) > interval["start_seq"]
                and int(call.get("exit_seq") or 0) < interval["end_seq"]
                and interval["source_start_line"]
                <= int(call.get("origin_test_line") or 0)
                <= interval["source_end_line"]
            )
        }
        safe: Dict[int, bool] = {}

        def safe_subtree(invocation_id: int) -> bool:
            existing = safe.get(invocation_id)
            if existing is not None:
                return existing
            call = calls_by_id[invocation_id]
            value = (
                invocation_id in candidates
                and str(call.get("exit_type") or "") == "RETURN"
                and all(
                    safe_subtree(child_id)
                    for child_id in children.get(invocation_id, [])
                )
            )
            safe[invocation_id] = value
            return value

        safe_ids = {
            invocation_id for invocation_id in candidates
            if safe_subtree(invocation_id)
        }
        root_ids = sorted(
            (
                invocation_id for invocation_id in safe_ids
                if int(calls_by_id[invocation_id]["parent_invocation_id"])
                not in safe_ids
            ),
            key=lambda invocation_id: (
                int(calls_by_id[invocation_id].get("enter_seq") or 0),
                invocation_id,
            ),
        )
        if not root_ids:
            continue
        invocation_ids = sorted(
            safe_ids,
            key=lambda invocation_id: (
                int(calls_by_id[invocation_id].get("enter_seq") or 0),
                invocation_id,
            ),
        )
        folded_ids.update(invocation_ids)
        folds.append({
            "fold_id": f"AF{len(folds) + 1:03d}",
            **interval,
            "call_count": len(invocation_ids),
            "root_invocation_ids": root_ids,
            "invocation_ids": invocation_ids,
            "root_methods": [
                str(calls_by_id[invocation_id]["callee"])
                for invocation_id in root_ids
            ],
        })

    metadata = {
        "schema": FOLDING_SCHEMA,
        "schema_version": FOLDING_SCHEMA_VERSION,
        "strategy": "dynamic-successful-assertion-subtree-folding",
        "original_call_count": len(execution["calls"]),
        "retained_call_count": len(execution["calls"]) - len(folded_ids),
        "folded_call_count": len(folded_ids),
        "assertion_interval_count": len(intervals),
        "successful_assertion_count": sum(
            1 for item in intervals if item["outcome"] == "PASS"
        ),
        "failed_assertion_count": sum(
            1 for item in intervals if item["outcome"] == "FAIL"
        ),
        "unmatched_assertion_event_count": unmatched,
        "fold_count": len(folds),
        "folds": folds,
    }
    validate_assertion_folding(metadata)
    if not folded_ids:
        return execution, metadata
    retained_calls = [
        dict(call) for call in execution["calls"]
        if int(call["invocation_id"]) not in folded_ids
    ]
    retained_invocation_ids = {
        int(call["invocation_id"]) for call in retained_calls
    }
    parents = {
        int(item["invocation_id"]): int(item.get("parent_id") or 0)
        for item in execution["invocations"]
    }
    # Compute the ancestor closure once, without materializing a chain per call.
    pending = list(retained_invocation_ids)
    while pending:
        parent = parents.get(pending.pop(), 0)
        if parent and parent not in retained_invocation_ids:
            retained_invocation_ids.add(parent)
            pending.append(parent)
    retained_invocations = [
        dict(invocation) for invocation in execution["invocations"]
        if int(invocation["invocation_id"]) in retained_invocation_ids
    ]
    result = dict(execution)
    result.update({
        "calls": retained_calls,
        "invocations": retained_invocations,
        "call_count": len(retained_calls),
        "assertion_folding": metadata,
    })
    validate_trace(result, EXECUTION_SCHEMA)
    return result, metadata


def validate_assertion_folding(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("assertion folding must be a JSON object")
    if (
        value.get("schema") != FOLDING_SCHEMA
        or value.get("schema_version") != FOLDING_SCHEMA_VERSION
    ):
        raise ValueError("unsupported assertion folding schema")
    counts = (
        "original_call_count", "retained_call_count", "folded_call_count",
        "assertion_interval_count", "successful_assertion_count",
        "failed_assertion_count", "unmatched_assertion_event_count", "fold_count",
    )
    if any(
        not isinstance(value.get(field), int)
        or isinstance(value[field], bool)
        or value[field] < 0
        for field in counts
    ):
        raise ValueError("invalid assertion folding counts")
    if (
        value["original_call_count"]
        != value["retained_call_count"] + value["folded_call_count"]
        or value["assertion_interval_count"]
        != value["successful_assertion_count"] + value["failed_assertion_count"]
    ):
        raise ValueError("inconsistent assertion folding counts")
    folds = value.get("folds")
    if not isinstance(folds, list) or value["fold_count"] != len(folds):
        raise ValueError("invalid assertion folding folds")
    seen_ids, seen_invocations = set(), set()
    for index, fold in enumerate(folds, 1):
        invocation_ids = fold.get("invocation_ids") if isinstance(fold, dict) else None
        roots = fold.get("root_invocation_ids") if isinstance(fold, dict) else None
        if (
            not isinstance(fold, dict)
            or fold.get("fold_id") != f"AF{index:03d}"
            or fold["fold_id"] in seen_ids
            or not isinstance(fold.get("assertion_id"), str)
            or re.fullmatch(r"A\d{3,}", fold["assertion_id"]) is None
            or fold.get("outcome") != "PASS"
            or not isinstance(invocation_ids, list)
            or not invocation_ids
            or len(invocation_ids) != len(set(invocation_ids))
            or any(
                not isinstance(item, int) or isinstance(item, bool) or item <= 0
                or item in seen_invocations
                for item in invocation_ids
            )
            or not isinstance(roots, list)
            or not roots
            or any(item not in invocation_ids for item in roots)
            or fold.get("call_count") != len(invocation_ids)
            or not isinstance(fold.get("root_methods"), list)
            or len(fold["root_methods"]) != len(roots)
        ):
            raise ValueError(f"invalid assertion fold at index {index - 1}")
        seen_ids.add(fold["fold_id"])
        seen_invocations.update(invocation_ids)
    if len(seen_invocations) != value["folded_call_count"]:
        raise ValueError("inconsistent assertion folded invocation count")
    return value
