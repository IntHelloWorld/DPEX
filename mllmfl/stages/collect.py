from pathlib import Path
from typing import Dict, List, Sequence

from mllmfl.infrastructure.defects4j import (
    bug_ids, checkout, compile_project, defects4j_environment, ensure_defects4j, trigger_tests,
)
from mllmfl.infrastructure.io import write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.process import run_command


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    d4j_home: Path | None,
    java_home: Path | None,
    timeout: int,
    force: bool = False,
) -> List[Dict[str, object]]:
    layout.ensure()
    env = defects4j_environment(d4j_home, java_home)
    ensure_defects4j(env)
    rows: List[Dict[str, object]] = []
    for project in projects:
        selected = sorted(bugs or set(bug_ids(project, env)), key=lambda value: int(value))
        for bug in selected:
            workspace = layout.workspace_dir(project, bug)
            checkout_result = checkout(project, bug, workspace, env, force=force, timeout=timeout)
            bug_log = layout.logs / "collect" / project / f"bug_{bug}"
            write_text(bug_log / "checkout.stdout.log", checkout_result.stdout)
            write_text(bug_log / "checkout.stderr.log", checkout_result.stderr)
            if checkout_result.returncode != 0:
                rows.append(
                    {
                        "project": project,
                        "bug": bug,
                        "status": "SETUP_FAILED",
                        "trigger_count": 0,
                    }
                )
                continue

            compile_result = compile_project(workspace, env, timeout)
            write_text(bug_log / "compile.stdout.log", compile_result.stdout)
            write_text(bug_log / "compile.stderr.log", compile_result.stderr)
            if compile_result.returncode != 0:
                rows.append(
                    {
                        "project": project,
                        "bug": bug,
                        "status": "SETUP_FAILED",
                        "trigger_count": 0,
                    }
                )
                continue
            tests = trigger_tests(workspace, env)
            for index, test in enumerate(tests, 1):
                output = layout.trigger_dir(project, bug, index)
                trigger_log = layout.stage_log_dir("collect", project, bug, index)
                result = run_command(
                    ["defects4j", "test", "-t", test],
                    cwd=workspace,
                    env=env,
                    timeout=timeout,
                )
                write_text(output / "trigger_test.txt", test + "\n")
                write_text(trigger_log / "test.stdout.log", result.stdout)
                write_text(trigger_log / "test.stderr.log", result.stderr)
                write_text(
                    output / "failure.txt",
                    (result.stdout + "\n" + result.stderr).strip() + "\n",
                )
                write_json(output / "collect.json", {
                    "schema": "collected-trigger", "schema_version": 1,
                    "project": project, "bug": bug, "trigger": index,
                    "test": test, "test_exit_code": result.returncode,
                })
            rows.append(
                {
                    "project": project,
                    "bug": bug,
                    "status": "OK",
                    "trigger_count": len(tests),
                }
            )
    write_csv(layout.logs / "collect.csv", rows, ["project", "bug", "status", "trigger_count"])
    return rows
