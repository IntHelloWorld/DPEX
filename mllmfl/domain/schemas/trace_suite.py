import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict


def _artifact_path(
    value: Any, field: str, base_dir: Path | None
) -> str:
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
        if not isinstance(item, dict):
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


def validate_trace_index(
    value: Any, base_dir: Path | None = None
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("trace index must be a JSON object")
    if (
        value.get("schema") != "execution-trace-index"
        or value.get("schema_version") != 2
        or set(value) != {
            "schema", "schema_version", "test_id", "test",
            "method_catalog_fingerprint", "execution", "default_execution",
            "method_count", "occurrence_count", "default_occurrence_count",
            "folded_occurrence_count", "methods",
        }
    ):
        raise ValueError("unsupported trace index schema")
    test_id = value.get("test_id")
    test = value.get("test")
    fingerprint = value.get("method_catalog_fingerprint")
    if (
        not isinstance(test_id, str)
        or re.fullmatch(r"T[1-9]\d*", test_id) is None
        or not isinstance(test, str)
        or "::" not in test
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
    ):
        raise ValueError("invalid trace index identity")
    execution_path = _artifact_path(
        value.get("execution"), "trace index execution", base_dir
    )
    default_execution_path = _artifact_path(
        value.get("default_execution"),
        "trace index default execution",
        base_dir,
    )
    if (
        execution_path != "execution.json"
        or default_execution_path not in {
            "execution.json", "execution_assertion_pruned.json",
        }
    ):
        raise ValueError("invalid trace index execution selection")
    methods = value.get("methods")
    if not isinstance(methods, list):
        raise ValueError("trace index methods must be an array")
    seen_methods, seen_invocations = set(), set()
    occurrence_total = 0
    for method_index, method in enumerate(methods):
        if not isinstance(method, dict):
            raise ValueError(f"invalid trace index method at index {method_index}")
        method_id = method.get("method_id")
        occurrences = method.get("occurrences")
        if (
            not isinstance(method_id, str)
            or re.fullmatch(r"M[1-9]\d*", method_id) is None
            or method_id in seen_methods
            or not isinstance(occurrences, list)
            or not occurrences
        ):
            raise ValueError(f"invalid trace index method at index {method_index}")
        seen_methods.add(method_id)
        previous_invocation_id = 0
        for occurrence in occurrences:
            if not isinstance(occurrence, dict):
                raise ValueError("invalid trace occurrence")
            invocation_id = occurrence.get("invocation_id")
            caller_signature = occurrence.get("caller_signature")
            fold_id = occurrence.get("successful_assertion_fold_id")
            if (
                set(occurrence) != {
                    "invocation_id", "successful_assertion_fold_id",
                    "caller_signature",
                }
                or not isinstance(invocation_id, int)
                or isinstance(invocation_id, bool)
                or invocation_id <= 0
                or invocation_id <= previous_invocation_id
                or invocation_id in seen_invocations
                or not isinstance(caller_signature, str)
                or not caller_signature.strip()
                or (
                    fold_id is not None
                    and (
                        not isinstance(fold_id, str)
                        or re.fullmatch(r"AF\d{3,}", fold_id) is None
                    )
                )
            ):
                raise ValueError("invalid trace occurrence context")
            seen_invocations.add(invocation_id)
            previous_invocation_id = invocation_id
            occurrence_total += 1
    if value.get("method_count") != len(methods):
        raise ValueError("inconsistent trace index method count")
    if value.get("occurrence_count") != occurrence_total:
        raise ValueError("inconsistent trace index occurrence count")
    folded_total = sum(
        1
        for method in methods
        for occurrence in method["occurrences"]
        if occurrence["successful_assertion_fold_id"] is not None
    )
    if (
        value.get("folded_occurrence_count") != folded_total
        or value.get("default_occurrence_count") != occurrence_total - folded_total
        or (folded_total > 0)
        != (default_execution_path == "execution_assertion_pruned.json")
    ):
        raise ValueError("inconsistent trace index folded occurrence counts")
    return value


def validate_trace_suite(
    value: Any, base_dir: Path | None = None
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("trace suite must be a JSON object")
    if (
        value.get("schema") != "execution-trace-suite"
        or value.get("schema_version") != 1
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
        if not isinstance(item, dict):
            raise ValueError(f"invalid trace suite test at index {index - 1}")
        expected_id = f"T{index}"
        if (
            item.get("test_id") != expected_id
            or item.get("test_id") in seen_tests
            or not isinstance(item.get("test"), str)
            or "::" not in item["test"]
            or str(item.get("trigger")) != str(index)
        ):
            raise ValueError(f"invalid trace suite test at index {index - 1}")
        index_value = _artifact_path(
            item.get("trace_index"), "trace suite index", base_dir
        )
        if base_dir is not None:
            index_path = base_dir / Path(*Path(index_value).parts)
            trace_index = validate_trace_index(
                json.loads(index_path.read_text(encoding="utf-8")),
                index_path.parent,
            )
            if (
                trace_index["test_id"] != expected_id
                or trace_index["test"] != item["test"]
                or trace_index["method_catalog_fingerprint"]
                != value["method_catalog_fingerprint"]
                or any(
                    method["method_id"] not in known_method_ids
                    for method in trace_index["methods"]
                )
            ):
                raise ValueError(f"trace suite index mismatch for {expected_id}")
        seen_tests.add(expected_id)
    if value.get("test_count") != len(tests):
        raise ValueError("inconsistent trace suite test count")
    return value
