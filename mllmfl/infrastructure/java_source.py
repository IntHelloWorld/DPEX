import re
from pathlib import Path
from typing import Any, Dict, List, Optional


IGNORED_PARTS = {"target", "build", "bin", "classes", ".gradle", ".git"}


def split_function(function: str) -> tuple[str, str]:
    value = re.sub(r"\.+", ".", function.replace("/", ".").strip("."))
    if "." not in value:
        return "", value
    return value.rsplit(".", 1)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def find_java_file(workspace: Path, class_name: str) -> Optional[Path]:
    outer = class_name.split("$")[0]
    filename = outer.rsplit(".", 1)[-1] + ".java"
    package = outer.rsplit(".", 1)[0] if "." in outer else ""
    candidates = [
        path
        for path in workspace.rglob(filename)
        if not IGNORED_PARTS.intersection(path.parts)
    ]
    for path in candidates:
        match = re.search(r"^\s*package\s+([A-Za-z_$][\w.$]*)\s*;", _read(path), re.M)
        if (match.group(1) if match else "") == package:
            return path
    return candidates[0] if candidates else None


def find_matching_brace(text: str, open_pos: int) -> int:
    depth = 0
    state = "code"
    escaped = False
    index = open_pos
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "line":
            if char == "\n":
                state = "code"
        elif state == "block":
            if char == "*" and nxt == "/":
                state = "code"
                index += 1
        elif state in {"string", "char"}:
            quote = '"' if state == "string" else "'"
            if char == quote and not escaped:
                state = "code"
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
        elif char == "/" and nxt == "/":
            state = "line"
            index += 1
        elif char == "/" and nxt == "*":
            state = "block"
            index += 1
        elif char == '"':
            state = "string"
        elif char == "'":
            state = "char"
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def extract_methods(java_text: str, class_name: str, method: str) -> List[Dict[str, Any]]:
    simple_class = class_name.rsplit(".", 1)[-1].split("$")[0]
    name = simple_class if method == "<init>" else method
    pattern = re.compile(
        r"(?P<signature>(?:@[\w.]+(?:\([^)]*\))?\s*)*"
        r"(?:(?:public|protected|private|static|final|native|synchronized|"
        r"abstract|strictfp|default)\s+)*"
        + (r"(?:<[^>{};]+>\s*)?[\w$<>\[\],.?]+\s+" if method != "<init>" else "")
        + re.escape(name) + r"\s*\([^;{}]*\)\s*(?:throws\s+[^{;]+)?\s*)\{",
        re.S,
    )
    results = []
    for match in pattern.finditer(java_text):
        close = find_matching_brace(java_text, match.end() - 1)
        if close < 0:
            continue
        start_line = java_text.count("\n", 0, match.start()) + 1
        end_line = java_text.count("\n", 0, close) + 1
        results.append({
            "signature": re.sub(r"\s+", " ", match.group("signature").strip()),
            "code": java_text[match.start() : close + 1],
            "start_line": start_line,
            "end_line": end_line,
        })
    return results
