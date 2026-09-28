from pathlib import PurePosixPath
from typing import Any, Dict


METHOD_RECORD_PREFIX = "METHOD"


def format_method_record(name: str, line: str, reason: str) -> str:
    fields = {"method name": name, "method line": line}
    for label, value in fields.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be non-empty text")
        if (
            value != value.strip()
            or "\n" in value
            or "\r" in value
            or "|" in value
        ):
            raise ValueError(f"{label} is not safe for METHOD line format")
    if not isinstance(reason, str):
        raise ValueError("method reason must be text")
    normalized_reason = " ".join(reason.split()) or "(no locator reason provided)"
    return f"{METHOD_RECORD_PREFIX}|{name}|{line}|{normalized_reason}"


def parse_method_line(value: str) -> tuple[str, int]:
    if not isinstance(value, str) or ":" not in value:
        raise ValueError("method line must use relative/path.java:line format")
    source_file, raw_line = value.rsplit(":", 1)
    if not raw_line.isdigit() or int(raw_line) <= 0:
        raise ValueError("method line number must be a positive integer")
    path = PurePosixPath(source_file)
    if (
        not source_file
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in source_file
        or path.suffix != ".java"
    ):
        raise ValueError("unsafe method source path")
    return source_file, int(raw_line)


def parse_model_response(text: str) -> list[Any] | None:
    """Extract well-formed METHOD records that begin at a physical line boundary.

    Non-record output and blank lines are ignored. A record line remains atomic:
    embedding a second METHOD record on that same physical line is invalid.
    """
    if not isinstance(text, str):
        return None
    result = []
    record_prefix = f"{METHOD_RECORD_PREFIX}|"
    for raw_line in text.splitlines():
        if not raw_line.startswith(record_prefix):
            continue
        if raw_line.count(record_prefix) != 1:
            return None
        parts = raw_line.split("|", 3)
        if len(parts) != 4 or parts[0] != METHOD_RECORD_PREFIX:
            return None
        name, line, reason = parts[1:]
        result.append({
            "method": {"name": name, "line": line},
            "reason": reason,
        })
    return result or None


def validate_model_refinement(
    value: list[Any], top_k: int
) -> list[Dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("final ranking must be a list of METHOD lines")
    if not 1 <= len(value) <= top_k:
        raise ValueError(
            f"final ranking must contain between 1 and {top_k} METHOD lines"
        )
    result = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"method", "reason"}:
            raise ValueError(f"invalid refined ranking entry at index {index}")
        method = item["method"]
        if (
            not isinstance(method, dict)
            or set(method) != {"name", "line"}
        ):
            raise ValueError(f"invalid method reference at index {index}")
        name = method["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"invalid method name at index {index}")
        name = name.strip()
        try:
            source_file, line = parse_method_line(method["line"])
        except ValueError as error:
            raise ValueError(
                f"invalid method line at index {index}: {error}"
            ) from error
        identity = (source_file, line)
        if identity in seen:
            raise ValueError(f"duplicate method reference at index {index}")
        reason = item["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"empty reason at index {index}")
        seen.add(identity)
        result.append({
            "method": {
                "name": name,
                "line": f"{source_file}:{line}",
            },
            "reason": reason.strip()[:500],
        })
    return result
