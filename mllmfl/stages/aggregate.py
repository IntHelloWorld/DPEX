from collections import Counter
from typing import Any, Dict, List, Sequence

from mllmfl.domain.schemas import validate_localization
from mllmfl.infrastructure.io import read_json, write_csv, write_json
from mllmfl.infrastructure.layout import RunLayout


def aggregate_rankings(results: Sequence[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    stats: Dict[str, Dict[str, Any]] = {}
    for result in results:
        seen = set()
        trigger = str(result.get("trigger", ""))
        for fallback_rank, item in enumerate((result.get("ranking") or [])[:top_k], 1):
            function = str(item.get("function") or "")
            if not function or function in seen:
                continue
            seen.add(function)
            rank = int(item.get("rank") or fallback_rank)
            stat = stats.setdefault(
                function,
                {
                    "function": function,
                    "trigger_support": 0,
                    "rank_sum": 0,
                    "best_rank": rank,
                    "reciprocal_rank_sum": 0.0,
                    "trigger_details": [],
                },
            )
            stat["trigger_support"] += 1
            stat["rank_sum"] += rank
            stat["best_rank"] = min(stat["best_rank"], rank)
            stat["reciprocal_rank_sum"] += 1.0 / rank
            stat["trigger_details"].append(
                {
                    "trigger": trigger,
                    "rank": rank,
                    "reason": str(item.get("reason") or "")[:200],
                }
            )
    ranking = []
    for stat in stats.values():
        support = stat["trigger_support"]
        stat["average_rank"] = round(stat.pop("rank_sum") / support, 4)
        stat["reciprocal_rank_sum"] = round(stat["reciprocal_rank_sum"], 6)
        ranking.append(stat)
    ranking.sort(
        key=lambda item: (
            -item["trigger_support"],
            item["average_rank"],
            item["best_rank"],
            -item["reciprocal_rank_sum"],
            item["function"],
        )
    )
    for index, item in enumerate(ranking[:top_k], 1):
        item["rank"] = index
    return ranking[:top_k]


def run(layout: RunLayout, projects: Sequence[str], bugs: set[str] | None,
        top_k: int) -> List[Dict[str, object]]:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    grouped: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
    statuses: Dict[tuple[str, str], Counter] = {}
    for project, bug, _, directory in layout.discover_triggers(projects, bugs):
        key = (project, bug)
        try:
            result = read_json(directory / "localization.json")
            validate_localization(result)
        except ValueError:
            statuses.setdefault(key, Counter())["MISSING"] += 1
            continue
        statuses.setdefault(key, Counter())[str(result.get("status") or "UNKNOWN")] += 1
        if result.get("ranking"):
            grouped.setdefault(key, []).append(result)
    rows = []
    for key in sorted(set(grouped) | set(statuses)):
        project, bug = key
        ranking = aggregate_rankings(grouped.get(key, []), top_k)
        output = {
            "schema": "fault-localization-aggregate",
            "schema_version": 1,
            "project": project,
            "bug": bug,
            "top_k": top_k,
            "valid_trigger_count": len(grouped.get(key, [])),
            "status_counts": dict(statuses.get(key, {})),
            "ranking": ranking,
        }
        path = layout.summaries / project / f"bug_{bug}.json"
        write_json(path, output)
        rows.append(
            {
                "project": project,
                "bug": bug,
                "valid_trigger_count": output["valid_trigger_count"],
                "top1": ranking[0]["function"] if ranking else "",
                "result": str(path),
            }
        )
    write_csv(layout.logs / "aggregate.csv", rows,
              ["project", "bug", "valid_trigger_count", "top1", "result"])
    return rows
