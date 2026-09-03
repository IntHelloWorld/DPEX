import os
import shutil
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Sequence

from mllmfl.domain.failure import extract_error_stack
from mllmfl.domain.assertion_folding import (
    fold_successful_assertions,
    validate_assertion_folding,
)
from mllmfl.domain.schemas import (
    validate_defect_context,
    validate_trace_suite,
)
from mllmfl.domain.refinement_trace import (
    build_method_catalog,
    build_refinement_trace,
    validate_refinement_trace,
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
    compress_zstd_file,
    read_json,
    read_zstd_json,
    write_compact_json,
    write_csv,
    write_json,
    write_text,
    write_zstd_json,
)
from mllmfl.infrastructure.java_source import find_java_file
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.process import run_command

TRACE_WORK_NAME = "refinement-trace.work.json.zst"


def archive_failed_raw_trace(output: Path) -> Path | None:
    raw_path = output / "raw_events.jsonl"
    if not raw_path.is_file():
        return None
    target = output / "raw_events.failed.jsonl.zst"
    compress_zstd_file(raw_path, target, level=1)
    raw_path.unlink()
    return target


def _suite_only_targets(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
) -> list[tuple[str, str, str, Path, str, Dict[str, Any]]]:
    """Recover lean trace targets after their temporary trigger dirs are gone."""
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
                or raw_suite.get("schema_version") != 2
            ):
                # Old suites are not read. Their still-present trigger inputs
                # are migrated through the normal tracing path.
                continue
            suite = validate_trace_suite(raw_suite)
            known_method_ids = {
                str(item["method_id"]) for item in suite["method_catalog"]
            }
            for spec in suite["tests"]:
                number = str(spec["trigger"])
                if trigger is not None and trigger != number:
                    continue
                trace_path = bug_dir / Path(*Path(str(spec["trace"])).parts)
                trace_value = validate_refinement_trace(
                    read_zstd_json(trace_path)
                )
                if (
                    trace_value["project"] != suite["project"]
                    or trace_value["test_id"] != spec["test_id"]
                    or trace_value["test"] != spec["test"]
                    or trace_value["fingerprint"] != spec["trace_fingerprint"]
                    or trace_value["method_catalog_fingerprint"]
                    != suite["method_catalog_fingerprint"]
                    or any(
                        method_id not in known_method_ids
                        for method_id, *_ in trace_value["methods"]
                        if str(method_id).startswith("M")
                    )
                ):
                    raise ValueError(
                        f"trace suite payload mismatch for {spec['test_id']}"
                    )
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


def trace_trigger(workspace: Path, output: Path, test: str, project: str,
                  agent_jar: Path, env: Dict[str, str], timeout: int,
                  log_dir: Path | None = None, capture_values: bool = True,
                  value_max_chars: int = 120, value_max_items: int = 8,
                  value_max_depth: int = 2,
                  value_max_arguments_chars: int = 480,
                  retain_debug_artifacts: bool = False) -> Dict[str, Any]:
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
    (output / "raw_events.failed.jsonl.zst").unlink(missing_ok=True)
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
    execution["process_exit_code"] = result.returncode
    assertion_pruned, assertion_folding = fold_successful_assertions(execution)
    work = {
        "schema": "refinement-trace-work",
        "schema_version": 1,
        "test": test,
        "execution": assertion_pruned,
        "assertion_folding": assertion_folding,
        "defect_context": defect_context,
    }
    write_zstd_json(output / TRACE_WORK_NAME, work, level=1)
    if retain_debug_artifacts:
        write_zstd_json(output / "execution.debug.json.zst", execution, level=1)
        write_json(output / "assertion_folding.debug.json", assertion_folding)
        write_json(output / "defect_context.debug.json", defect_context)
        compress_zstd_file(
            raw_path, output / "raw_events.jsonl.zst", level=1
        )
    else:
        (output / "raw_events.jsonl.zst").unlink(missing_ok=True)
    raw_path.unlink(missing_ok=True)
    return work


def _write_trace_suites(
    layout: RunLayout,
    grouped: Dict[tuple[str, str], list[tuple[str, str, str, Path]]],
    *,
    retain_debug_artifacts: bool,
) -> None:
    for (project, bug), items in grouped.items():
        bug_dir = layout.artifacts / project / f"bug_{bug}"
        suite_path = bug_dir / "trace_suite.json"
        try:
            ordered = sorted(items, key=lambda item: int(item[2]))

            def executions():
                for _, _, _, directory in ordered:
                    work = read_zstd_json(directory / TRACE_WORK_NAME)
                    yield validate_trace(work["execution"], EXECUTION_SCHEMA)

            catalog, method_ids, fingerprint = build_method_catalog(executions())
            tests = []
            for index, (_, _, number, directory) in enumerate(ordered, 1):
                test_id = f"T{index}"
                work = read_zstd_json(directory / TRACE_WORK_NAME)
                if (
                    not isinstance(work, dict)
                    or work.get("schema") != "refinement-trace-work"
                    or work.get("schema_version") != 1
                ):
                    raise ValueError("unsupported refinement trace work schema")
                test = str(work["test"])
                execution = validate_trace(work["execution"], EXECUTION_SCHEMA)
                assertion_folding = validate_assertion_folding(
                    work["assertion_folding"]
                )
                if execution["call_count"] != assertion_folding[
                    "retained_call_count"
                ]:
                    raise ValueError("assertion-pruned execution count mismatch")
                context = validate_defect_context(work["defect_context"])
                normalized = build_refinement_trace(
                    execution,
                    project=project,
                    test_id=test_id,
                    test=test,
                    method_ids=method_ids,
                    catalog_fingerprint=fingerprint,
                    assertion_folding=assertion_folding,
                    error_stack=str(context["error_stack"]),
                    test_output=str(context["test_output"]),
                )
                trace_path = bug_dir / "traces" / (
                    f"{test_id}.refinement-trace.json.zst"
                )
                write_zstd_json(trace_path, normalized, level=1)
                tests.append({
                    "test_id": test_id,
                    "test": test,
                    "trigger": int(number),
                    "trace": trace_path.relative_to(bug_dir).as_posix(),
                    "trace_fingerprint": normalized["fingerprint"],
                })
            expected_traces = {str(item["trace"]) for item in tests}
            traces_dir = bug_dir / "traces"
            for path in traces_dir.glob("T*.refinement-trace.json.zst"):
                if path.relative_to(bug_dir).as_posix() not in expected_traces:
                    path.unlink()
            suite = {
                "schema": "execution-trace-suite",
                "schema_version": 2,
                "project": project,
                "bug": bug,
                "method_catalog_fingerprint": fingerprint,
                "method_catalog": catalog,
                "test_count": len(tests),
                "tests": tests,
            }
            validate_trace_suite(suite, bug_dir)
            write_compact_json(suite_path, suite)
            if not retain_debug_artifacts:
                for _, _, _, directory in ordered:
                    for name in (
                        TRACE_WORK_NAME,
                        "collect.json",
                        "trigger_test.txt",
                        "defect_context.json",
                        "assertion_folding.json",
                        "execution.json",
                        "execution_assertion_pruned.json",
                        "trace_index.json",
                        "raw_events.jsonl",
                        "raw_events.jsonl.zst",
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
    timeout: int,
    force: bool = False,
    capture_values: bool = True,
    value_max_chars: int = 120,
    value_max_items: int = 8,
    value_max_depth: int = 2,
    value_max_arguments_chars: int = 480,
    retain_debug_artifacts: bool = False,
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
    suite_targets = _suite_only_targets(layout, projects, bugs, trigger)
    suite_keys = {(item[0], item[1]) for item in suite_targets}
    retrace_suite_keys = {
        (project, bug)
        for project, bug, _, _, _, normalized in suite_targets
        if force or normalized.get("capture") != requested_capture
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
        write_json(output / "collect.json", {
            "schema": "collected-trigger",
            "schema_version": 2,
            "project": project,
            "bug": bug,
            "trigger": int(number),
            "test_id": str(normalized["test_id"]),
            "test": test,
            "test_exit_code": int(
                normalized["failure"]["process_exit_code"]
            ),
            "test_output": str(normalized["failure"]["test_output"]),
        })
        trigger_items.append((project, bug, number, output))
    grouped = defaultdict(list)
    for item in trigger_items:
        grouped[(item[0], item[1])].append(item)
    if force and trigger is None:
        for project, bug in grouped:
            (layout.artifacts / project / f"bug_{bug}" / "trace_suite.json").unlink(
                missing_ok=True
            )
    for project, bug, number, output in trigger_items:
        reuse = (
            (output / TRACE_WORK_NAME).exists()
            and not force
        )
        if reuse:
            try:
                work = read_zstd_json(output / TRACE_WORK_NAME)
                execution = validate_trace(
                    work["execution"], EXECUTION_SCHEMA
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
        (output / TRACE_WORK_NAME).unlink(missing_ok=True)
        if not retain_debug_artifacts:
            for name in (
                "execution.debug.json.zst",
                "assertion_folding.debug.json",
                "defect_context.debug.json",
                "raw_events.jsonl.zst",
            ):
                (output / name).unlink(missing_ok=True)
        test_path = output / "trigger_test.txt"
        try:
            test = test_path.read_text(encoding="utf-8").strip().splitlines()[0]
            log_dir = layout.stage_log_dir("trace", project, bug, number)
            execution = trace_trigger(
                layout.workspace_dir(project, bug), output, test, project,
                agent_jar, env, timeout, log_dir,
                capture_values, value_max_chars, value_max_items,
                value_max_depth, value_max_arguments_chars,
                retain_debug_artifacts,
            )
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "OK",
                         "call_count": execution["execution"]["call_count"]})
        except Exception as error:
            if (output / "raw_events.jsonl").is_file():
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
            layout, grouped, retain_debug_artifacts=retain_debug_artifacts
        )
    write_csv(
        layout.logs / "trace.csv",
        rows,
        ["project", "bug", "trigger", "status", "call_count"],
    )
    return rows
