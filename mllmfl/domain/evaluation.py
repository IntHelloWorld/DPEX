from typing import Any, Dict, Hashable, Sequence, Set


def method_location_key(item: Dict[str, Any]) -> tuple[str, int, int]:
    source_file = str(item.get("source_file") or "")
    start_line = item.get("start_line")
    end_line = item.get("end_line")
    if (
        not source_file
        or not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not isinstance(end_line, int)
        or isinstance(end_line, bool)
        or start_line <= 0
        or end_line < start_line
    ):
        raise ValueError("invalid method source location")
    return source_file, start_line, end_line


def _evaluate_identities(
    ranking: Sequence[Hashable], ground_truth: Set[Hashable]
) -> Dict[str, Any]:
    if not ground_truth:
        raise ValueError("ground truth must not be empty")
    seen = set()
    relevant_ranks = []
    relevant_seen = 0
    precision_sum = 0.0
    for rank, identity in enumerate(ranking, 1):
        if not identity or identity in seen:
            raise ValueError("ranking methods must be non-empty and unique")
        seen.add(identity)
        if identity not in ground_truth:
            continue
        relevant_seen += 1
        relevant_ranks.append(rank)
        precision_sum += relevant_seen / rank
    first_rank = relevant_ranks[0] if relevant_ranks else None
    return {
        "relevant_ranks": relevant_ranks,
        "top_1": bool(first_rank is not None and first_rank <= 1),
        "top_3": bool(first_rank is not None and first_rank <= 3),
        "top_5": bool(first_rank is not None and first_rank <= 5),
        "reciprocal_rank": round(1.0 / first_rank, 6) if first_rank else 0.0,
        "average_precision": round(precision_sum / len(ground_truth), 6),
    }


def evaluate_ranking(
    ranking: Sequence[str],
    ground_truth: Set[str],
) -> Dict[str, Any]:
    return _evaluate_identities(ranking, ground_truth)


def evaluate_location_ranking(
    ranking: Sequence[Dict[str, Any]],
    ground_truth: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    return _evaluate_identities(
        [method_location_key(item) for item in ranking],
        {method_location_key(item) for item in ground_truth},
    )


def mean_metrics(results: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    if not results:
        return {"top_1": 0.0, "top_3": 0.0, "top_5": 0.0, "mrr": 0.0, "map": 0.0}
    count = len(results)
    return {
        "top_1": round(sum(int(item["top_1"]) for item in results) / count, 6),
        "top_3": round(sum(int(item["top_3"]) for item in results) / count, 6),
        "top_5": round(sum(int(item["top_5"]) for item in results) / count, 6),
        "mrr": round(sum(float(item["reciprocal_rank"]) for item in results) / count, 6),
        "map": round(sum(float(item["average_precision"]) for item in results) / count, 6),
    }
