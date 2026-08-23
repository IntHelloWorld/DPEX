import os
import shutil
import struct
from pathlib import Path

from .process import run_command


def _command_args(command: str, jar: Path | None, paths: list[Path]) -> list[str]:
    if shutil.which(command):
        return [command, "-tpng", *(str(path) for path in paths)]
    if jar and jar.is_file():
        return [
            "java", "-Djava.awt.headless=true", "-jar", str(jar), "-tpng",
            *(str(path) for path in paths),
        ]
    raise RuntimeError("PlantUML executable or jar was not found")


def _png_error(png: Path, limit_size: int) -> str | None:
    if not png.is_file():
        return "PlantUML did not create the expected PNG"
    try:
        with png.open("rb") as handle:
            header = handle.read(24)
    except OSError as error:
        return f"cannot read generated PNG: {error}"
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return "generated file is not a valid PNG with an IHDR header"
    width, height = struct.unpack(">II", header[16:24])
    if width >= limit_size or height >= limit_size:
        return (
            f"PlantUML output reached the {limit_size}px limit "
            f"({width}x{height})"
        )
    return None


def render_many(
    puml_paths: list[Path],
    command: str = "plantuml",
    jar: Path | None = None,
    timeout: int = 300,
    limit_size: int = 16384,
    batch_size: int = 100,
) -> tuple[dict[Path, Path], dict[Path, str]]:
    """Render PlantUML files in batches and report every unsuccessful output."""
    if limit_size <= 0:
        raise ValueError("PlantUML limit_size must be positive")
    if batch_size <= 0:
        raise ValueError("PlantUML batch_size must be positive")
    paths = [Path(path) for path in puml_paths]
    successes: dict[Path, Path] = {}
    failures: dict[Path, str] = {}
    env = os.environ.copy()
    env["PLANTUML_LIMIT_SIZE"] = str(limit_size)
    for start in range(0, len(paths), batch_size):
        batch = paths[start : start + batch_size]
        for path in batch:
            path.with_suffix(".png").unlink(missing_ok=True)
        try:
            args = _command_args(command, jar, batch)
        except RuntimeError as error:
            for path in batch:
                failures[path] = str(error)
            continue
        result = run_command(
            args, env=env, timeout=timeout
        )
        command_error = ""
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().replace("\n", " ")
            command_error = f"PlantUML batch exited {result.returncode}"
            if detail:
                command_error += f": {detail[:1000]}"
        for path in batch:
            png = path.with_suffix(".png")
            error = _png_error(png, limit_size)
            if error is None:
                successes[path] = png
            else:
                failures[path] = f"{error}; {command_error}" if command_error else error
    return successes, failures


def render(
    puml_path: Path,
    command: str = "plantuml",
    jar: Path | None = None,
    timeout: int = 300,
    limit_size: int = 16384,
) -> Path:
    successes, failures = render_many(
        [puml_path], command, jar, timeout, limit_size, batch_size=1
    )
    if puml_path in failures:
        raise RuntimeError(f"PlantUML failed for {puml_path}: {failures[puml_path]}")
    return successes[puml_path]
