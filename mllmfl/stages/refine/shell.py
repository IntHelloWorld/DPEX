import os
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Any, Dict


MAX_COMMAND_CHARS = 20000
MAX_OUTPUT_CHARS = 50000


BASH_TOOL = {
    "type": "function",
    "name": "bash",
    "description": (
        "Run one complete Bash command in the buggy Defects4J project root. "
        "stdout and stderr are returned as one merged text stream. Use this tool "
        "only to inspect buggy-project files; do not modify files. Repository "
        "history, patches, fixed versions, and paths outside the project are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Complete command string evaluated by /bin/bash -c.",
            },
            "max_output_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_OUTPUT_CHARS,
                "description": "Maximum number of merged output characters to return.",
            },
        },
        "required": ["command", "max_output_chars"],
        "additionalProperties": False,
    },
    "strict": True,
}

STATIC_BASH_TOOL = {
    **BASH_TOOL,
    "description": (
        "Run one complete Bash command in the buggy Defects4J project root. "
        "Only static file-inspection commands such as rg, grep, sed, cat, and find "
        "are allowed: do not compile, run tests, execute project code, or invoke "
        "a language runtime. stdout and "
        "stderr are returned as one merged text stream. Do not modify files or "
        "inspect repository history, patches, fixed versions, or paths outside "
        "the project."
    ),
}

STATIC_ALLOWED_COMMANDS = {
    "basename", "cat", "cut", "dirname", "echo", "file", "find", "grep",
    "head", "ls", "nl", "pwd", "rg", "sed", "sort", "stat", "tail", "tr",
    "uniq", "wc",
}
STATIC_COMMAND_SEPARATORS = {"&&", ";", "|", "||"}


def _validate_static_tokens(value: str, tokens: list[str]) -> None:
    if "$(" in value or "`" in value:
        raise ValueError(
            "bash-only agent permits static source inspection commands only"
        )
    expect_command = True
    for index, token in enumerate(tokens):
        if token in STATIC_COMMAND_SEPARATORS:
            expect_command = True
            continue
        if token in {">", ">>"}:
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            if target != "/dev/null":
                raise ValueError(
                    "bash-only agent permits static source inspection commands only"
                )
            continue
        if token in {"&", "(", ")", "<", "<<", "<<<", "|&"}:
            raise ValueError(
                "bash-only agent permits static source inspection commands only"
            )
        if expect_command:
            if Path(token.lower()).name not in STATIC_ALLOWED_COMMANDS:
                raise ValueError(
                    "bash-only agent permits static source inspection commands only"
                )
            expect_command = False
    lowered_tokens = {token.lower() for token in tokens}
    if (
        {"-delete", "-exec", "-execdir", "-ok", "-okdir"} & lowered_tokens
        or any(
            token == "-i" or token.startswith("-i.")
            for token in lowered_tokens
        )
        or {"--pre", "--pre-glob"} & lowered_tokens
        or any(token.startswith("--pre=") for token in lowered_tokens)
    ):
        raise ValueError(
            "bash-only agent permits static source inspection commands only"
        )


def validate_bash_command(
    value: object,
    workspace: Path | None = None,
    *,
    static_only: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("command must be a non-empty string")
    if "\x00" in value:
        raise ValueError("command must not contain NUL characters")
    if len(value) > MAX_COMMAND_CHARS:
        raise ValueError(f"command exceeds {MAX_COMMAND_CHARS} characters")
    try:
        lexer = shlex.shlex(value, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as error:
        raise ValueError(f"command has invalid shell syntax: {error}") from error
    forbidden_commands = {"git", "defects4j", "patch"}
    if static_only:
        _validate_static_tokens(value, tokens)
    for token in tokens:
        lowered = token.lower()
        if Path(lowered).name in forbidden_commands:
            raise ValueError(
                "command must not access repository history, patches, or fixed versions"
            )
        if (
            lowered == ".git"
            or lowered.startswith(".git/")
            or lowered.endswith("/.git")
            or "/.git/" in lowered
            or lowered.endswith(".src.patch")
            or "/patches/" in lowered
        ):
            raise ValueError(
                "command must not access repository history, patches, or fixed versions"
            )
    if workspace is not None:
        resolved_workspace = workspace.resolve()
        for token in tokens:
            if token in {"..", "/"} or token.startswith("../"):
                raise ValueError("command must remain inside the buggy-project workspace")
            if not token.startswith("/") or token == "/dev/null":
                continue
            try:
                Path(token).resolve().relative_to(resolved_workspace)
            except ValueError as error:
                raise ValueError(
                    "command must remain inside the buggy-project workspace"
                ) from error
    return value


def validate_max_output_chars(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_output_chars must be an integer")
    if value <= 0 or value > MAX_OUTPUT_CHARS:
        raise ValueError(
            f"max_output_chars must be between 1 and {MAX_OUTPUT_CHARS}"
        )
    return value


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def execute_bash(
    command_value: object,
    max_output_chars_value: object,
    workspace: Path,
    timeout: int,
    *,
    static_only: bool = False,
) -> Dict[str, Any]:
    try:
        command = validate_bash_command(
            command_value, workspace, static_only=static_only
        )
        max_output_chars = validate_max_output_chars(max_output_chars_value)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ValueError(f"buggy-project workspace is not a directory: {workspace}")
    if timeout <= 0:
        raise ValueError("terminal timeout must be positive")

    process = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        cwd=str(workspace),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
        output, _ = process.communicate()
        output = (output or "") + f"\n[TIMEOUT] {timeout}s\n"
    output = output or ""
    original_output_chars = len(output)
    truncated = original_output_chars > max_output_chars
    result: Dict[str, Any] = {
        "ok": not timed_out and process.returncode == 0,
        "exit_code": 124 if timed_out else process.returncode,
        "output": output[:max_output_chars],
        "truncated": truncated,
    }
    if truncated:
        result["original_output_chars"] = original_output_chars
    return result
