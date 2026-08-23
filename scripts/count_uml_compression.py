#!/usr/bin/env python3
"""Report call counts before and after UML compression for one bug.

The counts come from each trigger's ``execution_compressed.json``:
``source_call_count`` is the selected execution before presentation compression,
and ``displayed_call_count`` is the number of calls retained in the compressed
tree before diagram pagination.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def _safe_component(value: str, label: str) -> str:
    if not re.fullmatch(r"[0-9A-Za-z_.-]+", value):
        raise ValueError(f"invalid {label}: {value}")
    return value


def _trigger_key(path: Path) -> tuple[int, int | str]:
    match = re.fullmatch(r"trigger_(\d+)", path.name)
    if match:
        return 0, int(match.group(1))
    return 1, path.name


def _load_counts(path: Path) -> tuple[int, int]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError("execution_compressed.json is missing") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read compressed execution: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("compressed execution must be a JSON object")
    if (
        value.get("schema") != "fullchain-compressed-execution"
        or value.get("schema_version") not in {1, 2}
    ):
        raise ValueError("unsupported compressed execution schema")
    before = value.get("source_call_count")
    represented = value.get("represented_call_count")
    after = value.get("displayed_call_count")
    if (
        not isinstance(before, int) or before < 0
        or not isinstance(represented, int) or represented != before
        or not isinstance(after, int) or after < 0 or after > before
    ):
        raise ValueError("invalid compressed execution call counts")
    return before, after


def collect_bug_statistics(
    root: Path,
    project: str,
    bug: str,
    trigger: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    project = _safe_component(project, "project")
    bug = _safe_component(bug, "bug")
    trigger_root = root / "artifacts" / project / f"bug_{bug}" / "triggers"
    if not trigger_root.is_dir():
        raise ValueError(f"bug trigger directory does not exist: {trigger_root}")

    if trigger is not None:
        trigger_name = trigger if trigger.startswith("trigger_") else f"trigger_{trigger}"
        trigger_name = _safe_component(trigger_name, "trigger")
        trigger_dirs = [trigger_root / trigger_name]
        if not trigger_dirs[0].is_dir():
            raise ValueError(f"trigger directory does not exist: {trigger_dirs[0]}")
    else:
        trigger_dirs = sorted(
            (
                path for path in trigger_root.iterdir()
                if path.is_dir() and path.name.startswith("trigger_")
            ),
            key=_trigger_key,
        )
        if not trigger_dirs:
            raise ValueError(f"no trigger directories found: {trigger_root}")

    rows: list[dict[str, Any]] = []
    total_before = total_after = 0
    successful = 0
    for trigger_dir in trigger_dirs:
        artifact = trigger_dir / "execution_compressed.json"
        row: dict[str, Any] = {
            "trigger": trigger_dir.name,
            "status": "OK",
            "before": None,
            "after": None,
            "saved": None,
            "reduction_percent": None,
            "compression_ratio": None,
            "artifact": artifact.as_posix(),
            "error": None,
        }
        try:
            before, after = _load_counts(artifact)
            saved = before - after
            row.update({
                "before": before,
                "after": after,
                "saved": saved,
                "reduction_percent": (100.0 * saved / before) if before else 0.0,
                "compression_ratio": (before / after) if after else None,
            })
            total_before += before
            total_after += after
            successful += 1
        except ValueError as error:
            row["status"] = "ERROR"
            row["error"] = str(error)
        rows.append(row)

    total_saved = total_before - total_after
    total = {
        "status": "OK" if successful == len(rows) else "PARTIAL",
        "trigger_count": len(rows),
        "successful_trigger_count": successful,
        "before": total_before,
        "after": total_after,
        "saved": total_saved,
        "reduction_percent": (
            100.0 * total_saved / total_before if total_before else 0.0
        ),
        "compression_ratio": (
            total_before / total_after if total_after else None
        ),
    }
    return rows, total


def _number(value: Any) -> str:
    return "-" if value is None else str(value)


def _percent(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}%"


def _ratio(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}x"


def _print_table(rows: list[dict[str, Any]], total: dict[str, Any]) -> None:
    print(
        "TRIGGER\tSTATUS\tBEFORE\tAFTER\tSAVED\t"
        "REDUCTION\tRATIO\tARTIFACT"
    )
    for row in rows:
        print(
            f"{row['trigger']}\t{row['status']}\t{_number(row['before'])}\t"
            f"{_number(row['after'])}\t{_number(row['saved'])}\t"
            f"{_percent(row['reduction_percent'])}\t"
            f"{_ratio(row['compression_ratio'])}\t{row['artifact']}"
        )
        if row["error"]:
            print(f"error: {row['trigger']}: {row['error']}", file=sys.stderr)
    print(
        f"TOTAL\t{total['status']}\t{total['before']}\t{total['after']}\t"
        f"{total['saved']}\t{_percent(total['reduction_percent'])}\t"
        f"{_ratio(total['compression_ratio'])}\t-"
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Compare UML call counts before and after compression for one bug."
    )
    value.add_argument("--root", type=Path, required=True, help="run root")
    value.add_argument("--project", required=True, help="Defects4J project name")
    value.add_argument("--bug", required=True, help="bug number")
    value.add_argument(
        "--trigger",
        help="optional trigger number or directory name, for example 1 or trigger_1",
    )
    value.add_argument("--json", action="store_true", help="emit JSON instead of TSV")
    return value


def main() -> int:
    args = parser().parse_args()
    root = args.root.expanduser().resolve()
    try:
        rows, total = collect_bug_statistics(
            root, args.project, args.bug, args.trigger
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({
            "root": root.as_posix(),
            "project": args.project,
            "bug": args.bug,
            "rows": rows,
            "total": total,
        }, ensure_ascii=False, indent=2))
    else:
        _print_table(rows, total)
    return 0 if total["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
