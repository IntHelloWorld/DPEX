import os
import shutil
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Sequence

from dpex.domain.failure import extract_error_stack
from dpex.domain.schemas import (
    validate_defect_context,
    validate_trace_suite,
)
from dpex.domain.refinement_trace import (
    build_method_catalog_from_keys,
)
from dpex.domain.assertion_source import assertion_ranges
from dpex.infrastructure.defects4j import (
    compile_project,
    defects4j_environment,
    split_test,
    test_classpath,
)
from dpex.infrastructure.io import (
    compress_zstd_file,
    read_json,
    write_compact_json,
    write_csv,
    write_json,
    write_text,
)
from dpex.infrastructure.java_source import find_java_file
from dpex.infrastructure.layout import RunLayout
from dpex.infrastructure.process import run_command_with_zstd_fifo
from dpex.infrastructure.trace_store import (
    METHOD_SUMMARY_NAME,
    TRACE_STORE_ARCHIVE_NAME,
    TRACE_STORE_NAME,
    archive_final_trace_store,
    ensure_trace_store,
    final_trace_artifact_path,
    finalize_trace_store,
    raw_events_to_degraded_store,
    raw_events_to_store,
    read_available_trace_summary,
    trace_store_assertion_folding_strategy,
    validate_method_summary,
)


TRACE_CAPTURE_FIELDS = {
    "capture_values",
    "value_string_edge_chars",
    "value_container_edge_items",
    "value_nested_container_edge_items",
    "value_max_depth",
    "value_max_arguments",
}
TRACE_OPTIONAL_FIELDS = {
    "degradation_raw_size_bytes",
    "retry_without_values_on_timeout",
    "fold_successful_assertions",
}
TRACE_STORAGE_FIELDS = {"archive_final_sqlite"}
DEFAULT_DEGRADATION_RAW_SIZE_BYTES = 256 * 1024 * 1024
DEFAULT_RETRY_WITHOUT_VALUES_ON_TIMEOUT = True
DEFAULT_FOLD_SUCCESSFUL_ASSERTIONS = True
TRACE_PROVENANCE_NAME = "trace_provenance.json"


class TraceCaptureTimeout(RuntimeError):
    """The instrumented test process exceeded its capture deadline."""


def load_trace_configuration(config_path: Path) -> Dict[str, Any]:
    config = read_json(config_path)
    trace_config = config.get("trace") if isinstance(config, dict) else None
    if (
        not isinstance(trace_config, dict)
        or not TRACE_CAPTURE_FIELDS.issubset(trace_config)
        or set(trace_config) - TRACE_CAPTURE_FIELDS - TRACE_OPTIONAL_FIELDS \
        - TRACE_STORAGE_FIELDS
    ):
        raise ValueError(
            "trace configuration must contain: "
            + ", ".join(sorted(TRACE_CAPTURE_FIELDS))
            + "; optional: degradation_raw_size_bytes, "
            "retry_without_values_on_timeout, fold_successful_assertions, "
            "archive_final_sqlite"
        )
    if not isinstance(trace_config["capture_values"], bool):
        raise ValueError("trace.capture_values must be boolean")
    for field in TRACE_CAPTURE_FIELDS - {"capture_values", "value_max_depth"}:
        value = trace_config[field]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"trace.{field} must be a positive integer")
    depth = trace_config["value_max_depth"]
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
        raise ValueError("trace.value_max_depth must be a non-negative integer")
    threshold = trace_config.get(
        "degradation_raw_size_bytes", DEFAULT_DEGRADATION_RAW_SIZE_BYTES
    )
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold <= 0:
        raise ValueError(
            "trace.degradation_raw_size_bytes must be a positive integer"
        )
    retry_without_values = trace_config.get(
        "retry_without_values_on_timeout",
        DEFAULT_RETRY_WITHOUT_VALUES_ON_TIMEOUT,
    )
    if not isinstance(retry_without_values, bool):
        raise ValueError(
            "trace.retry_without_values_on_timeout must be boolean"
        )
    archive_final_sqlite = trace_config.get("archive_final_sqlite", False)
    if not isinstance(archive_final_sqlite, bool):
        raise ValueError("trace.archive_final_sqlite must be boolean")
    fold_assertions = trace_config.get(
        "fold_successful_assertions", DEFAULT_FOLD_SUCCESSFUL_ASSERTIONS
    )
    if not isinstance(fold_assertions, bool):
        raise ValueError("trace.fold_successful_assertions must be boolean")
    result = {
        field: trace_config[field] for field in TRACE_CAPTURE_FIELDS
    } | {
        "degradation_raw_size_bytes": threshold,
        "retry_without_values_on_timeout": retry_without_values,
    }
    if "fold_successful_assertions" in trace_config:
        result["fold_successful_assertions"] = fold_assertions
    return result


def load_trace_archive_configuration(config_path: Path) -> bool:
    config = read_json(config_path)
    trace_config = config.get("trace") if isinstance(config, dict) else None
    if not isinstance(trace_config, dict):
        raise ValueError("trace configuration must be an object")
    value = trace_config.get("archive_final_sqlite", False)
    if not isinstance(value, bool):
        raise ValueError("trace.archive_final_sqlite must be boolean")
    return value


def _capture_configuration(trace_config: Dict[str, Any]) -> Dict[str, Any]:
    return {field: trace_config[field] for field in TRACE_CAPTURE_FIELDS}


def _normalized_trace_configuration(value: Dict[str, Any]) -> Dict[str, Any]:
    """Add policy defaults when reading summaries written by older versions."""
    result = dict(value)
    result.setdefault(
        "retry_without_values_on_timeout",
        DEFAULT_RETRY_WITHOUT_VALUES_ON_TIMEOUT,
    )
    return result


TRACE_SUITE_SUMMARY_NAME = "trace_suite_summary.json"


def _trace_suite_summary(
    bug_dir: Path,
    suite: Dict[str, Any],
    expected_trace_config: Dict[str, Any] | None,
) -> tuple[Dict[str, Any], dict[str, int]]:
    """Load tiny reuse metadata without opening any final trace payload."""
    path = bug_dir / TRACE_SUITE_SUMMARY_NAME
    if not path.is_file():
        raise ValueError("trace suite summary not found")
    value = read_json(path)
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema",
            "schema_version",
            "method_catalog_fingerprint",
            "trace_config",
            "tests",
        }
        or value.get("schema") != "execution-trace-suite-summary"
        or value.get("schema_version") not in {2, 3}
        or value.get("method_catalog_fingerprint")
        != suite["method_catalog_fingerprint"]
        or not isinstance(value.get("trace_config"), dict)
        or frozenset(value["trace_config"]) not in {
            frozenset(TRACE_CAPTURE_FIELDS | {"degradation_raw_size_bytes"}),
            frozenset(TRACE_CAPTURE_FIELDS | TRACE_OPTIONAL_FIELDS),
            frozenset(
                TRACE_CAPTURE_FIELDS
                | {"degradation_raw_size_bytes", "retry_without_values_on_timeout"}
            ),
        }
        or not isinstance(value.get("tests"), list)
    ):
        raise ValueError("invalid trace suite summary")
    by_test: dict[str, int] = {}
    suite_tests = {str(item["test_id"]): item for item in suite["tests"]}
    for item in value["tests"]:
        expected_fields = (
            {"test_id", "trace_fingerprint", "call_count"}
            if value["schema_version"] == 2 else {
                "test_id", "trace_fingerprint", "call_count",
                "capture_values", "capture_attempt_count",
                "fallback_reason", "storage_mode",
            }
        )
        if (
            not isinstance(item, dict)
            or set(item) != expected_fields
            or item.get("test_id") not in suite_tests
            or item.get("trace_fingerprint")
            != suite_tests[item["test_id"]]["trace_fingerprint"]
            or not isinstance(item.get("call_count"), int)
            or isinstance(item.get("call_count"), bool)
            or item["call_count"] <= 0
            or item["test_id"] in by_test
        ):
            raise ValueError("invalid trace suite summary test")
        if value["schema_version"] == 3 and (
            not isinstance(item["capture_values"], bool)
            or not isinstance(item["capture_attempt_count"], int)
            or isinstance(item["capture_attempt_count"], bool)
            or item["capture_attempt_count"] <= 0
            or item["fallback_reason"] not in {None, "capture_timeout"}
            or item["storage_mode"] not in {"normal", "degraded", "unknown"}
        ):
            raise ValueError("invalid trace suite summary provenance")
        by_test[str(item["test_id"])] = int(item["call_count"])
    if set(by_test) != set(suite_tests):
        raise ValueError("trace suite summary tests do not match suite")
    return _normalized_trace_configuration(value["trace_config"]), by_test


def _write_trace_provenance(
    output: Path,
    *,
    requested_capture: Dict[str, Any],
    effective_capture: Dict[str, Any],
    capture_attempt_count: int,
    fallback_reason: str | None,
    storage_mode: str,
) -> None:
    write_compact_json(output / TRACE_PROVENANCE_NAME, {
        "schema": "trace-collection-provenance",
        "schema_version": 1,
        "requested_capture": dict(requested_capture),
        "effective_capture": dict(effective_capture),
        "capture_attempt_count": capture_attempt_count,
        "fallback_reason": fallback_reason,
        "storage_mode": storage_mode,
    })


def _read_trace_provenance(output: Path) -> Dict[str, Any] | None:
    path = output / TRACE_PROVENANCE_NAME
    if not path.is_file():
        return None
    value = read_json(path)
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema", "schema_version", "requested_capture",
            "effective_capture", "capture_attempt_count",
            "fallback_reason", "storage_mode",
        }
        or value.get("schema") != "trace-collection-provenance"
        or value.get("schema_version") != 1
        or not isinstance(value.get("requested_capture"), dict)
        or not isinstance(value.get("effective_capture"), dict)
        or not isinstance(value.get("capture_attempt_count"), int)
        or isinstance(value.get("capture_attempt_count"), bool)
        or value["capture_attempt_count"] <= 0
        or value.get("fallback_reason") not in {None, "capture_timeout"}
        or value.get("storage_mode") not in {"normal", "degraded"}
    ):
        raise ValueError("invalid trace collection provenance")
    return value


def _capture_timed_out(log_dir: Path) -> bool:
    for name in ("trace.stderr.log", "trace.stdout.log"):
        path = log_dir / name
        if path.is_file() and "[TIMEOUT]" in path.read_text(
            encoding="utf-8", errors="replace"
        ):
            return True
    return False


def _reusable_capture(
    output: Path,
    summary_capture: Dict[str, Any],
    requested_capture: Dict[str, Any],
) -> bool:
    if summary_capture == requested_capture:
        return True
    provenance = _read_trace_provenance(output)
    return bool(
        provenance is not None
        and provenance["requested_capture"] == requested_capture
        and provenance["effective_capture"] == summary_capture
        and provenance["fallback_reason"] == "capture_timeout"
    )


def archive_failed_raw_trace(output: Path) -> Path | None:
    raw_path = output / "raw_events.jsonl"
    target = output / "raw_events.failed.jsonl.zst"
    compressed = output / "raw_events.jsonl.zst"
    if compressed.is_file():
        compressed.replace(target)
        return target
    if raw_path.is_file():
        compress_zstd_file(raw_path, target, level=1)
        raw_path.unlink()
        return target
    return None


def convert_existing_raw_trace(
    workspace: Path,
    output: Path,
    test: str,
    project: str,
    capture_config: Dict[str, Any],
    log_dir: Path,
    retain_debug_artifacts: bool,
) -> Dict[str, Any]:
    """Recover a completed raw trace without running Java again."""
    compressed = output / "raw_events.jsonl.zst"
    plain = output / "raw_events.jsonl"
    archived = output / "raw_events.failed.jsonl.zst"
    raw_path = next(
        (path for path in (compressed, plain, archived) if path.is_file()),
        plain,
    )
    if not raw_path.is_file():
        raise ValueError("completed raw trace is missing")
    test_class, test_method = split_test(test)
    source = find_java_file(workspace, test_class)
    configured_ranges = assertion_range_argument(
        source, test_class, test_method
    )
    stdout_path = log_dir / "trace.stdout.log"
    stderr_path = log_dir / "trace.stderr.log"
    if not stdout_path.is_file() or not stderr_path.is_file():
        raise ValueError(
            "raw trace recovery requires the original trace stdout and stderr logs"
        )
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    test_output = "\n".join(
        part.rstrip() for part in (stdout, stderr) if part
    )
    defect_context = {
        "schema": "defect-context",
        "schema_version": 1,
        "test": test,
        "error_stack": extract_error_stack(stdout + "\n" + stderr),
        "test_output": test_output,
    }
    validate_defect_context(defect_context)
    instrumentation = {
        "schema": "assertion-instrumentation",
        "schema_version": 1,
        "configured_ranges": configured_ranges,
    }
    requested_capture = _capture_configuration(capture_config)
    common = dict(
        project=project, test=test, test_class=test_class,
        test_method=test_method, process_exit_code=None,
        assertion_instrumentation=instrumentation,
        defect_context=defect_context, capture_config=requested_capture,
    )
    threshold = int(capture_config["degradation_raw_size_bytes"])
    degraded = raw_path.stat().st_size > threshold
    if degraded:
        summary = raw_events_to_degraded_store(
            raw_path, output / TRACE_STORE_NAME, output / METHOD_SUMMARY_NAME,
            threshold_bytes=threshold, **common,
        )
    else:
        summary = raw_events_to_store(
            raw_path, output / TRACE_STORE_NAME, output / METHOD_SUMMARY_NAME,
            fold_assertions=bool(
                capture_config.get("fold_successful_assertions", True)
            ),
            **common,
        )
    if not retain_debug_artifacts:
        raw_path.unlink(missing_ok=True)
    return {
        "call_count": int(summary["call_count"]),
        "storage_mode": "degraded" if degraded else "normal",
    }


def _suite_only_targets(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
    expected_capture: Dict[str, Any] | None = None,
) -> list[tuple[str, str, str, Path, str, Dict[str, Any]]]:
    """Recover lean trace targets without reopening final trace payloads."""
    bases = (
        sorted(layout.artifacts.glob("*"))
        if not projects or "ALL" in projects
        else [layout.artifacts / project for project in projects]
    )
    result = []
    for base in bases:
        if not base.is_dir():
            continue
        for suite_path in sorted(base.glob("bug_*/trace_suite.json")):
            bug_dir = suite_path.parent
            bug = bug_dir.name.removeprefix("bug_")
            if not bug.isdigit() or (bugs is not None and bug not in bugs):
                continue
            raw_suite = read_json(suite_path)
            if (
                not isinstance(raw_suite, dict)
                or raw_suite.get("schema") != "execution-trace-suite"
                or raw_suite.get("schema_version") != 3
            ):
                # Obsolete suites are intentionally not read by the new path.
                continue
            suite = validate_trace_suite(raw_suite)
            trace_config, call_counts = _trace_suite_summary(
                bug_dir, suite, expected_capture
            )
            for spec in suite["tests"]:
                number = str(spec["trigger"])
                if trigger is not None and trigger != number:
                    continue
                trace_path = bug_dir / Path(*Path(str(spec["trace"])).parts)
                try:
                    artifact_path = final_trace_artifact_path(trace_path)
                except ValueError:
                    raise ValueError(f"refinement trace not found: {spec['trace']}")
                if artifact_path.stat().st_size <= 0:
                    raise ValueError(f"refinement trace is empty: {spec['trace']}")
                trace_value = {
                    "project": suite["project"],
                    "test_id": spec["test_id"],
                    "test": spec["test"],
                    "fingerprint": spec["trace_fingerprint"],
                    "method_catalog_fingerprint": suite[
                        "method_catalog_fingerprint"
                    ],
                    "call_count": call_counts.get(str(spec["test_id"]), 0),
                    "trace_config": trace_config,
                }
                result.append((
                    base.name,
                    bug,
                    number,
                    layout.trigger_dir(base.name, bug, number),
                    str(spec["test"]),
                    trace_value,
                ))
    return result


PROJECT_PREFIX = {
    "Chart": "org.jfree",
    "Cli": "org.apache.commons.cli",
    "Closure": "com.google.javascript",
    "Codec": "org.apache.commons.codec",
    "Collections": "org.apache.commons.collections",
    "Compress": "org.apache.commons.compress",
    "Csv": "org.apache.commons.csv",
    "Gson": "com.google.gson",
    "JacksonCore": "com.fasterxml.jackson.core",
    "JacksonDatabind": "com.fasterxml.jackson.databind",
    "JacksonXml": "com.fasterxml.jackson.dataformat.xml",
    "Jsoup": "org.jsoup",
    "JxPath": "org.apache.commons.jxpath",
    "Lang": "org.apache.commons.lang",
    "Math": "org.apache.commons.math",
    "Mockito": "org.mockito",
    "Time": "org.joda.time",
}


def assertion_range_argument(
    source_path: Path | None, class_name: str, method: str
) -> str:
    if source_path is None:
        return ""
    try:
        ranges = assertion_ranges(
            source_path.read_text(encoding="utf-8", errors="ignore"),
            class_name,
            method,
        )
    except (OSError, UnicodeError, ValueError):
        return ""
    return ";".join(
        f"A{index:03d}:{start_line}-{end_line}"
        for index, (start_line, end_line) in enumerate(ranges, 1)
    )


def build_classpath(workspace: Path, agent_jar: Path, env: Dict[str, str]) -> str:
    paths: List[str] = []
    for relative in (
        "target/classes", "target/test-classes", "build/classes", "build/tests",
        "build/test-classes", "classes", "test-classes", "build", "build-tests",
    ):
        path = workspace / relative
        if path.exists():
            paths.append(str(path))
    paths.extend(value for value in test_classpath(workspace, env).split(os.pathsep) if value)
    paths.append(str(agent_jar))
    return os.pathsep.join(dict.fromkeys(paths))


def classpath_has_class(classpath: str, class_name: str) -> bool:
    relative = class_name.replace(".", "/") + ".class"
    for raw_entry in classpath.split(os.pathsep):
        if not raw_entry:
            continue
        entry = Path(raw_entry)
        if entry.is_dir() and (entry / relative).is_file():
            return True
        if entry.is_file() and zipfile.is_zipfile(entry):
            try:
                with zipfile.ZipFile(entry) as archive:
                    if relative in archive.namelist():
                        return True
            except OSError:
                continue
    return False


def java_xml_compatibility_arguments(
    project: str,
    classpath: str,
    output: Path,
    java_home: str | None,
) -> list[str]:
    """Patch the legacy JxPath DOM LS interface into modular JDKs.

    JxPath's bundled Xerces 2.4.0 contains ``DocumentLS``, but Java 9+ owns
    ``org.w3c.dom.ls`` in ``java.xml`` and the application class loader cannot
    define the additional interface from the classpath.  Patch only that
    missing class rather than the whole Xerces jar, whose other packages clash
    with ``jdk.xml.dom``.
    """
    if project != "JxPath" or not java_home:
        return []
    if not (Path(java_home) / "jmods" / "java.xml.jmod").is_file():
        return []
    member = "org/w3c/dom/ls/DocumentLS.class"
    for entry in classpath.split(os.pathsep):
        jar = Path(entry)
        if not jar.is_file() or not zipfile.is_zipfile(jar):
            continue
        try:
            with zipfile.ZipFile(jar) as archive:
                if member not in archive.namelist():
                    continue
                target = output / "java_xml_patch" / Path(*member.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
                return ["--patch-module", f"java.xml={output / 'java_xml_patch'}"]
        except OSError:
            continue
    return []


def _trace_trigger_once(
    workspace: Path,
    output: Path,
    test: str,
    project: str,
    agent_jar: Path,
    env: Dict[str, str],
    timeout: int,
    log_dir: Path | None = None,
    capture_config: Dict[str, Any] | None = None,
    retain_debug_artifacts: bool = False,
    *,
    force_degraded: bool = False,
) -> Dict[str, Any]:
    if (
        capture_config is not None
        and TRACE_CAPTURE_FIELDS.issubset(capture_config)
        and not set(capture_config) - TRACE_CAPTURE_FIELDS - TRACE_OPTIONAL_FIELDS
    ):
        capture_config = dict(capture_config)
        capture_config.setdefault(
            "degradation_raw_size_bytes", DEFAULT_DEGRADATION_RAW_SIZE_BYTES
        )
        capture_config.setdefault(
            "retry_without_values_on_timeout",
            DEFAULT_RETRY_WITHOUT_VALUES_ON_TIMEOUT,
        )
        capture_config.setdefault(
            "fold_successful_assertions", DEFAULT_FOLD_SUCCESSFUL_ASSERTIONS,
        )
    if capture_config is None or set(capture_config) != (
        TRACE_CAPTURE_FIELDS | TRACE_OPTIONAL_FIELDS
    ):
        raise ValueError("validated trace capture configuration is required")
    requested_capture = _capture_configuration(capture_config)
    capture_values = bool(requested_capture["capture_values"])
    test_class, test_method = split_test(test)
    prefix = PROJECT_PREFIX.get(project, test_class.rsplit(".", 1)[0])
    test_source = find_java_file(workspace, test_class)
    assertion_ranges = assertion_range_argument(
        test_source, test_class, test_method
    )
    output.mkdir(parents=True, exist_ok=True)
    fifo_path = output / "raw_events.fifo"
    raw_path = output / "raw_events.jsonl.zst"
    fifo_path.unlink(missing_ok=True)
    raw_path.unlink(missing_ok=True)
    (output / "raw_events.failed.jsonl.zst").unlink(missing_ok=True)
    (output / "raw_events.jsonl.incomplete.zst").unlink(missing_ok=True)
    agent_args = f"{prefix},class:{test_class}"
    classpath = build_classpath(workspace, agent_jar, env)
    compatibility_arguments = java_xml_compatibility_arguments(
        project, classpath, output, env.get("JAVA_HOME")
    )
    log_dir = log_dir or output
    if not classpath_has_class(classpath, test_class):
        compile_result = compile_project(workspace, env, timeout)
        write_text(log_dir / "trace_compile.stdout.log", compile_result.stdout)
        write_text(log_dir / "trace_compile.stderr.log", compile_result.stderr)
        if compile_result.returncode != 0 or not classpath_has_class(
            classpath, test_class
        ):
            raise RuntimeError(f"cannot prepare compiled test class: {test_class}")
    command = [
        "java", *compatibility_arguments,
        "-Djava.awt.headless=true", f"-Dfltrace.raw.file={fifo_path}",
        f"-Dfltrace.test.class={test_class}", f"-Dfltrace.test.method={test_method}",
        f"-Dfltrace.assert.ranges={assertion_ranges}",
        f"-Dfltrace.capture.values={str(capture_values).lower()}",
        "-Dfltrace.value.string.edge.chars="
        f"{requested_capture['value_string_edge_chars']}",
        "-Dfltrace.value.container.edge.items="
        f"{requested_capture['value_container_edge_items']}",
        "-Dfltrace.value.nested.container.edge.items="
        f"{requested_capture['value_nested_container_edge_items']}",
        f"-Dfltrace.value.max.depth={requested_capture['value_max_depth']}",
        f"-Dfltrace.value.max.arguments={requested_capture['value_max_arguments']}",
        f"-javaagent:{agent_jar}={agent_args}", "-cp", classpath,
        "fltrace.runner.SingleTestRunner", test_class, test_method,
    ]
    result = run_command_with_zstd_fifo(
        command,
        fifo_path=fifo_path,
        target=raw_path,
        cwd=workspace,
        env=env,
        timeout=timeout,
    )
    write_text(log_dir / "trace.stdout.log", result.stdout)
    write_text(log_dir / "trace.stderr.log", result.stderr)
    error_stack = extract_error_stack(result.stdout + "\n" + result.stderr)
    if result.returncode == 124:
        raise TraceCaptureTimeout(f"test process timed out after {timeout}s")
    if result.returncode not in (0, 1):
        raise RuntimeError(f"test process failed: {result.returncode}")
    test_output = "\n".join(
        part.rstrip() for part in (result.stdout, result.stderr) if part
    )
    defect_context = {
        "schema": "defect-context",
        "schema_version": 1,
        "test": test,
        "error_stack": error_stack,
        "test_output": test_output,
    }
    validate_defect_context(defect_context)
    if not raw_path.is_file():
        raise RuntimeError("fullchain agent did not create a compressed raw trace")
    instrumentation = {
        "schema": "assertion-instrumentation",
        "schema_version": 1,
        "configured_ranges": assertion_ranges,
    }
    common = dict(
        project=project, test=test, test_class=test_class,
        test_method=test_method, process_exit_code=result.returncode,
        assertion_instrumentation=instrumentation,
        defect_context=defect_context, capture_config=requested_capture,
    )
    threshold = int(capture_config["degradation_raw_size_bytes"])
    degraded = force_degraded or raw_path.stat().st_size > threshold
    if degraded:
        summary = raw_events_to_degraded_store(
            raw_path, output / TRACE_STORE_NAME, output / METHOD_SUMMARY_NAME,
            threshold_bytes=threshold,
            degradation_reason=(
                "capture_timeout" if force_degraded
                else "raw_trace_size_threshold"
            ),
            **common,
        )
    else:
        summary = raw_events_to_store(
            raw_path, output / TRACE_STORE_NAME, output / METHOD_SUMMARY_NAME,
            fold_assertions=bool(
                capture_config.get("fold_successful_assertions", True)
            ),
            **common,
        )
    if retain_debug_artifacts:
        write_json(output / "defect_context.debug.json", defect_context)
    else:
        raw_path.unlink(missing_ok=True)
    return {
        "call_count": int(summary["call_count"]),
        "storage_mode": "degraded" if degraded else "normal",
    }


def _preserve_capture_attempt(
    output: Path,
    log_dir: Path,
    *,
    attempt_name: str,
) -> None:
    attempt = output / "attempts" / attempt_name
    suffix = 2
    while attempt.exists():
        attempt = output / "attempts" / f"{attempt_name}_{suffix}"
        suffix += 1
    attempt.mkdir(parents=True, exist_ok=True)
    for name in (
        "raw_events.jsonl.zst",
        "raw_events.failed.jsonl.zst",
        "raw_events.jsonl.incomplete.zst",
    ):
        source = output / name
        if source.is_file():
            source.replace(attempt / name)
    for name in ("trace.stdout.log", "trace.stderr.log"):
        source = log_dir / name
        if source.is_file():
            source.replace(attempt / name)


def trace_trigger(
    workspace: Path,
    output: Path,
    test: str,
    project: str,
    agent_jar: Path,
    env: Dict[str, str],
    timeout: int,
    log_dir: Path | None = None,
    capture_config: Dict[str, Any] | None = None,
    retain_debug_artifacts: bool = False,
) -> Dict[str, Any]:
    """Capture one trigger, retrying a capture timeout once without values."""
    if (
        capture_config is not None
        and TRACE_CAPTURE_FIELDS.issubset(capture_config)
        and not set(capture_config) - TRACE_CAPTURE_FIELDS - TRACE_OPTIONAL_FIELDS
    ):
        capture_config = dict(capture_config)
        capture_config.setdefault(
            "degradation_raw_size_bytes", DEFAULT_DEGRADATION_RAW_SIZE_BYTES
        )
        capture_config.setdefault(
            "retry_without_values_on_timeout",
            DEFAULT_RETRY_WITHOUT_VALUES_ON_TIMEOUT,
        )
        capture_config.setdefault(
            "fold_successful_assertions", DEFAULT_FOLD_SUCCESSFUL_ASSERTIONS,
        )
    if capture_config is None or set(capture_config) != (
        TRACE_CAPTURE_FIELDS | TRACE_OPTIONAL_FIELDS
    ):
        raise ValueError("validated trace capture configuration is required")
    requested_capture = _capture_configuration(capture_config)
    actual_log_dir = log_dir or output
    try:
        result = _trace_trigger_once(
            workspace, output, test, project, agent_jar, env, timeout,
            actual_log_dir, capture_config, retain_debug_artifacts,
        )
        _write_trace_provenance(
            output,
            requested_capture=requested_capture,
            effective_capture=requested_capture,
            capture_attempt_count=1,
            fallback_reason=None,
            storage_mode=str(result["storage_mode"]),
        )
        return result
    except TraceCaptureTimeout:
        if (
            not bool(capture_config["retry_without_values_on_timeout"])
            or not requested_capture["capture_values"]
        ):
            raise
        _preserve_capture_attempt(
            output, actual_log_dir, attempt_name="attempt_1_values_timeout"
        )
    fallback_config = dict(capture_config)
    fallback_config["capture_values"] = False
    try:
        result = _trace_trigger_once(
            workspace, output, test, project, agent_jar, env, timeout,
            actual_log_dir, fallback_config, retain_debug_artifacts,
            force_degraded=True,
        )
    except TraceCaptureTimeout:
        _preserve_capture_attempt(
            output,
            actual_log_dir,
            attempt_name="attempt_2_no_values_timeout",
        )
        raise
    effective_capture = _capture_configuration(fallback_config)
    _write_trace_provenance(
        output,
        requested_capture=requested_capture,
        effective_capture=effective_capture,
        capture_attempt_count=2,
        fallback_reason="capture_timeout",
        storage_mode="degraded",
    )
    return result


def _write_trace_suites(
    layout: RunLayout,
    grouped: Dict[tuple[str, str], list[tuple[str, str, str, Path]]],
    *,
    trace_config: Dict[str, Any],
    retain_debug_artifacts: bool,
    archive_final_sqlite: bool = False,
) -> None:
    for (project, bug), items in grouped.items():
        bug_dir = layout.artifacts / project / f"bug_{bug}"
        suite_path = bug_dir / "trace_suite.json"
        try:
            ordered = sorted(items, key=lambda item: int(item[2]))
            summaries = [
                read_available_trace_summary(directory, "")
                for _, _, _, directory in ordered
            ]
            captures = [
                validate_method_summary(summary)["capture"]
                for summary in summaries
            ]
            provenances = []
            requested_capture = _capture_configuration(trace_config)
            for (_, _, _, directory), capture in zip(ordered, captures):
                provenance = _read_trace_provenance(directory)
                if provenance is None:
                    if capture != requested_capture:
                        raise ValueError(
                            "trace capture differs from requested configuration "
                            "without fallback provenance"
                        )
                    provenance = {
                        "requested_capture": dict(capture),
                        "effective_capture": dict(capture),
                        "capture_attempt_count": 1,
                        "fallback_reason": None,
                        "storage_mode": "unknown",
                    }
                elif (
                    provenance["requested_capture"] != requested_capture
                    or provenance["effective_capture"] != capture
                ):
                    raise ValueError(
                        "trace collection provenance does not match capture configuration"
                    )
                provenances.append(provenance)
            method_keys = (
                tuple(str(part) for part in method)
                for summary in summaries
                for method in summary["methods"]
            )
            catalog, method_ids, fingerprint = build_method_catalog_from_keys(
                method_keys
            )
            tests_by_index: dict[int, Dict[str, object]] = {}
            for original_index, ((_, _, number, directory), summary) in enumerate(
                zip(ordered, summaries)
            ):
                test_id = f"T{original_index + 1}"
                summary = validate_method_summary(summary)
                test = str(summary["test"])
                trace_path = bug_dir / "traces" / (
                    f"{test_id}.trace.sqlite3"
                )
                ensure_trace_store(directory, "")
                trace_fingerprint = finalize_trace_store(
                    directory / TRACE_STORE_NAME,
                    trace_path,
                    project=project,
                    test_id=test_id,
                    test=test,
                    method_ids=method_ids,
                    catalog_fingerprint=fingerprint,
                )
                if archive_final_sqlite:
                    archive_final_trace_store(trace_path)
                tests_by_index[original_index] = {
                    "test_id": test_id,
                    "test": test,
                    "trigger": int(number),
                    "trace": trace_path.relative_to(bug_dir).as_posix(),
                    "trace_fingerprint": trace_fingerprint,
                }
            tests = [tests_by_index[index] for index in range(len(ordered))]
            expected_traces = {str(item["trace"]) for item in tests}
            traces_dir = bug_dir / "traces"
            for path in [
                *traces_dir.glob("T*.refinement-trace.json.zst"),
                *traces_dir.glob("T*.trace.sqlite3"),
                *traces_dir.glob("T*.trace.sqlite3.zst"),
            ]:
                relative = path.relative_to(bug_dir).as_posix()
                logical = relative.removesuffix(".zst")
                if logical not in expected_traces:
                    path.unlink()
            suite = {
                "schema": "execution-trace-suite",
                "schema_version": 3,
                "project": project,
                "bug": bug,
                "method_catalog_fingerprint": fingerprint,
                "method_catalog": catalog,
                "test_count": len(tests),
                "tests": tests,
            }
            # The trace writer computes the canonical payload fingerprint while
            # streaming.  Do not defeat the bounded-memory path by loading every
            # newly written trace during suite validation.
            validate_trace_suite(suite)
            write_compact_json(suite_path, suite)
            write_compact_json(bug_dir / TRACE_SUITE_SUMMARY_NAME, {
                "schema": "execution-trace-suite-summary",
                "schema_version": 3,
                "method_catalog_fingerprint": fingerprint,
                "trace_config": dict(trace_config),
                "tests": [
                    {
                        "test_id": str(item["test_id"]),
                        "trace_fingerprint": str(item["trace_fingerprint"]),
                        "call_count": int(summaries[index]["call_count"]),
                        "capture_values": bool(
                            provenances[index]["effective_capture"]["capture_values"]
                        ),
                        "capture_attempt_count": int(
                            provenances[index]["capture_attempt_count"]
                        ),
                        "fallback_reason": provenances[index]["fallback_reason"],
                        "storage_mode": str(provenances[index]["storage_mode"]),
                    }
                    for index, item in enumerate(tests)
                ],
            })
            if not retain_debug_artifacts:
                for _, _, _, directory in ordered:
                    for name in (
                        TRACE_STORE_NAME,
                        TRACE_STORE_ARCHIVE_NAME,
                        METHOD_SUMMARY_NAME,
                        "collect.json",
                        "trigger_test.txt",
                        "defect_context.json",
                        "assertion_folding.json",
                        "execution.json",
                        "execution_assertion_pruned.json",
                        "trace_index.json",
                        "raw_events.jsonl",
                        "raw_events.jsonl.zst",
                        "raw_events.failed.jsonl.zst",
                        TRACE_PROVENANCE_NAME,
                    ):
                        (directory / name).unlink(missing_ok=True)
                    java_xml_patch = directory / "java_xml_patch"
                    if java_xml_patch.is_dir():
                        shutil.rmtree(java_xml_patch)
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                triggers_dir = bug_dir / "triggers"
                try:
                    triggers_dir.rmdir()
                except OSError:
                    pass
        except Exception as error:
            suite_path.unlink(missing_ok=True)
            (bug_dir / TRACE_SUITE_SUMMARY_NAME).unlink(missing_ok=True)
            write_text(
                layout.stage_log_dir("trace", project, bug) / "suite_error.log",
                str(error) + "\n",
            )
            raise


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
    agent_jar: Path,
    d4j_home: Path | None,
    java_home: Path | None,
    config_path: Path,
    timeout: int,
    force: bool = False,
    retain_debug_artifacts: bool = False,
) -> List[Dict[str, object]]:
    if not agent_jar.is_file():
        raise FileNotFoundError(f"agent jar not found: {agent_jar}")
    env = defects4j_environment(d4j_home, java_home)
    requested_capture = load_trace_configuration(config_path)
    archive_final_sqlite = load_trace_archive_configuration(config_path)
    rows = []
    trigger_items = list(layout.discover_triggers(projects, bugs, trigger))
    suite_targets = _suite_only_targets(
        layout, projects, bugs, trigger, requested_capture
    )
    suite_keys = {(item[0], item[1]) for item in suite_targets}
    retrace_suite_keys = {
        (project, bug)
        for project, bug, _, _, _, normalized in suite_targets
        if force or normalized.get("trace_config") != requested_capture
    }
    trigger_items = [
        item for item in trigger_items
        if (item[0], item[1]) not in suite_keys
    ]
    for project, bug, number, output, test, normalized in suite_targets:
        if (project, bug) not in retrace_suite_keys:
            rows.append({
                "project": project,
                "bug": bug,
                "trigger": number,
                "status": "SKIPPED",
                "call_count": int(normalized["call_count"]),
            })
            continue
        output.mkdir(parents=True, exist_ok=True)
        write_text(output / "trigger_test.txt", test + "\n")
        trigger_items.append((project, bug, number, output))
    grouped = defaultdict(list)
    for item in trigger_items:
        grouped[(item[0], item[1])].append(item)
    if force and trigger is None:
        for project, bug in grouped:
            (layout.artifacts / project / f"bug_{bug}" / "trace_suite.json").unlink(
                missing_ok=True
            )
            (
                layout.artifacts
                / project
                / f"bug_{bug}"
                / TRACE_SUITE_SUMMARY_NAME
            ).unlink(missing_ok=True)
    for project, bug, number, output in trigger_items:
        reuse = (
            (
                (output / TRACE_STORE_NAME).exists()
                and (output / METHOD_SUMMARY_NAME).exists()
            )
            and not force
        )
        if reuse:
            try:
                summary = read_available_trace_summary(output, "")
                if not _reusable_capture(
                    output,
                    summary.get("capture"),
                    _capture_configuration(requested_capture),
                ):
                    reuse = False
                requested_strategy = (
                    "dynamic-successful-assertion-subtree-folding"
                    if requested_capture.get("fold_successful_assertions", True)
                    else "assertion-folding-disabled"
                )
                actual_strategy = trace_store_assertion_folding_strategy(
                    output / TRACE_STORE_NAME
                )
                if actual_strategy != requested_strategy:
                    reuse = False
                if reuse:
                    rows.append({
                        "project": project, "bug": bug, "trigger": number,
                        "status": "SKIPPED", "call_count": summary["call_count"],
                    })
            except Exception as error:
                write_text(
                    layout.stage_log_dir("trace", project, bug, number)
                    / "error.log",
                    str(error) + "\n",
                )
                rows.append({
                    "project": project, "bug": bug, "trigger": number,
                    "status": "ERROR", "call_count": 0,
                })
                continue
            finally:
                summary = None
            if reuse:
                continue
        (output / TRACE_STORE_NAME).unlink(missing_ok=True)
        (output / TRACE_STORE_ARCHIVE_NAME).unlink(missing_ok=True)
        (output / METHOD_SUMMARY_NAME).unlink(missing_ok=True)
        if not retain_debug_artifacts:
            for name in (
                "execution.debug.json.zst",
                "assertion_folding.debug.json",
                "defect_context.debug.json",
            ):
                (output / name).unlink(missing_ok=True)
            if force:
                (output / "raw_events.jsonl.zst").unlink(missing_ok=True)
        test_path = output / "trigger_test.txt"
        try:
            test = test_path.read_text(encoding="utf-8").strip().splitlines()[0]
            log_dir = layout.stage_log_dir("trace", project, bug, number)
            if not force and (
                (output / "raw_events.jsonl").is_file()
                or (output / "raw_events.jsonl.zst").is_file()
                or (output / "raw_events.failed.jsonl.zst").is_file()
            ):
                try:
                    execution = convert_existing_raw_trace(
                        layout.workspace_dir(project, bug),
                        output,
                        test,
                        project,
                        requested_capture,
                        log_dir,
                        retain_debug_artifacts,
                    )
                    _write_trace_provenance(
                        output,
                        requested_capture=_capture_configuration(requested_capture),
                        effective_capture=_capture_configuration(requested_capture),
                        capture_attempt_count=1,
                        fallback_reason=None,
                        storage_mode=str(execution["storage_mode"]),
                    )
                except Exception:
                    if not (
                        requested_capture["retry_without_values_on_timeout"]
                        and requested_capture["capture_values"]
                        and _capture_timed_out(log_dir)
                    ):
                        raise
                    _preserve_capture_attempt(
                        output,
                        log_dir,
                        attempt_name="attempt_1_values_timeout",
                    )
                    fallback_config = dict(requested_capture)
                    fallback_config["capture_values"] = False
                    try:
                        execution = _trace_trigger_once(
                            layout.workspace_dir(project, bug), output, test,
                            project, agent_jar, env, timeout, log_dir,
                            fallback_config, retain_debug_artifacts,
                            force_degraded=True,
                        )
                    except TraceCaptureTimeout:
                        _preserve_capture_attempt(
                            output,
                            log_dir,
                            attempt_name="attempt_2_no_values_timeout",
                        )
                        raise
                    _write_trace_provenance(
                        output,
                        requested_capture=_capture_configuration(requested_capture),
                        effective_capture=_capture_configuration(fallback_config),
                        capture_attempt_count=2,
                        fallback_reason="capture_timeout",
                        storage_mode="degraded",
                    )
            else:
                execution = trace_trigger(
                    layout.workspace_dir(project, bug), output, test, project,
                    agent_jar, env, timeout, log_dir,
                    requested_capture,
                    retain_debug_artifacts,
                )
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "OK",
                         "call_count": execution["call_count"]})
            del execution
        except Exception as error:
            if (
                (output / "raw_events.jsonl").is_file()
                or (output / "raw_events.jsonl.zst").is_file()
            ):
                try:
                    archive_failed_raw_trace(output)
                except Exception as archive_error:
                    error = RuntimeError(f"{error}; raw archive failed: {archive_error}")
            write_text(layout.stage_log_dir("trace", project, bug, number) / "error.log",
                       str(error) + "\n")
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "ERROR", "call_count": 0})
    if trigger is None and rows and all(row["status"] != "ERROR" for row in rows):
        _write_trace_suites(
            layout, grouped, trace_config=requested_capture,
            retain_debug_artifacts=retain_debug_artifacts,
            archive_final_sqlite=archive_final_sqlite,
        )
    write_csv(
        layout.logs / "trace.csv",
        rows,
        ["project", "bug", "trigger", "status", "call_count"],
    )
    return rows
