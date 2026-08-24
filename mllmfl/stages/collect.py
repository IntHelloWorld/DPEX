import shutil
from pathlib import Path
from typing import Dict, List, Sequence

from mllmfl.infrastructure.defects4j import (
    bug_ids, checkout, compile_project, defects4j_environment, ensure_defects4j, trigger_tests,
)
from mllmfl.infrastructure.io import write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.process import run_command


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
            available_tests = trigger_tests(workspace, env)
            tests = list(available_tests)
            _prepare_trigger_directories(layout, project, bug, tests)
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
                test_output = "\n".join(
                    part.rstrip() for part in (result.stdout, result.stderr) if part
                )
                write_text(
                    output / "failure.txt",
                    test_output + ("\n" if test_output else ""),
                )
                test_id = f"T{index:03d}"
                write_json(output / "collect.json", {
                    "schema": "collected-trigger", "schema_version": 2,
                    "project": project, "bug": bug, "trigger": index,
                    "test_id": test_id,
                    "test": test, "test_exit_code": result.returncode,
                    "test_output": test_output,
                })
            rows.append(
                {
                    "project": project,
                    "bug": bug,
                    "status": "OK",
                    "available_trigger_count": len(available_tests),
                    "trigger_count": len(tests),
                }
            )
    write_csv(
        layout.logs / "collect.csv",
        rows,
        ["project", "bug", "status", "available_trigger_count", "trigger_count"],
    )
    return rows
