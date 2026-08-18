from typing import Any, Dict, Sequence, Set


def evaluate_ranking(
    ranking: Sequence[str],
    ground_truth: Set[str],
) -> Dict[str, Any]:
    if not ground_truth:
        raise ValueError("ground truth must not be empty")
    seen = set()
    relevant_ranks = []
    relevant_seen = 0
    precision_sum = 0.0
    for rank, function in enumerate(ranking, 1):
        if not function or function in seen:
            raise ValueError("ranking functions must be non-empty and unique")
        seen.add(function)
        if function not in ground_truth:
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
