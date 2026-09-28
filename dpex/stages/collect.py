import shutil
from pathlib import Path
from typing import Dict, List, Sequence

from dpex.infrastructure.defects4j import (
    bug_ids, checkout, compile_project, defects4j_environment, ensure_defects4j, trigger_tests,
)
from dpex.infrastructure.io import write_csv, write_text
from dpex.infrastructure.layout import RunLayout
from dpex.infrastructure.checkouts import remove_checkout


def _prepare_trigger_directories(
    layout: RunLayout,
    project: str,
    bug: str,
    tests: Sequence[str],
) -> None:
    triggers = layout.artifacts / project / f"bug_{bug}" / "triggers"
    expected = {f"trigger_{index}" for index in range(1, len(tests) + 1)}
    if triggers.is_dir():
        for path in triggers.glob("trigger_*"):
            if (
                path.is_dir()
                and path.name.removeprefix("trigger_").isdigit()
                and path.name not in expected
            ):
                shutil.rmtree(path)
    for index, test in enumerate(tests, 1):
        output = layout.trigger_dir(project, bug, index)
        test_path = output / "trigger_test.txt"
        if test_path.is_file() and test_path.read_text(encoding="utf-8").strip() != test:
            shutil.rmtree(output)


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    d4j_home: Path | None,
    java_home: Path | None,
    timeout: int,
    force: bool = False,
    *,
    agent_jar: Path,
    config_path: Path,
    retain_debug_artifacts: bool = False,
) -> List[Dict[str, object]]:
    from dpex.stages import trace

    layout.ensure()
    if not agent_jar.is_file():
        raise FileNotFoundError(f"agent jar not found: {agent_jar}")
    capture = trace.load_trace_configuration(config_path)
    env = defects4j_environment(d4j_home, java_home)
    ensure_defects4j(env)
    rows: List[Dict[str, object]] = []
    for project in dict.fromkeys(projects):
        selected = sorted(bugs if bugs is not None else set(bug_ids(project, env)), key=int)
        for bug in selected:
            workspace = layout.workspace_dir(project, bug)
            bug_log = layout.stage_log_dir("collect", project, bug)
            row = {"project": project, "bug": bug, "status": "ERROR",
                   "trigger_count": 0, "checkout_removed": False}
            try:
                targets = []
                if not force:
                    targets = trace._suite_only_targets(
                        layout, [project], {bug}, None, capture
                    )
                if targets and all(
                    item[5].get("trace_config") == capture
                    for item in targets
                ):
                    row.update(status="SKIPPED", trigger_count=len(targets))
                else:
                    result = checkout(
                        project, bug, workspace, env, force=force, timeout=timeout
                    )
                    write_text(bug_log / "checkout.stdout.log", result.stdout)
                    write_text(bug_log / "checkout.stderr.log", result.stderr)
                    if result.returncode != 0:
                        raise RuntimeError(f"checkout failed: {result.returncode}")
                    result = compile_project(workspace, env, timeout)
                    write_text(bug_log / "compile.stdout.log", result.stdout)
                    write_text(bug_log / "compile.stderr.log", result.stderr)
                    if result.returncode != 0:
                        raise RuntimeError(f"compile failed: {result.returncode}")
                    tests = list(dict.fromkeys(trigger_tests(workspace, env)))
                    if not tests:
                        raise ValueError("no trigger tests exported")
                    _prepare_trigger_directories(layout, project, bug, tests)
                    # Invalidate the old suite before attempting fresh executions.
                    (layout.artifacts / project / f"bug_{bug}" / "trace_suite.json").unlink(missing_ok=True)
                    (
                        layout.artifacts
                        / project
                        / f"bug_{bug}"
                        / trace.TRACE_SUITE_SUMMARY_NAME
                    ).unlink(missing_ok=True)
                    for index, test in enumerate(tests, 1):
                        write_text(layout.trigger_dir(project, bug, index) / "trigger_test.txt", test + "\n")
                    results = trace.run(
                        layout, [project], {bug}, None, agent_jar, d4j_home,
                        java_home, config_path, timeout, force, retain_debug_artifacts,
                    )
                    row["trigger_count"] = len(tests)
                    if len(results) != len(tests) or any(r["status"] == "ERROR" for r in results):
                        raise RuntimeError("trigger tracing failed; checkout retained")
                    verified = trace._suite_only_targets(
                        layout, [project], {bug}, None, capture
                    )
                    if len(verified) != len(tests) or [v[4] for v in verified] != tests:
                        raise ValueError("persisted trace suite does not match exported tests")
                    row["status"] = "OK"
                remove_checkout(layout, project, bug)
                row["checkout_removed"] = True
            except Exception as error:
                row["status"] = "ERROR"
                write_text(bug_log / "error.log", str(error) + "\n")
            rows.append(row)
    write_csv(layout.logs / "collect.csv", rows,
              ["project", "bug", "status", "trigger_count", "checkout_removed"])
    return rows
