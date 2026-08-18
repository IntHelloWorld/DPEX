import os
from pathlib import Path
from typing import Any, Dict, List, Sequence

from mllmfl.domain.failure import extract_error_stack
from mllmfl.domain.schemas import validate_defect_context
from mllmfl.domain.trace import build_trace, load_events, project_execution
from mllmfl.domain.test_slice import slice_execution
from mllmfl.infrastructure.defects4j import defects4j_environment, split_test, test_classpath
from mllmfl.infrastructure.io import read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.java_source import find_java_file
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.process import run_command


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


def trace_trigger(workspace: Path, output: Path, test: str, project: str,
                  agent_jar: Path, env: Dict[str, str], timeout: int,
                  log_dir: Path | None = None) -> Dict[str, Any]:
    test_class, test_method = split_test(test)
    prefix = PROJECT_PREFIX.get(project, test_class.rsplit(".", 1)[0])
    output.mkdir(parents=True, exist_ok=True)
    raw_path = output / "raw_events.jsonl"
    if raw_path.exists():
        raw_path.unlink()
    agent_args = f"{prefix},class:{test_class}"
    command = [
        "java", "-Djava.awt.headless=true", f"-Dfltrace.raw.file={raw_path}",
        f"-Dfltrace.test.class={test_class}", f"-Dfltrace.test.method={test_method}",
        f"-javaagent:{agent_jar}={agent_args}", "-cp", build_classpath(workspace, agent_jar, env),
        "fltrace.runner.SingleTestRunner", test_class, test_method,
    ]
    result = run_command(command, cwd=workspace, env=env, timeout=timeout)
    log_dir = log_dir or output
    write_text(log_dir / "trace.stdout.log", result.stdout)
    write_text(log_dir / "trace.stderr.log", result.stderr)
    error_stack = extract_error_stack(result.stdout + "\n" + result.stderr)
    collect_path = output / "collect.json"
    collect_data = read_json(collect_path) if collect_path.is_file() else {}
    test_output = str(collect_data.get("test_output") or "")
    if not test_output:
        legacy_failure = output / "failure.txt"
        if legacy_failure.is_file():
            test_output = legacy_failure.read_text(encoding="utf-8", errors="ignore").strip()
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
    full.update({
        "project": project, "test": {"class": test_class, "method": test_method},
        "process_exit_code": result.returncode,
    })
    execution = project_execution(full, test_class, test_method)
    execution["project"] = project
    sliced = slice_execution(
        execution, find_java_file(workspace, test_class), test_class, test_method
    )
    write_json(output / "trace.json", full)
    write_json(output / "execution.json", execution)
    write_json(output / "execution_sliced.json", sliced)
    write_json(output / "test_slice.json", sliced["slice"])
    (output / "window.json").unlink(missing_ok=True)
    return execution


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
) -> List[Dict[str, object]]:
    if not agent_jar.is_file():
        raise FileNotFoundError(f"agent jar not found: {agent_jar}")
    env = defects4j_environment(d4j_home, java_home)
    rows = []
    for project, bug, number, output in layout.discover_triggers(projects, bugs, trigger):
        if (output / "execution.json").exists() and (output / "execution_sliced.json").exists() and not force:
            rows.append({"project": project, "bug": bug, "trigger": number, "status": "SKIPPED"})
            continue
        test_path = output / "trigger_test.txt"
        try:
            test = test_path.read_text(encoding="utf-8").strip().splitlines()[0]
            log_dir = layout.stage_log_dir("trace", project, bug, number)
            execution = trace_trigger(
                layout.workspace_dir(project, bug), output, test, project,
                agent_jar, env, timeout, log_dir,
            )
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "OK", "call_count": execution["call_count"]})
        except Exception as error:
            write_text(layout.stage_log_dir("trace", project, bug, number) / "error.log",
                       str(error) + "\n")
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "ERROR", "call_count": 0})
    write_csv(
        layout.logs / "trace.csv",
        rows,
        ["project", "bug", "trigger", "status", "call_count"],
    )
    return rows
