import hashlib
import json
import os
import shutil
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import fcntl

from .process import run_command


DEFAULT_PLANTUML_JAR = Path(__file__).resolve().parents[2] / "lib" / "plantuml.jar"


def runtime_settings(
    config: dict[str, Any],
    rendering: dict[str, Any] | None = None,
) -> tuple[str, Path | None]:
    """Resolve non-secret PlantUML runtime settings shared by pipeline stages."""
    uml_cfg = config.get("uml") or {}
    if not isinstance(uml_cfg, dict):
        raise ValueError("uml configuration must be an object")
    artifact_settings = rendering or {}
    command = str(
        uml_cfg.get("plantuml_command")
        or artifact_settings.get("plantuml_command")
        or "plantuml"
    )
    jar_value = (
        uml_cfg["plantuml_jar"]
        if "plantuml_jar" in uml_cfg
        else artifact_settings.get("plantuml_jar", DEFAULT_PLANTUML_JAR)
    )
    jar = (
        Path(str(jar_value)).expanduser().resolve()
        if jar_value
        else None
    )
    return command, jar


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


def validate_png(png: Path, limit_size: int) -> None:
    """Raise when a cached or newly rendered PNG is missing or malformed."""
    if limit_size <= 0:
        raise ValueError("PlantUML limit_size must be positive")
    error = _png_error(png, limit_size)
    if error is not None:
        raise RuntimeError(f"invalid PlantUML image {png}: {error}")


def renderer_available(command: str, jar: Path | None) -> None:
    """Validate that the configured renderer can be resolved without rendering."""
    _command_args(command, jar, [])


def _renderer_identity(command: str, jar: Path | None) -> dict[str, object]:
    executable = shutil.which(command)
    if executable:
        path = Path(executable).resolve()
        stat = path.stat()
        return {
            "kind": "command",
            "path": str(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    if jar and jar.is_file():
        path = jar.resolve()
        stat = path.stat()
        return {
            "kind": "jar",
            "path": str(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    raise RuntimeError("PlantUML executable or jar was not found")


def _render_cache_key(
    puml_path: Path,
    command: str,
    jar: Path | None,
    limit_size: int,
) -> str:
    material = {
        "puml_sha256": hashlib.sha256(puml_path.read_bytes()).hexdigest(),
        "renderer": _renderer_identity(command, jar),
        "limit_size": limit_size,
        "format": "png",
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_rendered(
    puml_path: Path,
    image_path: Path,
    command: str = "plantuml",
    jar: Path | None = None,
    timeout: int = 300,
    limit_size: int = 16384,
) -> tuple[Path, bool]:
    """Render one PUML atomically when its content-addressed PNG cache is absent.

    Returns ``(image_path, cache_hit)``. Only a fully validated PNG and matching
    manifest are reused, so an interrupted render cannot poison later localization.
    """
    puml_path = Path(puml_path)
    image_path = Path(image_path)
    if not puml_path.is_file():
        raise RuntimeError(f"PlantUML source was not found: {puml_path}")
    if puml_path.suffix != ".puml" or image_path.suffix != ".png":
        raise ValueError("on-demand PlantUML paths must use .puml and .png")
    if puml_path.with_suffix(".png") != image_path:
        raise ValueError("on-demand PlantUML image must share its PUML path and stem")
    if limit_size <= 0:
        raise ValueError("PlantUML limit_size must be positive")

    manifest_path = image_path.with_suffix(image_path.suffix + ".render.json")
    lock_path = image_path.with_suffix(image_path.suffix + ".lock")
    with _exclusive_lock(lock_path):
        cache_key = _render_cache_key(puml_path, command, jar, limit_size)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            manifest = None
        if (
            isinstance(manifest, dict)
            and manifest.get("schema") == "plantuml-render-cache"
            and manifest.get("schema_version") == 1
            and manifest.get("cache_key") == cache_key
            and _png_error(image_path, limit_size) is None
        ):
            return image_path, True

        image_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{puml_path.stem}.render-", dir=puml_path.parent
        ) as temp_directory:
            temp_puml = Path(temp_directory) / puml_path.name
            shutil.copyfile(puml_path, temp_puml)
            temp_png = render(
                temp_puml, command, jar, timeout, limit_size
            )
            validate_png(temp_png, limit_size)
            os.replace(temp_png, image_path)

        manifest_value = {
            "schema": "plantuml-render-cache",
            "schema_version": 1,
            "cache_key": cache_key,
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{manifest_path.name}.",
            suffix=".tmp",
            dir=manifest_path.parent,
            delete=False,
        ) as handle:
            json.dump(manifest_value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temp_manifest = Path(handle.name)
        os.replace(temp_manifest, manifest_path)
        return image_path, False


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
