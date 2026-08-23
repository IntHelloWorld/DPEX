#!/usr/bin/env python3
"""Recursively report PNG dimensions under a result path."""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Any, Iterable


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ValueError("invalid PNG header or missing IHDR")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("PNG dimensions must be positive")
    return width, height


def png_files(root: Path, recursive: bool) -> Iterable[Path]:
    if root.is_file():
        if root.suffix.lower() == ".png":
            yield root
        return
    iterator = root.rglob("*") if recursive else root.iterdir()
    yield from sorted(
        path for path in iterator
        if path.is_file() and path.suffix.lower() == ".png"
    )


def diagram_metadata(root: Path, recursive: bool) -> dict[Path, dict[str, Any]]:
    """Index UML segment metadata by its absolute PNG path."""
    search_root = root if root.is_dir() else root.parent
    index_paths = (
        search_root.rglob("uml.json") if recursive else search_root.glob("uml.json")
    )
    result: dict[Path, dict[str, Any]] = {}
    for index_path in sorted(index_paths):
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for segment in payload.get("nodes") or payload.get("segments") or []:
            if not isinstance(segment, dict) or not segment.get("image"):
                continue
            image_path = (index_path.parent / str(segment["image"])).resolve()
            result[image_path] = segment
    return result


def diagram_call_counts(segment: dict[str, Any]) -> tuple[int, int]:
    """Return represented and visible calls for either UML index generation."""
    represented = segment.get("represented_call_count", segment.get("call_count", 0))
    visible = segment.get("visible_call_count", segment.get("displayed_call_count", 0))
    if (
        not isinstance(represented, int)
        or isinstance(represented, bool)
        or represented < 0
        or not isinstance(visible, int)
        or isinstance(visible, bool)
        or visible < 0
    ):
        return 0, 0
    return represented, visible


def puml_statistics(path: Path) -> tuple[int, int]:
    """Return participant count and maximum visible activation depth."""
    depth = maximum = 0
    participants = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("participant "):
            participants += 1
        elif line.startswith("activate "):
            depth += 1
            maximum = max(maximum, depth)
        elif line.startswith("deactivate "):
            depth = max(0, depth - 1)
    return participants, maximum


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="List PNG resolutions and flag images exceeding configured limits."
    )
    value.add_argument("result_path", type=Path, help="PNG file or result directory")
    value.add_argument(
        "--max-width", type=int, default=32768,
        help="maximum allowed width; use 0 to disable (default: 32768)",
    )
    value.add_argument(
        "--max-height", type=int, default=32768,
        help="maximum allowed height; use 0 to disable (default: 32768)",
    )
    value.add_argument(
        "--no-recursive", action="store_true",
        help="inspect only PNG files directly inside the result directory",
    )
    return value


def main() -> int:
    args = parser().parse_args()
    root = args.result_path.expanduser().resolve()
    if not root.exists():
        print(f"error: path does not exist: {root}", file=sys.stderr)
        return 2
    if args.max_width < 0 or args.max_height < 0:
        print("error: dimension limits cannot be negative", file=sys.stderr)
        return 2

    paths = list(png_files(root, not args.no_recursive))
    if not paths:
        print(f"No PNG files found under {root}")
        return 0

    metadata = diagram_metadata(root, not args.no_recursive)
    print(
        "STATUS\tWIDTH\tHEIGHT\tMEGAPIXELS\tSIZE_MIB\t"
        "CALLS\tDISPLAYED_CALLS\tPARTICIPANTS\tMAX_DEPTH\tPATH"
    )
    valid = invalid = too_large = 0
    metadata_matched = 0
    for path in paths:
        display_path = path.relative_to(root) if root.is_dir() else path.name
        try:
            width, height = png_dimensions(path)
            exceeds = (
                (args.max_width > 0 and width > args.max_width)
                or (args.max_height > 0 and height > args.max_height)
            )
            status = "TOO_LARGE" if exceeds else "OK"
            too_large += int(exceeds)
            valid += 1
            megapixels = width * height / 1_000_000
            size_mib = path.stat().st_size / (1024 * 1024)
            segment = metadata.get(path.resolve())
            calls: int | str = "-"
            displayed_calls: int | str = "-"
            participants: int | str = "-"
            max_depth: int | str = "-"
            if segment is not None:
                metadata_matched += 1
                calls, displayed_calls = diagram_call_counts(segment)
                puml_value = segment.get("puml")
                if puml_value:
                    puml_path = path.parent.parent / str(puml_value)
                    try:
                        participants, max_depth = puml_statistics(puml_path)
                    except (OSError, UnicodeError):
                        participants = "-"
                        max_depth = "-"
            print(
                f"{status}\t{width}\t{height}\t{megapixels:.2f}\t"
                f"{size_mib:.2f}\t{calls}\t{displayed_calls}\t"
                f"{participants}\t{max_depth}\t{display_path}"
            )
        except (OSError, ValueError, struct.error) as error:
            invalid += 1
            print(f"INVALID\t-\t-\t-\t-\t-\t-\t-\t-\t{display_path}: {error}")

    print(
        f"Summary: total={len(paths)} valid={valid} "
        f"too_large={too_large} invalid={invalid} "
        f"metadata_matched={metadata_matched} metadata_missing={valid - metadata_matched}"
    )
    return 1 if too_large or invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
