"""Bounded lifecycle for temporary buggy Defects4J checkouts."""
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from .defects4j import checkout, defects4j_environment
from .io import write_text
from .layout import RunLayout


def remove_checkout(layout: RunLayout, project: str, bug: str) -> None:
    path = layout.workspace_dir(project, bug)
    if (path.is_symlink() or path.parent.resolve() != layout.workspace.resolve()
            or path.resolve().parent != layout.workspace.resolve()):
        raise ValueError(f"unsafe checkout cleanup path: {path}")
    if path.exists():
        shutil.rmtree(path)


@contextmanager
def temporary_checkout(layout: RunLayout, project: str, bug: str,
                       timeout: int = 1200, d4j_home: Path | None = None):
    workspace = layout.workspace_dir(project, bug)
    created = not workspace.exists()
    try:
        if created:
            home = d4j_home or os.environ.get("D4J_HOME") or os.environ.get("DEFECTS4J_HOME")
            env = defects4j_environment(Path(home) if home else None, None)
            result = checkout(project, bug, workspace, env, timeout=timeout)
            log = layout.stage_log_dir("checkout", project, bug)
            write_text(log / "checkout.stdout.log", result.stdout)
            write_text(log / "checkout.stderr.log", result.stderr)
            if result.returncode != 0:
                raise RuntimeError(f"cannot restore buggy checkout: {result.returncode}")
        yield workspace
    finally:
        if created:
            remove_checkout(layout, project, bug)
