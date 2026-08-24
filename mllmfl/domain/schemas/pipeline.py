import re
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


def validate_candidates(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("candidates artifact must be a JSON object")
    if value.get("schema") != "fault-candidates" or value.get("schema_version") != 1:
        raise ValueError("unsupported candidates schema")
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidates must be an array")
    summary_generation = value.get("summary_generation")
    if summary_generation not in {None, "enabled", "disabled"}:
        raise ValueError("invalid candidates summary_generation")
    seen = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("function"), str):
            raise ValueError(f"invalid candidate at index {index}")
        function = candidate["function"].strip()
        if not function:
            raise ValueError(f"empty candidate function at index {index}")
        if function in seen:
            raise ValueError(f"duplicate candidate function: {function}")
        seen.add(function)
        if summary_generation == "disabled" and (
            candidate.get("summary") != ""
            or candidate.get("status") != "SUMMARY_DISABLED"
        ):
            raise ValueError(f"candidate summary is not disabled at index {index}")
    return value


def validate_defect_context(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("defect context must be a JSON object")
    if value.get("schema") != "defect-context" or value.get("schema_version") != 1:
        raise ValueError("unsupported defect context schema")
    if not isinstance(value.get("test"), str) or not value["test"].strip():
        raise ValueError("invalid defect context test")
    if not isinstance(value.get("error_stack"), str) or not value["error_stack"].strip():
        raise ValueError("error stack is unavailable")
    if not isinstance(value.get("test_output"), str):
        raise ValueError("invalid defect context test_output")
    return value


def validate_localization(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("localization artifact must be a JSON object")
    if value.get("schema") != "fault-localization" or value.get("schema_version") not in {1, 2, 3, 4, 5}:
        raise ValueError("unsupported localization schema")
    schema_version = value["schema_version"]
    if schema_version in {2, 3, 4, 5}:
        viewed = value.get("viewed_diagrams")
        counts = (
            value.get("diagram_count"),
            value.get("tool_rounds"),
            value.get("diagram_view_count"),
        )
        if not isinstance(viewed, list) or not all(
            isinstance(item, str) and item.strip() for item in viewed
        ):
            raise ValueError("invalid viewed_diagrams")
        if len(viewed) != len(set(viewed)):
            raise ValueError("duplicate viewed diagram")
        if not all(isinstance(item, int) and item >= 0 for item in counts):
            raise ValueError("invalid localization diagram audit counts")
        if len(viewed) > value["diagram_count"] or len(viewed) > value["diagram_view_count"]:
            raise ValueError("inconsistent localization diagram audit counts")
    if schema_version == 5:
        if (
            not all(
                isinstance(value.get(field), str) and value[field].strip()
                for field in ("project", "bug", "status")
            )
            or not isinstance(value.get("model"), str)
        ):
            raise ValueError("invalid bug-level localization identity")
        top_k = value.get("top_k")
        tests = value.get("tests")
        viewed_test_ids = value.get("viewed_test_ids")
        if not isinstance(top_k, int) or top_k <= 0:
            raise ValueError("invalid bug-level localization top_k")
        if not isinstance(tests, list) or not tests or value.get("test_count") != len(tests):
            raise ValueError("invalid bug-level localization tests")
        expected_test_ids = []
        entry_by_test = {}
        for index, test in enumerate(tests, 1):
            test_id = f"T{index:03d}"
            if (
                not isinstance(test, dict)
                or test.get("test_id") != test_id
                or not isinstance(test.get("test"), str)
                or "::" not in test["test"]
                or not isinstance(test.get("entry_diagram_id"), str)
                or re.fullmatch(rf"{test_id}-D\d{{3,}}", test["entry_diagram_id"])
                is None
            ):
                raise ValueError(f"invalid bug-level localization test at index {index - 1}")
            expected_test_ids.append(test_id)
            entry_by_test[test_id] = test["entry_diagram_id"]
        if (
            not isinstance(viewed_test_ids, list)
            or len(viewed_test_ids) != len(set(viewed_test_ids))
            or not all(item in expected_test_ids for item in viewed_test_ids)
            or any(entry_by_test[item] not in value["viewed_diagrams"] for item in viewed_test_ids)
            or not isinstance(value.get("candidate_count"), int)
            or value["candidate_count"] < 0
        ):
            raise ValueError("invalid bug-level localization test audit")

    ranking = value.get("ranking")
    if not isinstance(ranking, list):
        raise ValueError("ranking must be an array")
    if schema_version == 5 and len(ranking) > value["top_k"]:
        raise ValueError("bug-level localization ranking exceeds top_k")
    seen = set()
    seen_signatures = set()
    seen_locations = set()
    for index, item in enumerate(ranking):
        if not isinstance(item, dict) or not str(item.get("function") or "").strip():
            raise ValueError(f"invalid ranking at index {index}")
        function = str(item["function"])
        if schema_version not in {3, 4, 5} and function in seen:
            raise ValueError(f"duplicate ranking function: {function}")
        seen.add(function)
        if schema_version in {3, 4, 5}:
            signature = item.get("signature")
            if not isinstance(signature, str) or not signature.strip():
                raise ValueError(f"invalid ranking signature at index {index}")
            if schema_version == 3 and signature in seen_signatures:
                raise ValueError(f"duplicate ranking signature: {signature}")
            if signature.rsplit("(", 1)[0] != function:
                raise ValueError(f"ranking signature does not match function at index {index}")
            seen_signatures.add(signature)
        if schema_version in {4, 5}:
            location_key = _method_location_key(item, index, "ranking")
            if location_key in seen_locations:
                raise ValueError(f"duplicate ranking source location: {location_key}")
            seen_locations.add(location_key)
        try:
            rank = int(item.get("rank") or 0)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"invalid ranking rank at index {index}") from error
        if rank != index + 1:
            raise ValueError(f"non-contiguous rank at index {index}")
    interaction_mode = value.get("interaction_mode")
    if schema_version == 5 and "interaction_mode" in value:
        raise ValueError("bug-level localization is image-only and has no interaction mode")
    if schema_version != 5 and interaction_mode not in {None, "text_index", "image_only"}:
        raise ValueError("invalid localization interaction_mode")
    if schema_version == 5 or interaction_mode == "image_only":
        returned_ids = value.get("returned_method_ids")
        dropped_ids = value.get("dropped_invalid_method_ids")
        if (
            not isinstance(returned_ids, list)
            or len(returned_ids) != len(ranking)
            or len(returned_ids) != len(set(returned_ids))
            or not all(
                isinstance(item, str) and re.fullmatch(r"M\d{3,}", item)
                for item in returned_ids
            )
            or not isinstance(dropped_ids, list)
            or not all(isinstance(item, str) for item in dropped_ids)
        ):
            raise ValueError("invalid localization method ID audit")
    elif "returned_method_ids" in value or "dropped_invalid_method_ids" in value:
        raise ValueError("method ID audit requires image_only interaction mode")
    source_dropped = value.get("dropped_unresolved_source_methods")
    if schema_version in {4, 5}:
        if not isinstance(source_dropped, list) or not all(
            isinstance(item, str) and item.strip() for item in source_dropped
        ):
            raise ValueError("invalid unresolved source method audit")
    elif source_dropped is not None:
        raise ValueError("unresolved source method audit requires localization v4 or v5")
    return value


def validate_aggregate(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("aggregate artifact must be a JSON object")
    if value.get("schema") != "fault-localization-aggregate" or value.get(
        "schema_version"
    ) not in {1, 2}:
        raise ValueError("unsupported aggregate schema")
    if not all(
        isinstance(value.get(field), str) and value[field].strip()
        for field in ("project", "bug")
    ):
        raise ValueError("invalid aggregate identity")
    top_k = value.get("top_k")
    valid_count = value.get("valid_trigger_count")
    if not isinstance(top_k, int) or top_k <= 0:
        raise ValueError("invalid aggregate top_k")
    if not isinstance(valid_count, int) or valid_count < 0:
        raise ValueError("invalid aggregate valid_trigger_count")
    status_counts = value.get("status_counts")
    if not isinstance(status_counts, dict) or not all(
        isinstance(status, str)
        and status.strip()
        and isinstance(count, int)
        and count >= 0
        for status, count in status_counts.items()
    ):
        raise ValueError("invalid aggregate status_counts")
    ranking = value.get("ranking")
    if not isinstance(ranking, list) or len(ranking) > top_k:
        raise ValueError("invalid aggregate ranking")
    if bool(valid_count) != bool(ranking):
        raise ValueError("inconsistent aggregate valid result count")
    seen = set()
    for index, item in enumerate(ranking, 1):
        if not isinstance(item, dict):
            raise ValueError(f"invalid aggregate ranking at index {index - 1}")
        function = item.get("function")
        if not isinstance(function, str) or not function.strip():
            raise ValueError(f"invalid aggregate function at index {index - 1}")
        identity = function
        if value.get("schema_version") == 2:
            signature = item.get("signature")
            if (
                not isinstance(signature, str)
                or not signature.strip()
                or signature.rsplit("(", 1)[0] != function
            ):
                raise ValueError(f"invalid aggregate signature at index {index - 1}")
            identity = _method_location_key(item, index - 1, "aggregate")
        if identity in seen:
            raise ValueError(f"duplicate aggregate method: {identity}")
        seen.add(identity)
        if item.get("rank") != index:
            raise ValueError(f"non-contiguous aggregate rank at index {index - 1}")
    return value


def validate_evaluation(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("evaluation artifact must be a JSON object")
    if value.get("schema") != "fault-localization-evaluation" or value.get(
        "schema_version"
    ) not in {1, 2}:
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
        if not isinstance(metric, (int, float)) or isinstance(metric, bool) or not 0 <= metric <= 1:
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
        if any(
            not isinstance(function, str) or not function.strip()
            for function in truth + ranking
        ) or len(truth) != len(set(truth)) or (
            value.get("schema_version") == 1
            and len(ranking) != len(set(ranking))
        ):
            raise ValueError(f"invalid evaluation functions at index {index}")
        if value.get("schema_version") == 2:
            identity_mode = item.get("identity_mode")
            truth_locations = item.get("ground_truth_locations")
            ranking_locations = item.get("ranking_locations")
            if identity_mode not in {"none", "function", "source_range"} or not all(
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
                    raise ValueError(
                        f"invalid ground-truth location at index {index}"
                    )
                truth_keys.append(_method_location_key(
                    location, location_index, "ground-truth"
                ))
            for location_index, location in enumerate(ranking_locations):
                if (
                    not isinstance(location, dict)
                    or location_index >= len(ranking)
                    or location.get("function") != ranking[location_index]
                ):
                    raise ValueError(f"invalid ranking location at index {index}")
                ranking_keys.append(_method_location_key(
                    location, location_index, "evaluation ranking"
                ))
            if len(truth_keys) != len(set(truth_keys)) or len(ranking_keys) != len(
                set(ranking_keys)
            ):
                raise ValueError(f"duplicate evaluation source location at index {index}")
            if identity_mode == "source_range" and (
                not truth_locations
                or len(ranking_locations) != len(ranking)
            ):
                raise ValueError(f"incomplete evaluation source locations at index {index}")
            if identity_mode == "function" and len(ranking) != len(set(ranking)):
                raise ValueError(f"duplicate evaluation function at index {index}")
            if identity_mode != "source_range" and (
                truth_locations or ranking_locations
            ):
                raise ValueError(f"unexpected evaluation source locations at index {index}")
        if (
            any(not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0 for rank in ranks)
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
