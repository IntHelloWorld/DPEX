from pathlib import Path
from typing import Any, Dict, List, Sequence

from mllmfl.domain.evaluation import evaluate_ranking, mean_metrics
from mllmfl.domain.schemas import validate_aggregate, validate_evaluation
from mllmfl.infrastructure.ground_truth import ground_truth_methods
from mllmfl.infrastructure.io import read_json, write_csv, write_json
from mllmfl.infrastructure.layout import RunLayout


def _targets(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
) -> List[tuple[str, str, Path]]:
    result = []
    for project in sorted(set(projects)):
        directory = layout.summaries / project
        selected = (
            sorted(bugs, key=int)
            if bugs is not None
            else sorted(
                (
                    path.stem.removeprefix("bug_")
                    for path in directory.glob("bug_*.json")
                    if path.stem.removeprefix("bug_").isdigit()
                ),
                key=int,
            )
        )
        for bug in selected:
            result.append((project, bug, directory / f"bug_{bug}.json"))
    return result


def _skipped(project: str, bug: str, status: str, error: str) -> Dict[str, Any]:
    return {
        "project": project,
        "bug": bug,
        "status": status,
        "error": error,
        "ground_truth": [],
        "ranking": [],
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
            aggregate = validate_aggregate(read_json(result_path))
            if aggregate["project"] != project or aggregate["bug"] != bug:
                raise ValueError("aggregate identity does not match its path")
        except ValueError as error:
            details.append(_skipped(project, bug, "INVALID_RESULT", str(error)))
            continue
        if aggregate["valid_trigger_count"] == 0:
            details.append(_skipped(
                project,
                bug,
                "NO_VALID_RESULT",
                "aggregate contains no valid trigger ranking",
            ))
            continue
        try:
            truth = ground_truth_methods(
                d4j_home, layout.workspace_dir(project, bug), project, bug
            )
        except (OSError, UnicodeError, ValueError) as error:
            details.append(_skipped(project, bug, "GROUND_TRUTH_ERROR", str(error)))
            continue
        ranking = [str(item["function"]) for item in aggregate["ranking"]]
        metrics = evaluate_ranking(ranking, set(truth))
        detail = {
            "project": project,
            "bug": bug,
            "status": "OK",
            "error": "",
            "ground_truth": truth,
            "ranking": ranking,
            **metrics,
        }
        details.append(detail)
        evaluated_metrics.append(metrics)

    output = {
        "schema": "fault-localization-evaluation",
        "schema_version": 1,
        "ground_truth_source": "Defects4J source patches mapped to buggy Java AST methods",
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
            "top1": int(item["top_1"]),
            "top3": int(item["top_3"]),
            "top5": int(item["top_5"]),
            "reciprocal_rank": item["reciprocal_rank"],
            "average_precision": item["average_precision"],
            "ground_truth": ";".join(item["ground_truth"]),
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
            "top1",
            "top3",
            "top5",
            "reciprocal_rank",
            "average_precision",
            "ground_truth",
            "error",
        ],
    )
    return rows
