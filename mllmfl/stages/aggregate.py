from collections import Counter
from typing import Any, Dict, List, Sequence

from mllmfl.domain.schemas import validate_localization
from mllmfl.infrastructure.io import read_json, write_csv, write_json
from mllmfl.infrastructure.layout import RunLayout


def aggregate_rankings(
    results: Sequence[Dict[str, Any]],
    top_k: int,
    use_source_ranges: bool | None = None,
) -> List[Dict[str, Any]]:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    ranked_items = [
        item
        for result in results
        for item in (result.get("ranking") or [])[:top_k]
    ]
    if use_source_ranges is None:
        use_source_ranges = bool(ranked_items) and all(
            str(item.get("source_file") or "")
            and isinstance(item.get("start_line"), int)
            and isinstance(item.get("end_line"), int)
            for item in ranked_items
        )
    stats: Dict[Any, Dict[str, Any]] = {}
    for result in results:
        seen = set()
        trigger = str(result.get("trigger", ""))
        for fallback_rank, item in enumerate((result.get("ranking") or [])[:top_k], 1):
            function = str(item.get("function") or "")
            identity: Any = function
            if use_source_ranges:
                identity = (
                    str(item.get("source_file") or ""),
                    int(item.get("start_line") or 0),
                    int(item.get("end_line") or 0),
                )
            if not function or identity in seen:
                continue
            seen.add(identity)
            rank = int(item.get("rank") or fallback_rank)
            stat = stats.setdefault(
                identity,
                {
                    "function": function,
                    **(
                        {
                            "signature": str(item.get("signature") or ""),
                            "source_file": identity[0],
                            "start_line": identity[1],
                            "end_line": identity[2],
                        }
                        if use_source_ranges else {}
                    ),
                    "trigger_support": 0,
                    "trigger_details": [],
                },
            )
            stat["trigger_support"] += 1
            stat["trigger_details"].append(
                {
                    "trigger": trigger,
                    "rank": rank,
                    "reason": str(item.get("reason") or "")[:200],
                }
            )
    ranking = list(stats.values())
    ranking.sort(key=lambda item: -item["trigger_support"])
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
        results = grouped.get(key, [])
        use_source_ranges = bool(results) and all(
            result.get("schema_version") == 4 for result in results
        )
        ranking = aggregate_rankings(
            results, top_k, use_source_ranges=use_source_ranges
        )
        output = {
            "schema": "fault-localization-aggregate",
            "schema_version": 2 if use_source_ranges else 1,
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
