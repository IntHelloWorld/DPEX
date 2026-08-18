from pathlib import Path, PurePosixPath
from typing import Any, Dict


def _artifact_path(value: Any, field: str, base_dir: Path | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid UML segment {field}")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError(f"unsafe UML segment {field}: {value}")
    if base_dir is not None:
        target = (base_dir / Path(*relative.parts)).resolve()
        root = base_dir.resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"UML segment {field} escapes trigger directory: {value}")
        if not target.is_file():
            raise ValueError(f"UML segment {field} not found: {value}")
    return value


def validate_uml_index(value: Any, base_dir: Path | None = None) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("UML index must be a JSON object")
    if value.get("schema") != "execution-uml-index" or value.get("schema_version") != 2:
        raise ValueError("unsupported UML index schema")
    if value.get("source_schema") != "fullchain-execution":
        raise ValueError("invalid UML index source schema")
    if value.get("strategy") != "test-root-direct-invocation-subtrees":
        raise ValueError("invalid UML index strategy")
    if not isinstance(value.get("slice_applied"), bool):
        raise ValueError("invalid UML index slice_applied")
    test = value.get("test")
    if not isinstance(test, dict) or not all(
        isinstance(test.get(field), str) and test[field].strip()
        for field in ("class", "method")
    ):
        raise ValueError("invalid UML index test")
    if not isinstance(value.get("root_invocation_id"), int) or value["root_invocation_id"] <= 0:
        raise ValueError("invalid UML index root invocation")
    segments = value.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("UML index segments must be a non-empty array")
    seen_ids, seen_invocations = set(), set()
    total_calls = 0
    previous_order: tuple[int, int] | None = None
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            raise ValueError(f"invalid UML segment at index {index - 1}")
        if segment.get("ordinal") != index:
            raise ValueError(f"non-contiguous UML segment ordinal at index {index - 1}")
        diagram_id = segment.get("diagram_id")
        if not isinstance(diagram_id, str) or not diagram_id.strip():
            raise ValueError(f"invalid UML segment diagram_id at index {index - 1}")
        if diagram_id in seen_ids:
            raise ValueError(f"duplicate UML segment diagram_id: {diagram_id}")
        seen_ids.add(diagram_id)
        invocation_id = segment.get("invocation_id")
        if not isinstance(invocation_id, int) or invocation_id <= 0:
            raise ValueError(f"invalid UML segment invocation_id at index {index - 1}")
        if invocation_id in seen_invocations:
            raise ValueError(f"duplicate UML segment invocation_id: {invocation_id}")
        seen_invocations.add(invocation_id)
        enter_seq = segment.get("enter_seq")
        if not isinstance(enter_seq, int) or enter_seq < 0:
            raise ValueError(f"invalid UML segment enter_seq at index {index - 1}")
        order = (enter_seq, invocation_id)
        if previous_order is not None and order < previous_order:
            raise ValueError(f"out-of-order UML segment at index {index - 1}")
        previous_order = order
        if not isinstance(segment.get("function"), str) or not segment["function"].strip():
            raise ValueError(f"invalid UML segment function at index {index - 1}")
        if not isinstance(segment.get("descriptor"), str) or not isinstance(
            segment.get("signature"), str
        ):
            raise ValueError(f"invalid UML segment signature at index {index - 1}")
        method_signatures = segment.get("method_signatures")
        if (
            not isinstance(method_signatures, list)
            or not method_signatures
            or not all(isinstance(item, str) and item.strip() for item in method_signatures)
            or len(method_signatures) != len(set(method_signatures))
        ):
            raise ValueError(f"invalid UML segment method_signatures at index {index - 1}")
        call_count = segment.get("call_count")
        displayed = segment.get("displayed_call_count")
        if (
            not isinstance(call_count, int)
            or not isinstance(displayed, int)
            or call_count <= 0
            or displayed <= 0
            or displayed > call_count
        ):
            raise ValueError(f"invalid UML segment call counts at index {index - 1}")
        total_calls += call_count
        origin_line = segment.get("origin_test_line")
        exit_seq = segment.get("exit_seq")
        if not isinstance(origin_line, int) or origin_line < 0:
            raise ValueError(f"invalid UML segment origin_test_line at index {index - 1}")
        if not isinstance(exit_seq, int) or exit_seq < enter_seq:
            raise ValueError(f"invalid UML segment exit_seq at index {index - 1}")
        if segment.get("exit_type") not in {"RETURN", "THROW"}:
            raise ValueError(f"invalid UML segment exit_type at index {index - 1}")
        puml = _artifact_path(segment.get("puml"), "puml", base_dir)
        image = _artifact_path(segment.get("image"), "image", base_dir)
        if not puml.endswith(".puml") or not image.endswith(".png"):
            raise ValueError(f"invalid UML segment file extension at index {index - 1}")
    partitioned = value.get("partitioned_call_count")
    source = value.get("source_call_count")
    excluded = value.get("excluded_call_count")
    if not all(isinstance(item, int) and item >= 0 for item in (partitioned, source, excluded)):
        raise ValueError("invalid UML index call counts")
    if partitioned != total_calls or source != partitioned + excluded:
        raise ValueError("inconsistent UML index call counts")
    if value.get("segment_count") != len(segments):
        raise ValueError("inconsistent UML index segment count")
    return value


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
    if value.get("schema") != "fault-localization" or value.get("schema_version") not in {1, 2, 3}:
        raise ValueError("unsupported localization schema")
    if value.get("schema_version") in {2, 3}:
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
    ranking = value.get("ranking")
    if not isinstance(ranking, list):
        raise ValueError("ranking must be an array")
    seen = set()
    seen_signatures = set()
    for index, item in enumerate(ranking):
        if not isinstance(item, dict) or not str(item.get("function") or "").strip():
            raise ValueError(f"invalid ranking at index {index}")
        function = str(item["function"])
        if value.get("schema_version") != 3 and function in seen:
            raise ValueError(f"duplicate ranking function: {function}")
        seen.add(function)
        if value.get("schema_version") == 3:
            signature = item.get("signature")
            if not isinstance(signature, str) or not signature.strip():
                raise ValueError(f"invalid ranking signature at index {index}")
            if signature in seen_signatures:
                raise ValueError(f"duplicate ranking signature: {signature}")
            if signature.rsplit("(", 1)[0] != function:
                raise ValueError(f"ranking signature does not match function at index {index}")
            seen_signatures.add(signature)
        try:
            rank = int(item.get("rank") or 0)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"invalid ranking rank at index {index}") from error
        if rank != index + 1:
            raise ValueError(f"non-contiguous rank at index {index}")
    return value


def validate_aggregate(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("aggregate artifact must be a JSON object")
    if value.get("schema") != "fault-localization-aggregate" or value.get(
        "schema_version"
    ) != 1:
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
        if function in seen:
            raise ValueError(f"duplicate aggregate function: {function}")
        seen.add(function)
        if item.get("rank") != index:
            raise ValueError(f"non-contiguous aggregate rank at index {index - 1}")
    return value


def validate_evaluation(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("evaluation artifact must be a JSON object")
    if value.get("schema") != "fault-localization-evaluation" or value.get(
        "schema_version"
    ) != 1:
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
        ) or len(truth) != len(set(truth)) or len(ranking) != len(set(ranking)):
            raise ValueError(f"invalid evaluation functions at index {index}")
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
