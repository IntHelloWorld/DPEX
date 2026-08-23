import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List

from mllmfl.infrastructure.method_location import (
    JavaExecutable,
    MethodLocation,
    exact_parameters,
    java_executables,
    same_parameters,
)
HUNK_RE = re.compile(
    r"^@@\s+-(?P<fixed_start>\d+)(?:,(?P<fixed_count>\d+))?\s+"
    r"\+(?P<buggy_start>\d+)(?:,(?P<buggy_count>\d+))?\s+@@"
)


@dataclass(frozen=True)
class PatchHunk:
    fixed_start: int
    buggy_start: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class PatchFile:
    path: str
    fixed_changed_lines: frozenset[int]
    buggy_changed_lines: frozenset[int]
    hunks: tuple[PatchHunk, ...]


def _patch_path(line: str) -> str:
    value = line[4:].split("\t", 1)[0].strip()
    if value == "/dev/null":
        return ""
    return value[2:] if value.startswith("b/") else value


def parse_source_patch(text: str) -> List[PatchFile]:
    """Return changed buggy-side line numbers grouped by Java source file."""
    result: List[PatchFile] = []
    path = ""
    fixed_changed: set[int] = set()
    buggy_changed: set[int] = set()
    hunks: List[PatchHunk] = []
    hunk_lines: List[str] = []
    fixed_start = buggy_start = 0
    fixed_line: int | None = None
    buggy_line: int | None = None

    def finish_hunk() -> None:
        nonlocal hunk_lines
        if fixed_start or buggy_start:
            hunks.append(PatchHunk(fixed_start, buggy_start, tuple(hunk_lines)))
        hunk_lines = []

    def finish() -> None:
        nonlocal fixed_changed, buggy_changed, hunks, fixed_start, buggy_start
        finish_hunk()
        if path.endswith(".java"):
            result.append(PatchFile(
                path,
                frozenset(fixed_changed),
                frozenset(buggy_changed),
                tuple(hunks),
            ))
        fixed_changed = set()
        buggy_changed = set()
        hunks = []
        fixed_start = buggy_start = 0

    for line in text.splitlines():
        if line.startswith("+++ "):
            if path or fixed_changed or buggy_changed or hunks:
                finish()
            path = _patch_path(line)
            fixed_line = buggy_line = None
            continue
        match = HUNK_RE.match(line)
        if match:
            finish_hunk()
            fixed_start = int(match.group("fixed_start"))
            buggy_start = int(match.group("buggy_start"))
            fixed_line = fixed_start
            buggy_line = buggy_start
            continue
        if fixed_line is None or buggy_line is None or not path:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            buggy_changed.add(buggy_line)
            buggy_line += 1
            hunk_lines.append(line)
        elif line.startswith("-") and not line.startswith("---"):
            fixed_changed.add(fixed_line)
            fixed_line += 1
            hunk_lines.append(line)
        elif line.startswith(" "):
            fixed_line += 1
            buggy_line += 1
            hunk_lines.append(line)
        elif line.startswith("\\ No newline at end of file"):
            continue
        else:
            fixed_line = buggy_line = None
    if path or fixed_changed or buggy_changed or hunks:
        finish()
    return result


def _resolve_source(workspace: Path, patch_path: str) -> Path:
    relative = PurePosixPath(patch_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in patch_path:
        raise ValueError(f"unsafe patched source path: {patch_path}")
    root = workspace.resolve()
    direct = (workspace / Path(*relative.parts)).resolve()
    if not direct.is_relative_to(root):
        raise ValueError(f"patched source escapes workspace: {patch_path}")
    if direct.is_file():
        return direct
    parts = relative.parts
    matches = [
        candidate
        for candidate in workspace.rglob(Path(patch_path).name)
        if candidate.is_file()
        and len(candidate.relative_to(workspace).parts) >= len(parts)
        and candidate.relative_to(workspace).parts[-len(parts) :] == parts
    ]
    if len(matches) != 1:
        raise ValueError(f"cannot uniquely resolve patched source: {patch_path}")
    return matches[0]


def reconstruct_fixed_source(buggy_text: str, hunks: tuple[PatchHunk, ...]) -> str:
    buggy_lines = buggy_text.splitlines()
    fixed_lines: List[str] = []
    buggy_index = 0
    for hunk in hunks:
        target = hunk.buggy_start - 1
        if target < buggy_index or target > len(buggy_lines):
            raise ValueError("invalid or overlapping Defects4J patch hunk")
        fixed_lines.extend(buggy_lines[buggy_index:target])
        buggy_index = target
        for patch_line in hunk.lines:
            prefix, content = patch_line[0], patch_line[1:]
            if prefix in {" ", "+"}:
                if buggy_index >= len(buggy_lines) or buggy_lines[buggy_index] != content:
                    raise ValueError("Defects4J patch does not match buggy checkout")
                buggy_index += 1
            if prefix in {" ", "-"}:
                fixed_lines.append(content)
    fixed_lines.extend(buggy_lines[buggy_index:])
    return "\n".join(fixed_lines) + ("\n" if buggy_text.endswith("\n") else "")


def _changed_executables(
    executables: List[JavaExecutable], changed_lines: frozenset[int]
) -> List[JavaExecutable]:
    return [
        item for item in executables
        if any(item.start_line <= line <= item.end_line for line in changed_lines)
    ]


def _buggy_counterpart(
    fixed: JavaExecutable,
    buggy_executables: List[JavaExecutable],
) -> JavaExecutable | None:
    same_function = [
        item for item in buggy_executables if item.function == fixed.function
    ]
    exact = [
        item for item in same_function
        if exact_parameters(item.parameter_types, fixed.parameter_types)
    ]
    compatible = [
        item for item in same_function
        if same_parameters(item.parameter_types, fixed.parameter_types)
    ]
    if len(exact) == 1:
        return exact[0]
    if not exact and len(compatible) == 1:
        return compatible[0]
    if not compatible and len(same_function) == 1:
        return same_function[0]
    return None


def ground_truth_locations(
    d4j_home: Path,
    workspace: Path,
    project: str,
    bug: str,
) -> List[Dict[str, Any]]:
    patch = d4j_home / "framework" / "projects" / project / "patches" / f"{bug}.src.patch"
    if not patch.is_file():
        raise ValueError(f"Defects4J source patch not found: {patch}")
    files = parse_source_patch(patch.read_text(encoding="utf-8", errors="replace"))
    if not files:
        raise ValueError(f"Defects4J source patch contains no Java changes: {patch}")
    locations: set[MethodLocation] = set()
    unresolved = []
    for patched in files:
        source_path = _resolve_source(workspace, patched.path)
        buggy_text = source_path.read_text(encoding="utf-8", errors="replace")
        fixed_text = reconstruct_fixed_source(buggy_text, patched.hunks)
        buggy_executables = java_executables(buggy_text)
        fixed_executables = java_executables(fixed_text)
        matched = _changed_executables(
            buggy_executables, patched.buggy_changed_lines
        )
        for fixed in _changed_executables(
            fixed_executables, patched.fixed_changed_lines
        ):
            counterpart = _buggy_counterpart(fixed, buggy_executables)
            if counterpart is not None and counterpart not in matched:
                matched.append(counterpart)
        if not matched:
            unresolved.append(patched.path)
        relative = source_path.resolve().relative_to(workspace.resolve()).as_posix()
        locations.update(
            MethodLocation(
                function=item.function,
                source_file=relative,
                start_line=item.start_line,
                end_line=item.end_line,
            )
            for item in matched
        )
    if unresolved:
        raise ValueError(
            "cannot map Defects4J patch to buggy methods: " + ", ".join(sorted(unresolved))
        )
    if not locations:
        raise ValueError("Defects4J patch produced no ground-truth methods")
    return [
        item.to_dict()
        for item in sorted(
            locations,
            key=lambda value: (
                value.source_file,
                value.start_line,
                value.end_line,
                value.function,
            ),
        )
    ]


def ground_truth_methods(
    d4j_home: Path,
    workspace: Path,
    project: str,
    bug: str,
) -> List[str]:
    """Compatibility view of source-range ground truth as unique function names."""
    return sorted({
        str(item["function"])
        for item in ground_truth_locations(d4j_home, workspace, project, bug)
    })
