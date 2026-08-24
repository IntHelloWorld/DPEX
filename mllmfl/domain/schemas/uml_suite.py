import re
from pathlib import Path
from typing import Any, Dict

from .uml_support import artifact_path


def validate_uml_suite(
    value: Any, base_dir: Path | None = None
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("UML suite must be a JSON object")
    if value.get("schema") != "execution-uml-suite" or value.get(
        "schema_version"
    ) != 1:
        raise ValueError("unsupported UML suite schema")
    if not all(
        isinstance(value.get(field), str) and value[field].strip()
        for field in ("project", "bug", "method_catalog_fingerprint")
    ):
        raise ValueError("invalid UML suite identity")
    if re.fullmatch(r"[0-9a-f]{64}", value["method_catalog_fingerprint"]) is None:
        raise ValueError("invalid UML suite method catalog fingerprint")

    catalog = value.get("method_catalog")
    if not isinstance(catalog, list):
        raise ValueError("invalid UML suite method catalog")
    seen_ids, seen_keys = set(), set()
    for index, item in enumerate(catalog):
        if not isinstance(item, dict):
            raise ValueError(f"invalid UML suite method at index {index}")
        method_id = item.get("method_id")
        function = item.get("function")
        signature = item.get("signature")
        descriptor = item.get("descriptor")
        key = (function, descriptor)
        if (
            not isinstance(method_id, str)
            or re.fullmatch(r"M\d{3,}", method_id) is None
            or method_id in seen_ids
            or not isinstance(function, str)
            or not function.strip()
            or not isinstance(signature, str)
            or not signature.strip()
            or signature.rsplit("(", 1)[0] != function
            or not isinstance(descriptor, str)
            or key in seen_keys
        ):
            raise ValueError(f"invalid UML suite method at index {index}")
        seen_ids.add(method_id)
        seen_keys.add(key)

    tests = value.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ValueError("UML suite tests must be a non-empty array")
    seen_test_ids, seen_tests, seen_entries = set(), set(), set()
    for index, item in enumerate(tests, 1):
        if not isinstance(item, dict):
            raise ValueError(f"invalid UML suite test at index {index - 1}")
        test_id = item.get("test_id")
        test = item.get("test")
        trigger = item.get("trigger")
        entry_id = item.get("entry_diagram_id")
        uml_path = item.get("uml")
        expected_id = f"T{index:03d}"
        if (
            test_id != expected_id
            or test_id in seen_test_ids
            or not isinstance(test, str)
            or "::" not in test
            or test in seen_tests
            or str(trigger) != str(index)
            or not isinstance(entry_id, str)
            or re.fullmatch(rf"{test_id}-D\d{{3,}}", entry_id) is None
            or entry_id in seen_entries
        ):
            raise ValueError(f"invalid UML suite test at index {index - 1}")
        artifact_path(uml_path, "UML suite index", base_dir)
        seen_test_ids.add(test_id)
        seen_tests.add(test)
        seen_entries.add(entry_id)
    if value.get("test_count") != len(tests):
        raise ValueError("inconsistent UML suite test count")
    diagram_count = value.get("diagram_count")
    if not isinstance(diagram_count, int) or diagram_count < len(tests):
        raise ValueError("invalid UML suite diagram count")
    return value
