"""Validated, reusable failing-test reports for standalone localization."""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict

from dpex.domain.schemas import validate_trace_suite
from dpex.infrastructure.io import read_json, write_json, write_text
from dpex.infrastructure.trace_store import SQLiteTraceTopology


MANIFEST_NAME = "manifest.json"


def failure_cache_dir(cache_root: Path, project: str, bug: str) -> Path:
    return cache_root / "artifacts" / project / f"bug_{bug}" / "failing_tests"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_fingerprint(suite: Dict[str, Any]) -> str:
    payload = {
        "project": suite["project"],
        "bug": suite["bug"],
        "tests": [
            {
                "test_id": item["test_id"],
                "test": item["test"],
                "trace_fingerprint": item["trace_fingerprint"],
            }
            for item in suite["tests"]
        ],
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def validate_failure_cache(
    value: Any, directory: Path, project: str, bug: str,
) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "schema_version", "project", "bug", "source_fingerprint",
        "test_count", "tests",
    }:
        raise ValueError("invalid failing-test cache manifest")
    if (
        value.get("schema") != "failing-test-evidence-cache"
        or value.get("schema_version") != 1
        or value.get("project") != project
        or value.get("bug") != bug
        or re.fullmatch(r"[0-9a-f]{64}", str(value.get("source_fingerprint")))
        is None
    ):
        raise ValueError("invalid failing-test cache identity")
    tests = value.get("tests")
    if (
        not isinstance(tests, list) or not tests
        or value.get("test_count") != len(tests)
    ):
        raise ValueError("invalid failing-test cache tests")
    seen_ids, seen_tests = set(), set()
    for index, item in enumerate(tests, 1):
        if not isinstance(item, dict) or set(item) != {
            "test_id", "test", "file", "sha256",
        }:
            raise ValueError(f"invalid failing-test cache entry at index {index - 1}")
        relative = PurePosixPath(str(item.get("file") or ""))
        if (
            item.get("test_id") != f"T{index}"
            or item.get("test_id") in seen_ids
            or not isinstance(item.get("test"), str)
            or "::" not in item["test"]
            or item["test"] in seen_tests
            or relative.is_absolute()
            or len(relative.parts) != 1
            or relative.name != f"T{index}.txt"
            or re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256"))) is None
        ):
            raise ValueError(f"invalid failing-test cache entry at index {index - 1}")
        report = directory / relative.name
        try:
            content = report.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as error:
            raise ValueError(f"cannot read failing-test report {report}: {error}") from error
        if _sha256_text(content) != item["sha256"]:
            raise ValueError(f"failing-test report hash mismatch: {report}")
        seen_ids.add(item["test_id"])
        seen_tests.add(item["test"])
    return value


def load_failure_cache(
    cache_root: Path, project: str, bug: str,
) -> Dict[str, Any] | None:
    directory = failure_cache_dir(cache_root, project, bug)
    manifest = directory / MANIFEST_NAME
    if not manifest.is_file():
        return None
    return validate_failure_cache(read_json(manifest), directory, project, bug)


def build_failure_cache(
    trace_bug_dir: Path, cache_root: Path, project: str, bug: str,
) -> Dict[str, Any]:
    suite_path = trace_bug_dir / "trace_suite.json"
    suite = validate_trace_suite(read_json(suite_path), base_dir=trace_bug_dir)
    if suite["project"] != project or suite["bug"] != bug:
        raise ValueError("trace suite identity does not match failing-test cache")
    source_fingerprint = _source_fingerprint(suite)
    try:
        cached = load_failure_cache(cache_root, project, bug)
    except ValueError:
        cached = None
    if cached is not None and cached["source_fingerprint"] == source_fingerprint:
        return {**cached, "cache_hit": True}

    directory = failure_cache_dir(cache_root, project, bug)
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    expected_files = set()
    for item in suite["tests"]:
        test_id = str(item["test_id"])
        relative = Path(str(item["trace"]))
        with SQLiteTraceTopology.open(trace_bug_dir / relative) as topology:
            failure = topology.trace["failure"]
        content = "\n".join([
            f"Failing test: {item['test']}",
            "",
            "Error stack:",
            str(failure["error_stack"]) or "(empty)",
            "",
            "Test output:",
            str(failure["test_output"]) or "(empty)",
            "",
        ])
        filename = f"{test_id}.txt"
        write_text(directory / filename, content)
        expected_files.add(filename)
        entries.append({
            "test_id": test_id,
            "test": str(item["test"]),
            "file": filename,
            "sha256": _sha256_text(content),
        })
    for path in directory.glob("T*.txt"):
        if path.name not in expected_files:
            path.unlink()
    manifest = {
        "schema": "failing-test-evidence-cache",
        "schema_version": 1,
        "project": project,
        "bug": bug,
        "source_fingerprint": source_fingerprint,
        "test_count": len(entries),
        "tests": entries,
    }
    validate_failure_cache(manifest, directory, project, bug)
    write_json(directory / MANIFEST_NAME, manifest)
    return {**manifest, "cache_hit": False}
