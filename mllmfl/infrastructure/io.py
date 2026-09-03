import csv
import io
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import zstandard


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON {path}: {error}") from error


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_compact_json(path: Path, value: Any) -> None:
    """Write machine-only JSON without depth-amplified indentation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_zstd_json(path: Path) -> Any:
    try:
        with path.open("rb") as source:
            with zstandard.ZstdDecompressor().stream_reader(source) as compressed:
                return json.load(compressed)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        zstandard.ZstdError,
    ) as error:
        raise ValueError(f"cannot read zstd JSON {path}: {error}") from error


def write_zstd_json(path: Path, value: Any, *, level: int = 1) -> None:
    """Atomically write compact UTF-8 JSON using fast zstd compression."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("wb") as target:
            with zstandard.ZstdCompressor(level=level).stream_writer(target) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8") as text:
                    json.dump(
                        value,
                        text,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
        temporary.replace(path)
    except (OSError, TypeError, ValueError, zstandard.ZstdError):
        temporary.unlink(missing_ok=True)
        raise


def compress_zstd_file(source: Path, target: Path, *, level: int = 1) -> None:
    """Atomically compress one file without loading it into memory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with source.open("rb") as input_handle, temporary.open("wb") as output_handle:
            compressor = zstandard.ZstdCompressor(level=level)
            compressor.copy_stream(input_handle, output_handle)
        temporary.replace(target)
    except (OSError, zstandard.ZstdError):
        temporary.unlink(missing_ok=True)
        raise


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        writer.writerows(rows)
