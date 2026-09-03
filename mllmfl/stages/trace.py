import os
import sys
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Sequence

from mllmfl.domain.failure import extract_error_stack
from mllmfl.domain.assertion_folding import (
    fold_successful_assertions,
    validate_assertion_folding,
)
from mllmfl.domain.schemas import (
    validate_defect_context,
    validate_trace_index,
    validate_trace_suite,
)
from mllmfl.domain.trace import (
    EXECUTION_SCHEMA,
    build_trace,
    load_events,
    project_execution,
    validate_trace,
)
from mllmfl.domain.assertion_source import assertion_ranges
from mllmfl.infrastructure.defects4j import (
    compile_project,
    defects4j_environment,
    split_test,
    test_classpath,
)
from mllmfl.infrastructure.io import (
    read_json,
    write_compact_json,
    write_csv,
    write_json,
    write_text,
)
from mllmfl.infrastructure.java_source import find_java_file
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.process import run_command
from .trace_index import build_method_catalog, build_trace_index


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


@contextmanager
def compression_recursion_limit(execution: Dict[str, Any]):
    max_depth = max(
        (len(call.get("parent_chain") or []) + 1 for call in execution["calls"]),
        default=1,
    )
    previous = sys.getrecursionlimit()
    required = max(previous, max_depth * 4 + 1000)
    if required != previous:
        sys.setrecursionlimit(required)
    try:
        yield
    finally:
        if required != previous:
            sys.setrecursionlimit(previous)


def trace_trigger(workspace: Path, output: Path, test: str, project: str,
                  agent_jar: Path, env: Dict[str, str], timeout: int,
                  log_dir: Path | None = None, capture_values: bool = True,
                  value_max_chars: int = 120, value_max_items: int = 8,
                  value_max_depth: int = 2,
                  value_max_arguments_chars: int = 480) -> Dict[str, Any]:
    test_class, test_method = split_test(test)
    prefix = PROJECT_PREFIX.get(project, test_class.rsplit(".", 1)[0])
    test_source = find_java_file(workspace, test_class)
    assertion_ranges = assertion_range_argument(
        test_source, test_class, test_method
    )
    output.mkdir(parents=True, exist_ok=True)
    raw_path = output / "raw_events.jsonl"
    if raw_path.exists():
        raw_path.unlink()
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
        "-Djava.awt.headless=true", f"-Dfltrace.raw.file={raw_path}",
        f"-Dfltrace.test.class={test_class}", f"-Dfltrace.test.method={test_method}",
        f"-Dfltrace.assert.ranges={assertion_ranges}",
        f"-Dfltrace.capture.values={str(capture_values).lower()}",
        f"-Dfltrace.value.max.chars={value_max_chars}",
        f"-Dfltrace.value.max.items={value_max_items}",
        f"-Dfltrace.value.max.depth={value_max_depth}",
        f"-Dfltrace.value.max.arguments.chars={value_max_arguments_chars}",
        f"-javaagent:{agent_jar}={agent_args}", "-cp", classpath,
        "fltrace.runner.SingleTestRunner", test_class, test_method,
    ]
    result = run_command(command, cwd=workspace, env=env, timeout=timeout)
    write_text(log_dir / "trace.stdout.log", result.stdout)
    write_text(log_dir / "trace.stderr.log", result.stderr)
    error_stack = extract_error_stack(result.stdout + "\n" + result.stderr)
    collect_path = output / "collect.json"
    if not collect_path.is_file():
        raise RuntimeError("collect.json is required before tracing")
    collect_data = read_json(collect_path)
    test_output = str(collect_data.get("test_output") or "")
    defect_context = {
        "schema": "defect-context",
        "schema_version": 1,
        "test": test,
        "error_stack": error_stack,
        "test_output": test_output,
    }
    validate_defect_context(defect_context)
    write_json(output / "defect_context.json", defect_context)
    if not raw_path.is_file():
        raise RuntimeError("fullchain agent did not create raw_events.jsonl")
    full = build_trace(load_events(raw_path))
    requested_capture = {
        "capture_values": capture_values,
        "value_max_chars": value_max_chars,
        "value_max_items": value_max_items,
        "value_max_depth": value_max_depth,
        "value_max_arguments_chars": value_max_arguments_chars,
    }
    test_start = full.get("test_start") or {}
    if test_start.get("agent_protocol_version") != 4:
        raise ValueError("requested Fullchain agent v4 but raw trace used another protocol")
    if test_start.get("value_capture") != requested_capture:
        raise ValueError(
            "requested value capture configuration does not match agent TEST_START"
        )
    full.update({
        "project": project, "test": {"class": test_class, "method": test_method},
        "process_exit_code": result.returncode,
        "assertion_instrumentation": {
            "schema": "assertion-instrumentation",
            "schema_version": 1,
            "configured_ranges": assertion_ranges,
        },
    })
    execution = project_execution(full, test_class, test_method)
    execution["project"] = project
    assertion_pruned, assertion_folding = fold_successful_assertions(execution)
    write_json(output / "execution.json", execution)
    pruned_path = output / "execution_assertion_pruned.json"
    if assertion_folding["folded_call_count"]:
        write_json(pruned_path, assertion_pruned)
    else:
        pruned_path.unlink(missing_ok=True)
    write_json(output / "assertion_folding.json", assertion_folding)
    return execution


def _write_trace_suites(
    layout: RunLayout,
    grouped: Dict[tuple[str, str], list[tuple[str, str, str, Path]]],
) -> None:
    for (project, bug), items in grouped.items():
        bug_dir = layout.artifacts / project / f"bug_{bug}"
        suite_path = bug_dir / "trace_suite.json"
        try:
            ordered = sorted(items, key=lambda item: int(item[2]))

            def executions():
                for _, _, _, directory in ordered:
                    value = read_json(directory / "execution.json")
                    yield validate_trace(value, EXECUTION_SCHEMA)

            catalog, method_ids, fingerprint = build_method_catalog(executions())
            tests = []
            for index, (_, _, number, directory) in enumerate(ordered, 1):
                test_id = f"T{index}"
                test = (directory / "trigger_test.txt").read_text(
                    encoding="utf-8"
                ).strip()
                execution = validate_trace(
                    read_json(directory / "execution.json"), EXECUTION_SCHEMA
                )
                assertion_folding = validate_assertion_folding(
                    read_json(directory / "assertion_folding.json")
                )
                default_name = (
                    "execution_assertion_pruned.json"
                    if assertion_folding["folded_call_count"]
                    else "execution.json"
                )
                default_execution = validate_trace(
                    read_json(directory / default_name), EXECUTION_SCHEMA
                )
                if default_execution["call_count"] != assertion_folding[
                    "retained_call_count"
                ]:
                    raise ValueError("assertion-pruned execution count mismatch")
                fold_by_invocation = {
                    int(invocation_id): str(fold["fold_id"])
                    for fold in assertion_folding["folds"]
                    for invocation_id in fold["invocation_ids"]
                }
                trace_index = build_trace_index(
                    execution,
                    test_id=test_id,
                    test=test,
                    method_ids=method_ids,
                    catalog_fingerprint=fingerprint,
                    fold_by_invocation=fold_by_invocation,
                    default_execution=default_name,
                )
                validate_trace_index(trace_index, directory)
                index_path = directory / "trace_index.json"
                write_compact_json(index_path, trace_index)
                tests.append({
                    "test_id": test_id,
                    "test": test,
                    "trigger": int(number),
                    "trace_index": index_path.relative_to(bug_dir).as_posix(),
                })
            suite = {
                "schema": "execution-trace-suite",
                "schema_version": 1,
                "project": project,
                "bug": bug,
                "method_catalog_fingerprint": fingerprint,
                "method_catalog": catalog,
                "test_count": len(tests),
                "tests": tests,
            }
            validate_trace_suite(suite, bug_dir)
            write_json(suite_path, suite)
        except Exception as error:
            suite_path.unlink(missing_ok=True)
            write_text(
                layout.stage_log_dir("trace", project, bug) / "suite_error.log",
                str(error) + "\n",
            )
            raise


def _ensure_assertion_artifacts(
    output: Path, execution: Dict[str, Any]
) -> None:
    pruned_path = output / "execution_assertion_pruned.json"
    folding_path = output / "assertion_folding.json"
    valid = False
    if folding_path.is_file():
        folding = validate_assertion_folding(read_json(folding_path))
        if folding["original_call_count"] == execution["call_count"]:
            if folding["folded_call_count"] == 0:
                pruned_path.unlink(missing_ok=True)
                valid = True
            elif pruned_path.is_file():
                pruned = validate_trace(read_json(pruned_path), EXECUTION_SCHEMA)
                valid = (
                    pruned.get("schema_version") == execution.get("schema_version")
                    and pruned.get("test_start") == execution.get("test_start")
                    and pruned["call_count"] == folding["retained_call_count"]
                )
    if not valid:
        pruned, folding = fold_successful_assertions(execution)
        write_json(folding_path, folding)
        if folding["folded_call_count"]:
            write_json(pruned_path, pruned)
        else:
            pruned_path.unlink(missing_ok=True)


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
    agent_jar: Path,
    d4j_home: Path | None,
    java_home: Path | None,
    timeout: int,
    force: bool = False,
    capture_values: bool = True,
    value_max_chars: int = 120,
    value_max_items: int = 8,
    value_max_depth: int = 2,
    value_max_arguments_chars: int = 480,
) -> List[Dict[str, object]]:
    if not agent_jar.is_file():
        raise FileNotFoundError(f"agent jar not found: {agent_jar}")
    env = defects4j_environment(d4j_home, java_home)
    requested_capture = {
        "capture_values": capture_values,
        "value_max_chars": value_max_chars,
        "value_max_items": value_max_items,
        "value_max_depth": value_max_depth,
        "value_max_arguments_chars": value_max_arguments_chars,
    }
    rows = []
    trigger_items = list(layout.discover_triggers(projects, bugs, trigger))
    grouped = defaultdict(list)
    for item in trigger_items:
        grouped[(item[0], item[1])].append(item)
    for project, bug, number, output in trigger_items:
        reuse = (
            (output / "execution.json").exists()
            and not force
        )
        if reuse:
            try:
                execution = validate_trace(
                    read_json(output / "execution.json"), EXECUTION_SCHEMA
                )
                instrumentation = execution.get("assertion_instrumentation")
                if not isinstance(instrumentation, dict):
                    reuse = False
                if (
                    execution.get("schema_version") != 4
                    or (execution.get("test_start") or {}).get("value_capture")
                    != requested_capture
                ):
                    reuse = False
                if reuse:
                    _ensure_assertion_artifacts(output, execution)
                    rows.append({
                        "project": project, "bug": bug, "trigger": number,
                        "status": "SKIPPED", "call_count": execution["call_count"],
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
            if reuse:
                continue
        test_path = output / "trigger_test.txt"
        try:
            test = test_path.read_text(encoding="utf-8").strip().splitlines()[0]
            log_dir = layout.stage_log_dir("trace", project, bug, number)
            execution = trace_trigger(
                layout.workspace_dir(project, bug), output, test, project,
                agent_jar, env, timeout, log_dir,
                capture_values, value_max_chars, value_max_items,
                value_max_depth, value_max_arguments_chars,
            )
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "OK", "call_count": execution["call_count"]})
        except Exception as error:
            write_text(layout.stage_log_dir("trace", project, bug, number) / "error.log",
                       str(error) + "\n")
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "ERROR", "call_count": 0})
    if trigger is None and rows and all(row["status"] != "ERROR" for row in rows):
        _write_trace_suites(layout, grouped)
    write_csv(
        layout.logs / "trace.csv",
        rows,
        ["project", "bug", "trigger", "status", "call_count"],
    )
    return rows
