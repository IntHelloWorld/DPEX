import os
import shutil
from pathlib import Path
from typing import Dict, List, Tuple

from .process import CommandResult, run_command


def defects4j_environment(d4j_home: Path | None, java_home: Path | None) -> Dict[str, str]:
    env = os.environ.copy()
    if d4j_home:
        env["D4J_HOME"] = str(d4j_home)
        env["PATH"] = str(d4j_home / "framework" / "bin") + os.pathsep + env.get("PATH", "")
        env["PERL5LIB"] = (
            str(d4j_home / "framework" / "core")
            + os.pathsep
            + env.get("PERL5LIB", "")
        )
    if java_home:
        env["JAVA_HOME"] = str(java_home)
        env["PATH"] = str(java_home / "bin") + os.pathsep + env.get("PATH", "")
    env.setdefault("TZ", "America/Los_Angeles")
    return env


def ensure_defects4j(env: Dict[str, str]) -> None:
    result = run_command(["defects4j", "info", "-p", "Lang"], env=env, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"Defects4J is unavailable: {result.stderr or result.stdout}")


def bug_ids(project: str, env: Dict[str, str]) -> List[str]:
    result = run_command(["defects4j", "bids", "-p", project], env=env, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"cannot list bugs for {project}: {result.stderr}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]


def checkout(
    project: str,
    bug: str,
    destination: Path,
    env: Dict[str, str],
    force: bool = False,
    timeout: int = 1200,
) -> CommandResult:
    if destination.exists() and force:
        shutil.rmtree(destination)
    if destination.exists():
        return CommandResult(0, "workspace already exists", "")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return run_command(
        ["defects4j", "checkout", "-p", project, "-v", f"{bug}b", "-w", str(destination)],
        env=env, timeout=timeout,
    )


def compile_project(workspace: Path, env: Dict[str, str], timeout: int) -> CommandResult:
    return run_command(["defects4j", "compile"], cwd=workspace, env=env, timeout=timeout)


def trigger_tests(workspace: Path, env: Dict[str, str]) -> List[str]:
    result = run_command(
        ["defects4j", "export", "-p", "tests.trigger"],
        cwd=workspace,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot export trigger tests: {result.stderr}")
    return [line.strip() for line in result.stdout.splitlines() if "::" in line]


def test_classpath(workspace: Path, env: Dict[str, str]) -> str:
    result = run_command(
        ["defects4j", "export", "-p", "cp.test"],
        cwd=workspace,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot export test classpath: {result.stderr}")
    return result.stdout.strip()


def split_test(test: str) -> Tuple[str, str]:
    if "::" not in test:
        raise ValueError(f"invalid Defects4J test name: {test!r}")
    test_class, test_method = test.split("::", 1)
    if not test_class or not test_method:
        raise ValueError(f"invalid Defects4J test name: {test!r}")
    return test_class, test_method
