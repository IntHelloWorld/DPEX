import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from mllmfl.domain.evaluation import (
    evaluate_location_ranking,
    evaluate_ranking,
    mean_metrics,
)
from mllmfl.domain.schemas import (
    validate_aggregate,
    validate_evaluation,
    validate_localization,
)
from mllmfl.infrastructure.ground_truth import (
    ground_truth_locations,
    ground_truth_methods,
)
from mllmfl.infrastructure.io import read_json, write_csv, write_json
from mllmfl.infrastructure.layout import RunLayout


def _targets(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
) -> List[tuple[str, str, Path]]:
    result = []
    for project in sorted(set(projects)):
        summary_dir = layout.summaries / project
        artifact_dir = layout.artifacts / project
        selected = (
            sorted(bugs, key=int)
            if bugs is not None
            else sorted(
                {
                    path.stem.removeprefix("bug_")
                    for path in summary_dir.glob("bug_*.json")
                    if path.stem.removeprefix("bug_").isdigit()
                }
                | {
                    path.name.removeprefix("bug_")
                    for path in artifact_dir.glob("bug_*")
                    if path.name.removeprefix("bug_").isdigit()
                    and (path / "localization.json").is_file()
                },
                key=int,
            )
        )
        for bug in selected:
            current = artifact_dir / f"bug_{bug}" / "localization.json"
            legacy = summary_dir / f"bug_{bug}.json"
            result.append((project, bug, current if current.is_file() else legacy))
    return result


def _skipped(project: str, bug: str, status: str, error: str) -> Dict[str, Any]:
    return {
        "project": project,
        "bug": bug,
        "status": status,
        "error": error,
        "identity_mode": "none",
        "ground_truth": [],
        "ground_truth_locations": [],
        "ranking": [],
        "ranking_locations": [],
        "relevant_ranks": [],
        "top_1": False,
        "top_3": False,
        "top_5": False,
        "reciprocal_rank": 0.0,
        "average_precision": 0.0,
    }


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    d4j_home: Path,
) -> List[Dict[str, Any]]:
    details: List[Dict[str, Any]] = []
    evaluated_metrics = []
    for project, bug, result_path in _targets(layout, projects, bugs):
        if not result_path.is_file():
            details.append(_skipped(project, bug, "MISSING_RESULT", str(result_path)))
            continue
        try:
            raw_result = read_json(result_path)
            if (
                raw_result.get("schema") == "fault-localization"
                and raw_result.get("schema_version") == 5
            ):
                localization = validate_localization(raw_result)
                result = localization
                valid_result_count = 1 if localization.get("ranking") else 0
                source_range_mode = True
            else:
                aggregate = validate_aggregate(raw_result)
                result = aggregate
                valid_result_count = int(aggregate["valid_trigger_count"])
                source_range_mode = aggregate["schema_version"] == 2
            if result["project"] != project or result["bug"] != bug:
                raise ValueError("localization result identity does not match its path")
        except ValueError as error:
            details.append(_skipped(project, bug, "INVALID_RESULT", str(error)))
            continue
        if valid_result_count == 0:
            details.append(_skipped(
                project,
                bug,
                "NO_VALID_RESULT",
                "localization contains no valid ranking",
            ))
            continue
        try:
            if source_range_mode:
                truth_locations = ground_truth_locations(
                    d4j_home, layout.workspace_dir(project, bug), project, bug
                )
                ranking_locations = [
                    {
                        "function": str(item["function"]),
                        "source_file": str(item["source_file"]),
                        "start_line": int(item["start_line"]),
                        "end_line": int(item["end_line"]),
                    }
                    for item in result["ranking"]
                ]
                truth = list(dict.fromkeys(
                    str(item["function"]) for item in truth_locations
                ))
                metrics = evaluate_location_ranking(
                    ranking_locations, truth_locations
                )
                identity_mode = "source_range"
            else:
                truth = ground_truth_methods(
                    d4j_home, layout.workspace_dir(project, bug), project, bug
                )
                truth_locations = []
                ranking_locations = []
                metrics = evaluate_ranking(
                    [str(item["function"]) for item in result["ranking"]],
                    set(truth),
                )
                identity_mode = "function"
        except (OSError, UnicodeError, ValueError) as error:
            details.append(_skipped(project, bug, "GROUND_TRUTH_ERROR", str(error)))
            continue
        ranking = [str(item["function"]) for item in result["ranking"]]
        detail = {
            "project": project,
            "bug": bug,
            "status": "OK",
            "error": "",
            "identity_mode": identity_mode,
            "ground_truth": truth,
            "ground_truth_locations": truth_locations,
            "ranking": ranking,
            "ranking_locations": ranking_locations,
            **metrics,
        }
        details.append(detail)
        evaluated_metrics.append(metrics)

    output = {
        "schema": "fault-localization-evaluation",
        "schema_version": 2,
        "ground_truth_source": (
            "Defects4J source patches mapped to buggy Java AST method ranges"
        ),
        "evaluated_bug_count": len(evaluated_metrics),
        "skipped_bug_count": len(details) - len(evaluated_metrics),
        "metrics": mean_metrics(evaluated_metrics),
        "bugs": details,
    }
    validate_evaluation(output)
    write_json(layout.summaries / "evaluation.json", output)
    rows = [
        {
            "project": item["project"],
            "bug": item["bug"],
            "status": item["status"],
            "identity_mode": item["identity_mode"],
            "top1": int(item["top_1"]),
            "top3": int(item["top_3"]),
            "top5": int(item["top_5"]),
            "reciprocal_rank": item["reciprocal_rank"],
            "average_precision": item["average_precision"],
            "ground_truth": ";".join(item["ground_truth"]),
            "ground_truth_locations": json.dumps(
                item["ground_truth_locations"], ensure_ascii=False
            ),
            "error": item["error"],
        }
        for item in details
    ]
    write_csv(
        layout.logs / "evaluate.csv",
        rows,
        [
            "project",
            "bug",
            "status",
            "identity_mode",
            "top1",
            "top3",
            "top5",
            "reciprocal_rank",
            "average_precision",
            "ground_truth",
            "ground_truth_locations",
            "error",
        ],
    )
    return rows
