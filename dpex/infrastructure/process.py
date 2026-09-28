import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_command(
    command: Sequence[str],
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: int = 300,
) -> CommandResult:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    try:
        result = subprocess.run(
            [str(item) for item in command],
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env else os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return CommandResult(result.returncode, result.stdout or "", result.stderr or "")
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        return CommandResult(124, stdout, stderr + f"\n[TIMEOUT] {timeout}s\n")


def run_command_with_zstd_fifo(
    command: Sequence[str],
    *,
    fifo_path: Path,
    target: Path,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: int = 300,
    zstd: Path = Path("/usr/bin/zstd"),
) -> CommandResult:
    """Run Java while an external zstd process drains its named-pipe trace.

    The final path is promoted only after the compressor exits successfully and
    ``zstd -t`` confirms a complete stream.  A corrupt partial stream is retained
    under an explicit ``.incomplete`` name for diagnosis.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if not zstd.is_file():
        raise FileNotFoundError(f"zstd executable not found: {zstd}")
    fifo_path.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    fifo_path.unlink(missing_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    incomplete = target.with_name(target.name.removesuffix(".zst") + ".incomplete.zst")
    temporary.unlink(missing_ok=True)
    incomplete.unlink(missing_ok=True)
    os.mkfifo(fifo_path, 0o600)
    compressor = None
    compressor_stderr = b""
    result: CommandResult | None = None
    try:
        with temporary.open("wb") as output:
            compressor = subprocess.Popen(
                [str(zstd), "-1", "--stdout", "--quiet", str(fifo_path)],
                cwd=str(cwd) if cwd else None,
                env=dict(env) if env else os.environ.copy(),
                stdout=output,
                stderr=subprocess.PIPE,
            )
            result = run_command(command, cwd=cwd, env=env, timeout=timeout)
            try:
                _, compressor_stderr = compressor.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                compressor.terminate()
                try:
                    _, compressor_stderr = compressor.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    compressor.kill()
                    _, compressor_stderr = compressor.communicate()
                raise RuntimeError(
                    "zstd compression process did not finish after the Java process"
                )
        if compressor.returncode != 0:
            if temporary.exists():
                temporary.replace(incomplete)
            detail = compressor_stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"zstd compression failed with exit {compressor.returncode}"
                + (f": {detail}" if detail else "")
            )
        verification = subprocess.run(
            [str(zstd), "--test", "--quiet", str(temporary)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        if verification.returncode != 0:
            temporary.replace(incomplete)
            detail = verification.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                "incomplete or corrupt Zstd trace stream"
                + (f": {detail}" if detail else "")
            )
        temporary.replace(target)
        return result
    except Exception:
        if compressor is not None and compressor.poll() is None:
            compressor.terminate()
            try:
                compressor.wait(timeout=5)
            except subprocess.TimeoutExpired:
                compressor.kill()
                compressor.wait()
        if temporary.exists():
            temporary.replace(incomplete)
        raise
    finally:
        fifo_path.unlink(missing_ok=True)
