import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict

from mllmfl.domain.refinement_trace import validate_refinement_trace
from mllmfl.infrastructure.io import read_zstd_json


def _artifact_path(value: Any, field: str, base_dir: Path | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {field}")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError(f"unsafe {field}: {value}")
    if base_dir is not None:
        target = (base_dir / Path(*relative.parts)).resolve()
        root = base_dir.resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"{field} escapes its artifact directory: {value}")
        if not target.is_file():
            raise ValueError(f"{field} not found: {value}")
    return value


def _catalog(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("trace suite method catalog must be a non-empty array")
    seen_ids, seen_keys = set(), set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {
            "method_id", "function", "signature", "descriptor",
        }:
            raise ValueError(f"invalid trace suite method at index {index}")
        method_id = item.get("method_id")
        function = item.get("function")
        signature = item.get("signature")
        descriptor = item.get("descriptor")
        key = (function, descriptor)
        if (
            not isinstance(method_id, str)
            or re.fullmatch(r"M[1-9]\d*", method_id) is None
            or method_id in seen_ids
            or not isinstance(function, str)
            or not function.strip()
            or not isinstance(signature, str)
            or signature.rsplit("(", 1)[0] != function
            or not isinstance(descriptor, str)
            or key in seen_keys
        ):
            raise ValueError(f"invalid trace suite method at index {index}")
        seen_ids.add(method_id)
        seen_keys.add(key)
    return value


def validate_trace_suite(
    value: Any, base_dir: Path | None = None
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("trace suite must be a JSON object")
    if (
        set(value) != {
            "schema", "schema_version", "project", "bug",
            "method_catalog_fingerprint", "method_catalog", "test_count", "tests",
        }
        or value.get("schema") != "execution-trace-suite"
        or value.get("schema_version") != 2
    ):
        raise ValueError("unsupported trace suite schema")
    if not all(
        isinstance(value.get(field), str) and value[field].strip()
        for field in ("project", "bug", "method_catalog_fingerprint")
    ) or re.fullmatch(
        r"[0-9a-f]{64}", str(value.get("method_catalog_fingerprint"))
    ) is None:
        raise ValueError("invalid trace suite identity")
    catalog = _catalog(value.get("method_catalog"))
    known_method_ids = {str(item["method_id"]) for item in catalog}
    tests = value.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ValueError("trace suite tests must be a non-empty array")
    seen_tests = set()
    for index, item in enumerate(tests, 1):
        if not isinstance(item, dict) or set(item) != {
            "test_id", "test", "trigger", "trace", "trace_fingerprint",
        }:
            raise ValueError(f"invalid trace suite test at index {index - 1}")
        expected_id = f"T{index}"
        if (
            item.get("test_id") != expected_id
            or item.get("test_id") in seen_tests
            or not isinstance(item.get("test"), str)
            or "::" not in item["test"]
            or str(item.get("trigger")) != str(index)
            or not isinstance(item.get("trace_fingerprint"), str)
            or re.fullmatch(r"[0-9a-f]{64}", item["trace_fingerprint"]) is None
        ):
            raise ValueError(f"invalid trace suite test at index {index - 1}")
        trace_value = _artifact_path(item.get("trace"), "refinement trace", base_dir)
        if not trace_value.endswith(".refinement-trace.json.zst"):
            raise ValueError("invalid refinement trace filename")
        if base_dir is not None:
            trace = validate_refinement_trace(
                read_zstd_json(base_dir / Path(*Path(trace_value).parts))
            )
            trace_method_ids = {str(method[0]) for method in trace["methods"]}
            if (
                trace["project"] != value["project"]
                or trace["test_id"] != expected_id
                or trace["test"] != item["test"]
                or trace["fingerprint"] != item["trace_fingerprint"]
                or trace["method_catalog_fingerprint"]
                != value["method_catalog_fingerprint"]
                or any(
                    method_id not in known_method_ids
                    for method_id in trace_method_ids
                    if method_id.startswith("M")
                )
            ):
                raise ValueError(f"trace suite payload mismatch for {expected_id}")
        seen_tests.add(expected_id)
    if value.get("test_count") != len(tests):
        raise ValueError("inconsistent trace suite test count")
    return value
