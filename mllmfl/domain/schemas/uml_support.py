from pathlib import Path, PurePosixPath
from typing import Any


def artifact_path(
    value: Any,
    field: str,
    base_dir: Path | None,
    *,
    must_exist: bool = True,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid UML segment {field}")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError(f"unsafe UML segment {field}: {value}")
    if base_dir is not None:
        target = (base_dir / Path(*relative.parts)).resolve()
        root = base_dir.resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"UML segment {field} escapes trigger directory: {value}")
        if must_exist and not target.is_file():
            raise ValueError(f"UML segment {field} not found: {value}")
    return value
