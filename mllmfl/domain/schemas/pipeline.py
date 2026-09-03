from pathlib import PurePosixPath
from typing import Any, Dict


def _method_location_key(
    item: Dict[str, Any], index: int, context: str
) -> tuple[str, int, int]:
    source_file = item.get("source_file")
    start_line = item.get("start_line")
    end_line = item.get("end_line")
    if not isinstance(source_file, str) or not source_file.strip():
        raise ValueError(f"invalid {context} source_file at index {index}")
    relative = PurePosixPath(source_file)
    if relative.is_absolute() or ".." in relative.parts or "\\" in source_file:
        raise ValueError(f"unsafe {context} source_file at index {index}")
    if (
        not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not isinstance(end_line, int)
        or isinstance(end_line, bool)
        or start_line <= 0
        or end_line < start_line
    ):
        raise ValueError(f"invalid {context} line range at index {index}")
    return source_file, start_line, end_line


def validate_defect_context(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("defect context must be a JSON object")
    if value.get("schema") != "defect-context" or value.get("schema_version") != 1:
        raise ValueError("unsupported defect context schema")
    if not isinstance(value.get("test"), str) or not value["test"].strip():
        raise ValueError("invalid defect context test")
    if not isinstance(value.get("error_stack"), str) or not value[
        "error_stack"
    ].strip():
        raise ValueError("error stack is unavailable")
    if not isinstance(value.get("test_output"), str):
        raise ValueError("invalid defect context test_output")
    return value


def validate_evaluation(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("evaluation artifact must be a JSON object")
    if value.get("schema") != "fault-localization-evaluation" or value.get(
        "schema_version"
    ) != 2:
        raise ValueError("unsupported evaluation schema")
    evaluated = value.get("evaluated_bug_count")
    skipped = value.get("skipped_bug_count")
    bugs = value.get("bugs")
    if not isinstance(evaluated, int) or evaluated < 0:
        raise ValueError("invalid evaluated_bug_count")
    if not isinstance(skipped, int) or skipped < 0:
        raise ValueError("invalid skipped_bug_count")
    if not isinstance(bugs, list) or evaluated + skipped != len(bugs):
        raise ValueError("inconsistent evaluation bug counts")
    if not isinstance(value.get("ground_truth_source"), str) or not value[
        "ground_truth_source"
    ].strip():
        raise ValueError("invalid evaluation ground_truth_source")
    metrics = value.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("invalid evaluation metrics")
    for name in ("top_1", "top_3", "top_5", "mrr", "map"):
        metric = metrics.get(name)
        if (
            not isinstance(metric, (int, float))
            or isinstance(metric, bool)
            or not 0 <= metric <= 1
        ):
            raise ValueError(f"invalid evaluation metric: {name}")
    ok_count = 0
    for index, item in enumerate(bugs):
        if not isinstance(item, dict):
            raise ValueError(f"invalid evaluation bug at index {index}")
        if not all(
            isinstance(item.get(field), str) and item[field].strip()
            for field in ("project", "bug", "status")
        ):
            raise ValueError(f"invalid evaluation bug identity at index {index}")
        if not isinstance(item.get("error"), str):
            raise ValueError(f"invalid evaluation error at index {index}")
        truth = item.get("ground_truth")
        ranking = item.get("ranking")
        ranks = item.get("relevant_ranks")
        if not all(isinstance(values, list) for values in (truth, ranking, ranks)):
            raise ValueError(f"invalid evaluation arrays at index {index}")
        if (
            any(
                not isinstance(function, str) or not function.strip()
                for function in truth + ranking
            )
            or len(truth) != len(set(truth))
        ):
            raise ValueError(f"invalid evaluation functions at index {index}")
        identity_mode = item.get("identity_mode")
        truth_locations = item.get("ground_truth_locations")
        ranking_locations = item.get("ranking_locations")
        if identity_mode not in {"none", "source_range"} or not all(
            isinstance(locations, list)
            for locations in (truth_locations, ranking_locations)
        ):
            raise ValueError(f"invalid evaluation identity mode at index {index}")
        truth_keys = []
        ranking_keys = []
        for location_index, location in enumerate(truth_locations):
            if (
                not isinstance(location, dict)
                or not str(location.get("function") or "").strip()
                or location.get("function") not in truth
            ):
                raise ValueError(f"invalid ground-truth location at index {index}")
            truth_keys.append(
                _method_location_key(location, location_index, "ground-truth")
            )
        for location_index, location in enumerate(ranking_locations):
            if (
                not isinstance(location, dict)
                or location_index >= len(ranking)
                or location.get("function") != ranking[location_index]
            ):
                raise ValueError(f"invalid ranking location at index {index}")
            ranking_keys.append(
                _method_location_key(location, location_index, "evaluation ranking")
            )
        if len(truth_keys) != len(set(truth_keys)) or len(ranking_keys) != len(
            set(ranking_keys)
        ):
            raise ValueError(f"duplicate evaluation source location at index {index}")
        if identity_mode == "source_range" and (
            not truth_locations or len(ranking_locations) != len(ranking)
        ):
            raise ValueError(f"incomplete evaluation source locations at index {index}")
        if identity_mode != "source_range" and (
            truth_locations or ranking_locations
        ):
            raise ValueError(f"unexpected evaluation source locations at index {index}")
        if (
            any(
                not isinstance(rank, int)
                or isinstance(rank, bool)
                or rank <= 0
                for rank in ranks
            )
            or ranks != sorted(set(ranks))
        ):
            raise ValueError(f"invalid evaluation relevant_ranks at index {index}")
        for field in ("top_1", "top_3", "top_5"):
            if not isinstance(item.get(field), bool):
                raise ValueError(f"invalid evaluation {field} at index {index}")
        for field in ("reciprocal_rank", "average_precision"):
            score = item.get(field)
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not 0 <= score <= 1
            ):
                raise ValueError(f"invalid evaluation {field} at index {index}")
        if item["status"] == "OK":
            ok_count += 1
            if not truth or item["error"]:
                raise ValueError(f"invalid evaluated bug at index {index}")
    if ok_count != evaluated:
        raise ValueError("inconsistent evaluated bug statuses")
    return value
