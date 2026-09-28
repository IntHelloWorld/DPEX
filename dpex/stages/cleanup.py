import shutil
from pathlib import Path
from typing import Any, Dict, Sequence

from dpex.infrastructure.io import write_csv
from dpex.infrastructure.layout import RunLayout


LEGACY_TRACE_FILENAMES = frozenset({
    "trace.json",
    "execution_sliced.json",
    "execution_compressed.json",
})

FINAL_ONLY_ARTIFACT_DIRECTORIES = (
    "traces",
    "triggers",
    "inspection_graphs",
)
FINAL_ONLY_ARTIFACT_FILES = (
    "trace_suite.json",
    "trace_suite_summary.json",
    "refine_conversation.jsonl",
    "refine_response_usage.jsonl",
    "refine_render_errors.jsonl",
    "localize_conversation.jsonl",
    "localize_response_usage.jsonl",
    "localize_render_errors.jsonl",
)


def _bug_directories(
    layout: RunLayout, projects: Sequence[str], bugs: set[str] | None,
) -> list[tuple[str, str, Path]]:
    result = []
    selected_projects = sorted(set(projects))
    if not selected_projects or "ALL" in selected_projects:
        selected_projects = [
            item.name for item in sorted(layout.artifacts.iterdir()) if item.is_dir()
        ] if layout.artifacts.is_dir() else []
    for project in selected_projects:
        project_dir = layout.artifacts / project
        if not project_dir.is_dir():
            continue
        for bug_dir in sorted(project_dir.glob("bug_*")):
            bug = bug_dir.name.removeprefix("bug_")
            if bug.isdigit() and (bugs is None or bug in bugs):
                result.append((project, bug, bug_dir))
    return result


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    *,
    apply: bool = False,
) -> list[Dict[str, Any]]:
    """Preview or apply the exact legacy trace-file allowlist."""
    rows: list[Dict[str, Any]] = []
    for project, bug, bug_dir in _bug_directories(layout, projects, bugs):
        candidates = sorted({
            path
            for filename in LEGACY_TRACE_FILENAMES
            for path in bug_dir.rglob(filename)
            if path.is_file()
        })
        for path in candidates:
            relative = path.relative_to(layout.root).as_posix()
            size = path.stat().st_size
            if apply:
                path.unlink()
            rows.append({
                "project": project,
                "bug": bug,
                "status": "REMOVED" if apply else "DRY_RUN",
                "bytes": size,
                "path": relative,
            })
    write_csv(
        layout.logs / "cleanup.csv",
        rows,
        ["project", "bug", "status", "bytes", "path"],
    )
    return rows


def final_only_bug(layout: RunLayout, project: str, bug: str) -> None:
    """Remove known intermediates after one bug was successfully evaluated."""
    bug_dir = layout.artifacts / project / f"bug_{bug}"
    if not any((bug_dir / name).is_file() for name in (
        "refinement.json", "localization.json",
    )):
        raise ValueError("final-only cleanup requires a localization result")
    for name in FINAL_ONLY_ARTIFACT_FILES:
        (bug_dir / name).unlink(missing_ok=True)
    for name in LEGACY_TRACE_FILENAMES:
        for path in bug_dir.rglob(name):
            if path.is_file():
                path.unlink()
    for name in FINAL_ONLY_ARTIFACT_DIRECTORIES:
        path = bug_dir / name
        if path.is_dir():
            shutil.rmtree(path)
    workspace = layout.workspace_dir(project, bug)
    if workspace.is_dir():
        if workspace.resolve().parent != layout.workspace.resolve():
            raise ValueError("final-only workspace target escaped run workspace")
        shutil.rmtree(workspace)
