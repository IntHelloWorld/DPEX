import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from dpex.domain.evaluation import (
    evaluate_location_ranking,
    mean_metrics,
)
from dpex.domain.schemas import (
    validate_evaluation,
    validate_evaluation_ground_truth_cache,
    validate_localization_result,
    validate_refinement,
)
from dpex.infrastructure.checkouts import temporary_checkout
from dpex.infrastructure.ground_truth import (
    GROUND_TRUTH_MAPPING_VERSION,
    ground_truth_locations,
)
from dpex.infrastructure.io import (
    read_json,
    write_compact_json,
    write_csv,
    write_json,
)
from dpex.infrastructure.layout import RunLayout
from dpex.stages.cleanup import final_only_bug


GROUND_TRUTH_CACHE_FILENAME = "evaluation_ground_truth.json"


def _source_patch(d4j_home: Path, project: str, bug: str) -> Path:
    return (
        d4j_home
        / "framework"
        / "projects"
        / project
        / "patches"
        / f"{bug}.src.patch"
    )


def _ground_truth_source_fingerprint(
    d4j_home: Path, project: str, bug: str,
) -> str:
    patch_path = _source_patch(d4j_home, project, bug)
    try:
        patch_bytes = patch_path.read_bytes()
    except OSError as error:
        raise ValueError(
            f"cannot read Defects4J source patch {patch_path}: {error}"
        ) from error
    digest = hashlib.sha256()
    # Preserve cache fingerprints across the package rename.
    digest.update(b"mllmfl-evaluation-ground-truth\0")
    digest.update(str(GROUND_TRUTH_MAPPING_VERSION).encode("ascii"))
    digest.update(b"\0")
    digest.update(project.encode("utf-8"))
    digest.update(b"\0")
    digest.update(bug.encode("utf-8"))
    digest.update(b"\0")
    digest.update(patch_bytes)
    return digest.hexdigest()


def _cached_ground_truth(
    path: Path, project: str, bug: str, source_fingerprint: str,
) -> List[Dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        cached = validate_evaluation_ground_truth_cache(read_json(path))
    except ValueError:
        return None
    if (
        cached["project"] != project
        or cached["bug"] != bug
        or cached["source_fingerprint"] != source_fingerprint
    ):
        return None
    return [dict(item) for item in cached["ground_truth_locations"]]


def _write_ground_truth_cache(
    path: Path,
    project: str,
    bug: str,
    source_fingerprint: str,
    locations: List[Dict[str, Any]],
) -> None:
    value = {
        "schema": "evaluation-ground-truth-cache",
        "schema_version": 1,
        "project": project,
        "bug": bug,
        "source_fingerprint": source_fingerprint,
        "ground_truth_locations": locations,
    }
    validate_evaluation_ground_truth_cache(value)
    write_compact_json(path, value)


def _targets(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
) -> List[tuple[str, str, Path]]:
    result = []
    for project in sorted(set(projects)):
        artifact_dir = layout.artifacts / project
        selected = (
            sorted(bugs, key=int)
            if bugs is not None
            else sorted(
                {
                    path.name.removeprefix("bug_")
                    for path in artifact_dir.glob("bug_*")
                    if path.name.removeprefix("bug_").isdigit()
                    and (
                        (path / "localization.json").is_file()
                        or (path / "refinement.json").is_file()
                    )
                },
                key=int,
            )
        )
        for bug in selected:
            bug_dir = artifact_dir / f"bug_{bug}"
            localization = bug_dir / "localization.json"
            result.append((project, bug, localization if localization.is_file()
                           else bug_dir / "refinement.json"))
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
    final_only: bool = False,
    cache_root: Path | None = None,
) -> List[Dict[str, Any]]:
    details: List[Dict[str, Any]] = []
    evaluated_metrics = []
    ground_truth_cache_hits: set[tuple[str, str]] = set()
    shared_cache_root = cache_root or layout.root.parent / ".dpex-cache"
    for project, bug, result_path in _targets(layout, projects, bugs):
        if not result_path.is_file():
            details.append(_skipped(project, bug, "MISSING_RESULT", str(result_path)))
            continue
        try:
            raw_result = read_json(result_path)
            result = (
                validate_localization_result(raw_result)
                if isinstance(raw_result, dict)
                and raw_result.get("schema") == "fault-localization-result"
                else validate_refinement(raw_result)
            )
            if result["project"] != project or result["bug"] != bug:
                raise ValueError("localization result identity does not match its path")
        except ValueError as error:
            details.append(_skipped(project, bug, "INVALID_RESULT", str(error)))
            continue
        if not result.get("ranking"):
            details.append(_skipped(
                project,
                bug,
                "NO_VALID_RESULT",
                "localization result contains no valid ranking",
            ))
            continue
        try:
            source_fingerprint = _ground_truth_source_fingerprint(
                d4j_home, project, bug
            )
            cache_path = (
                shared_cache_root
                / "artifacts"
                / project
                / f"bug_{bug}"
                / GROUND_TRUTH_CACHE_FILENAME
            )
            truth_locations = _cached_ground_truth(
                cache_path, project, bug, source_fingerprint
            )
            if truth_locations is None:
                with temporary_checkout(
                    layout, project, bug, d4j_home=d4j_home
                ) as workspace:
                    truth_locations = ground_truth_locations(
                        d4j_home, workspace, project, bug
                    )
                _write_ground_truth_cache(
                    cache_path,
                    project,
                    bug,
                    source_fingerprint,
                    truth_locations,
                )
            else:
                ground_truth_cache_hits.add((project, bug))
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
        except (OSError, UnicodeError, ValueError, RuntimeError) as error:
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
            "ground_truth_cache_hit": int(
                (str(item["project"]), str(item["bug"])) in ground_truth_cache_hits
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
            "ground_truth_cache_hit",
            "error",
        ],
    )
    if final_only:
        for item in details:
            if item["status"] == "OK":
                final_only_bug(layout, str(item["project"]), str(item["bug"]))
    return rows
