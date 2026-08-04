import os
import shutil
import struct
from pathlib import Path

from .process import run_command


def render(
    puml_path: Path,
    command: str = "plantuml",
    jar: Path | None = None,
    timeout: int = 300,
    limit_size: int = 16384,
) -> Path:
    if limit_size <= 0:
        raise ValueError("PlantUML limit_size must be positive")
    if shutil.which(command):
        args = [command, "-tpng", str(puml_path)]
    elif jar and jar.is_file():
        args = ["java", "-Djava.awt.headless=true", "-jar", str(jar), "-tpng", str(puml_path)]
    else:
        raise RuntimeError("PlantUML executable or jar was not found")
    env = os.environ.copy()
    env["PLANTUML_LIMIT_SIZE"] = str(limit_size)
    result = run_command(args, env=env, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"PlantUML failed: {result.stderr or result.stdout}")
    png = puml_path.with_suffix(".png")
    if not png.is_file():
        raise RuntimeError(f"PlantUML did not create {png}")
    with png.open("rb") as handle:
        header = handle.read(24)
    if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", header[16:24])
        if width >= limit_size or height >= limit_size:
            raise RuntimeError(
                f"PlantUML output reached the {limit_size}px limit ({width}x{height}); "
                "increase --plantuml-limit-size or reduce the UML window"
            )
    return png
