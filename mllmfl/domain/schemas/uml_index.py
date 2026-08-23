from pathlib import Path
from typing import Any, Dict

from .uml_graph import validate_adaptive_uml_graph
from .uml_recursive import validate_recursive_uml_index
from .uml_support import artifact_path as _artifact_path


def validate_uml_index(value: Any, base_dir: Path | None = None) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("UML index must be a JSON object")
    if value.get("schema") == "execution-uml-graph":
        return validate_adaptive_uml_graph(value, base_dir)
    if value.get("schema") != "execution-uml-index":
        raise ValueError("unsupported UML index schema")
    if value.get("schema_version") in {3, 4}:
        return validate_recursive_uml_index(value, base_dir)
    if value.get("schema_version") != 2:
        raise ValueError("unsupported UML index schema")
    if value.get("source_schema") != "fullchain-execution":
        raise ValueError("invalid UML index source schema")
    strategy = value.get("strategy")
    if strategy not in {
        "test-root-direct-invocation-subtrees",
        "complete-trace-single-diagram",
    }:
        raise ValueError("invalid UML index strategy")
    if not isinstance(value.get("slice_applied"), bool):
        raise ValueError("invalid UML index slice_applied")
    test = value.get("test")
    if not isinstance(test, dict) or not all(
        isinstance(test.get(field), str) and test[field].strip()
        for field in ("class", "method")
    ):
        raise ValueError("invalid UML index test")
    root_invocation_id = value.get("root_invocation_id")
    if not isinstance(root_invocation_id, int) or (
        strategy == "test-root-direct-invocation-subtrees" and root_invocation_id <= 0
    ) or (
        strategy == "complete-trace-single-diagram" and root_invocation_id != 0
    ):
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
    if strategy == "complete-trace-single-diagram" and (
        value["slice_applied"] or len(segments) != 1 or excluded != 0
    ):
        raise ValueError("invalid complete-trace UML index")
    return value
