import re
from pathlib import PurePosixPath
from typing import Any, Dict


def _identity(value: Dict[str, Any], context: str) -> None:
    if not all(
        isinstance(value.get(field), str) and value[field].strip()
        for field in ("project", "bug")
    ):
        raise ValueError(f"invalid {context} identity")


def _location(item: Dict[str, Any], index: int, context: str) -> None:
    source_file = item.get("source_file")
    start_line = item.get("start_line")
    end_line = item.get("end_line")
    if not isinstance(source_file, str) or not source_file.strip():
        raise ValueError(f"invalid {context} source_file at index {index}")
    path = PurePosixPath(source_file)
    if path.is_absolute() or ".." in path.parts or "\\" in source_file:
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


def _ranking(value: Any, context: str, *, refined: bool) -> list[Dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{context} ranking must be a non-empty array")
    seen_ids = set()
    seen_locations = set()
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise ValueError(f"invalid {context} ranking at index {index - 1}")
        if item.get("rank") != index:
            raise ValueError(f"non-contiguous {context} rank at index {index - 1}")
        candidate_id = item.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or (
                candidate_id != f"L{index:03d}"
                if not refined
                else re.fullmatch(r"[LN]\d{3,}", candidate_id) is None
            )
            or candidate_id in seen_ids
        ):
            raise ValueError(f"invalid {context} candidate_id at index {index - 1}")
        seen_ids.add(candidate_id)
        if not all(
            isinstance(item.get(field), str) and item[field].strip()
            for field in ("function", "signature")
        ) or str(item["signature"]).rsplit("(", 1)[0] != str(item["function"]):
            raise ValueError(f"invalid {context} method at index {index - 1}")
        _location(item, index - 1, context)
        location = (item["source_file"], item["start_line"], item["end_line"])
        if location in seen_locations:
            raise ValueError(f"duplicate {context} source location: {location}")
        seen_locations.add(location)
        if refined:
            if (
                (
                    item.get("original_rank") is not None
                    and (
                        not isinstance(item["original_rank"], int)
                        or isinstance(item["original_rank"], bool)
                        or item["original_rank"] <= 0
                    )
                )
                or not isinstance(item.get("reason"), str)
                or not item["reason"].strip()
            ):
                raise ValueError(f"invalid refined evidence at index {index - 1}")
    return value


def _aggregate_usage(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("invalid refinement aggregate usage audit")
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError("invalid refinement aggregate usage audit")
        if isinstance(item, dict):
            _aggregate_usage(item)
        elif (
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or item < 0
        ):
            raise ValueError("invalid refinement aggregate usage audit")


def validate_localization_input(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("localization input must be a JSON object")
    if (
        value.get("schema") != "fault-localization-input"
        or value.get("schema_version") != 1
    ):
        raise ValueError("unsupported localization input schema")
    _identity(value, "localization input")
    locator = value.get("locator")
    if (
        not isinstance(locator, dict)
        or not isinstance(locator.get("name"), str)
        or not locator["name"].strip()
    ):
        raise ValueError("invalid locator metadata")
    _ranking(value.get("ranking"), "localization input", refined=False)
    failing_tests = value.get("failing_tests")
    if failing_tests is not None and (
        not isinstance(failing_tests, list)
        or not failing_tests
        or not all(
            isinstance(item, str)
            and item.strip() == item
            and "::" in item
            for item in failing_tests
        )
        or len(failing_tests) != len(set(failing_tests))
    ):
        raise ValueError("invalid localization input failing tests")
    return value


def validate_refinement(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("refinement artifact must be a JSON object")
    if (
        value.get("schema") != "fault-localization-refinement"
        or value.get("schema_version") not in {4, 5, 6}
    ):
        raise ValueError("unsupported refinement schema")
    _identity(value, "refinement")
    if (
        value.get("status") != "OK"
        or not isinstance(value.get("model"), str)
        or not value["model"].strip()
    ):
        raise ValueError("invalid refinement status")
    locator = value.get("locator")
    top_k = value.get("top_k")
    fingerprints = (
        value.get("input_fingerprint"),
        value.get("configuration_fingerprint"),
        value.get("suite_fingerprint"),
    )
    if (
        not isinstance(locator, dict)
        or not isinstance(locator.get("name"), str)
        or not locator["name"].strip()
        or not isinstance(top_k, int)
        or isinstance(top_k, bool)
        or top_k <= 0
        or not all(
            isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
            for item in fingerprints
        )
    ):
        raise ValueError("invalid refinement configuration")
    input_ranking = _ranking(
        value.get("input_ranking"), "refinement input", refined=False
    )
    selected_test_ids: set[str] | None = None
    if value["schema_version"] in {5, 6}:
        tests = value.get("tests")
        test_count = value.get("test_count")
        if (
            not isinstance(tests, list)
            or not tests
            or not isinstance(test_count, int)
            or isinstance(test_count, bool)
            or test_count != len(tests)
            or any(
                not isinstance(item, dict)
                or set(item) != {"test_id", "test"}
                or not isinstance(item["test_id"], str)
                or re.fullmatch(r"T[1-9]\d*", item["test_id"]) is None
                or not isinstance(item["test"], str)
                or item["test"].strip() != item["test"]
                or "::" not in item["test"]
                for item in tests
            )
            or len({item["test_id"] for item in tests}) != len(tests)
            or len({item["test"] for item in tests}) != len(tests)
        ):
            raise ValueError("invalid refinement failing-test selection")
        selected_test_ids = {item["test_id"] for item in tests}
    ranking = _ranking(value.get("ranking"), "refinement", refined=True)
    if len(ranking) > top_k:
        raise ValueError("refinement ranking exceeds top_k")
    input_by_id = {item["candidate_id"]: item for item in input_ranking}
    input_locations = {
        (item["source_file"], item["start_line"], item["end_line"]): item
        for item in input_ranking
    }
    if any(
        (
            item["candidate_id"].startswith("L")
            and (
                item["candidate_id"] not in input_by_id
                or item["original_rank"]
                != input_by_id[item["candidate_id"]]["rank"]
                or (
                    item["source_file"], item["start_line"], item["end_line"]
                ) != (
                    input_by_id[item["candidate_id"]]["source_file"],
                    input_by_id[item["candidate_id"]]["start_line"],
                    input_by_id[item["candidate_id"]]["end_line"],
                )
            )
        )
        or (
            item["candidate_id"].startswith("N")
            and (
                item["original_rank"] is not None
                or (
                    item["source_file"], item["start_line"], item["end_line"]
                ) in input_locations
            )
        )
        for item in ranking
    ):
        raise ValueError("refinement candidate provenance does not match input")
    if not isinstance(value.get("rejected_candidate_ids"), list):
        raise ValueError("invalid rejected candidate audit")
    expected_rejected = [
        item["candidate_id"] for item in input_ranking
        if item["candidate_id"] not in {ranked["candidate_id"] for ranked in ranking}
    ]
    if value["rejected_candidate_ids"] != expected_rejected:
        raise ValueError("inconsistent rejected candidate audit")
    for field in (
        "tool_rounds", "diagram_view_count", "terminal_command_count",
    ):
        if not isinstance(value.get(field), int) or value[field] < 0:
            raise ValueError(f"invalid refinement {field}")
    if value["schema_version"] == 6:
        request_count = value.get("request_count")
        finalization_count = value.get("finalization_attempt_count")
        length_retry_count = value.get("final_length_retry_count")
        finish_reason = value.get("final_finish_reason")
        if (
            not isinstance(request_count, int)
            or isinstance(request_count, bool)
            or request_count <= 0
            or not isinstance(finalization_count, int)
            or isinstance(finalization_count, bool)
            or not 1 <= finalization_count <= request_count
            or not isinstance(length_retry_count, int)
            or isinstance(length_retry_count, bool)
            or not 0 <= length_retry_count < finalization_count
            or not isinstance(value.get("usage"), dict)
            or (
                finish_reason is not None
                and (
                    not isinstance(finish_reason, str)
                    or not finish_reason.strip()
                )
            )
            or "finalization_attempts" in value
        ):
            raise ValueError("invalid refinement aggregate usage audit")
        _aggregate_usage(value["usage"])
    else:
        finalization_attempts = value.get("finalization_attempts")
        if (
            not isinstance(finalization_attempts, list)
            or not finalization_attempts
            or any(
                not isinstance(item, dict)
                or set(item) != {
                    "response_id", "max_tokens", "finish_reason",
                    "content_empty", "usage",
                }
                or not isinstance(item["response_id"], str)
                or not item["response_id"].strip()
                or not isinstance(item["max_tokens"], int)
                or isinstance(item["max_tokens"], bool)
                or item["max_tokens"] <= 0
                or (
                    item["finish_reason"] is not None
                    and (
                        not isinstance(item["finish_reason"], str)
                        or not item["finish_reason"].strip()
                    )
                )
                or not isinstance(item["content_empty"], bool)
                or not isinstance(item["usage"], dict)
                for item in finalization_attempts
            )
            or finalization_attempts[-1]["content_empty"]
        ):
            raise ValueError("invalid refinement finalization audit")
    viewed = value.get("viewed_diagrams")
    inspected = value.get("inspected_candidate_ids")
    method_ids = value.get("candidate_runtime_method_ids")
    invocations = value.get("inspected_invocation_ids")
    queried_methods = value.get("queried_methods")
    queried_method_keys = (
        [
            (item.get("name"), item.get("line"))
            for item in queried_methods
            if isinstance(item, dict)
        ]
        if isinstance(queried_methods, list) else []
    )
    if (
        not isinstance(viewed, list)
        or len(viewed) != len(set(viewed))
        or not all(
            isinstance(item, str)
            and re.fullmatch(
                r"T[1-9]\d*-M[1-9]\d*-C[1-9]\d*-D[1-9]\d*", item
            ) is not None
            and (
                selected_test_ids is None
                or item.split("-", 1)[0] in selected_test_ids
            )
            for item in viewed
        )
        or not isinstance(inspected, list)
        or len(inspected) != len(set(inspected))
        or not all(item in input_by_id for item in inspected)
        or not isinstance(method_ids, dict)
        or not all(
            candidate_id in input_by_id
            and isinstance(method_id, str)
            and re.fullmatch(r"M[1-9]\d*", method_id) is not None
            for candidate_id, method_id in method_ids.items()
        )
        or not all(item in method_ids for item in inspected)
        or not isinstance(invocations, list)
        or len(invocations) != len(set(invocations))
        or not all(
            isinstance(item, str)
            and re.fullmatch(r"T[1-9]\d*-C[1-9]\d*", item) is not None
            and (
                selected_test_ids is None
                or item.split("-", 1)[0] in selected_test_ids
            )
            for item in invocations
        )
        or not isinstance(queried_methods, list)
        or len(queried_method_keys) != len(queried_methods)
        or len(queried_method_keys) != len(set(queried_method_keys))
        or not all(
            set(item) == {"name", "line"}
            and isinstance(item["name"], str)
            and item["name"].strip()
            and isinstance(item["line"], str)
            and re.fullmatch(r"[^\\:]+(?:/[^\\:]+)*\.java:[1-9]\d*", item["line"])
            is not None
            for item in queried_methods
        )
        or len(invocations) > value["tool_rounds"]
        or len(queried_methods) > value["tool_rounds"]
        or value["diagram_view_count"] != len(viewed)
        or value["diagram_view_count"] > value["tool_rounds"]
        or value["terminal_command_count"] > value["tool_rounds"]
    ):
        raise ValueError("invalid refinement inspection audit")
    return value
