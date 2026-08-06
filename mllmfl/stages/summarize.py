import re
from pathlib import Path
from typing import Dict, List, Sequence

from mllmfl.domain.models import Candidate
from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace
from mllmfl.infrastructure.io import read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.java_source import (
    called_methods,
    extract_methods,
    find_java_file,
    split_function,
)
from mllmfl.infrastructure.layout import RunLayout


def candidate_functions(execution: Dict[str, object], cap: int) -> List[str]:
    if cap <= 0:
        raise ValueError("candidate cap must be positive")
    validate_trace(execution, EXECUTION_SCHEMA)
    result = []
    test = execution.get("test") or {}
    test_function = f"{test.get('class', '')}.{test.get('method', '')}"
    for call in execution["calls"]:
        for function in (str(call["caller"]), str(call["callee"])):
            if function == test_function or function.endswith(".<clinit>") or function in result:
                continue
            result.append(function)
            if len(result) >= cap:
                return result
    return result


def _purpose(method: str, code: str) -> str:
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", method.replace("_", " ")).lower().split()
    first = words[0] if words else ""
    if method == "<init>":
        return "initializes object state"
    if first in {"get", "is", "has", "contains"}:
        return "queries and returns object state"
    if first in {"set", "put", "add", "remove", "clear"}:
        return "updates object state or stored data"
    if first in {"check", "validate", "verify"}:
        return "validates input or object state"
    if first in {"create", "build", "make"}:
        return "constructs a result or helper object"
    if "return " in code:
        return "computes and returns a result from its inputs and current state"
    return "performs the operation indicated by its method name"


def summarize_function(
    workspace: Path,
    function: str,
    max_chars: int,
    max_called: int,
) -> Candidate:
    class_name, method = split_function(function)
    java_file = find_java_file(workspace, class_name)
    if java_file is None:
        return Candidate(
            function,
            class_name,
            method,
            "Implementation details unavailable.",
            "SOURCE_NOT_FOUND",
        )
    text = java_file.read_text(encoding="utf-8", errors="ignore")
    methods = extract_methods(text, class_name, method)
    if not methods:
        return Candidate(function, class_name, method, "Implementation details unavailable.",
                         "METHOD_NOT_FOUND", str(java_file))
    body = methods[0]
    calls = called_methods(body["code"], max_called)
    loc = body["end_line"] - body["start_line"] + 1
    suffix = f" It calls {', '.join(calls)}." if calls else ""
    summary = (
        f"{function} {_purpose(method, body['code'])}; "
        f"implementation spans about {loc} lines.{suffix}"
    )
    if max_chars > 0 and len(summary) > max_chars:
        summary = summary[:max_chars - 3].rstrip() + "..."
    return Candidate(function, class_name, method, summary, "OK", str(java_file),
                     body["signature"], body["start_line"], body["end_line"], calls)


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
    cap: int,
    max_chars: int,
    max_called: int,
    force: bool = False,
) -> List[Dict[str, object]]:
    if cap <= 0:
        raise ValueError("candidate cap must be positive")
    rows = []
    for project, bug, number, directory in layout.discover_triggers(projects, bugs, trigger):
        path = directory / "candidates.json"
        if path.exists() and not force:
            rows.append(
                {
                    "project": project,
                    "bug": bug,
                    "trigger": number,
                    "status": "SKIPPED",
                    "candidate_count": 0,
                }
            )
            continue
        try:
            execution = read_json(directory / "execution.json")
            functions = candidate_functions(execution, cap)
            candidates = [
                summarize_function(
                    layout.workspace_dir(project, bug), value, max_chars, max_called
                )
                for value in functions
            ]
            write_json(path, {
                "schema": "fault-candidates", "schema_version": 1,
                "project": project, "bug": bug, "trigger": number,
                "source_schema": execution["schema"], "candidate_count": len(candidates),
                "candidates": [candidate.to_dict() for candidate in candidates],
            })
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "OK", "candidate_count": len(candidates)})
        except Exception as error:
            write_text(layout.stage_log_dir("summarize", project, bug, number) / "error.log",
                       str(error) + "\n")
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "ERROR", "candidate_count": 0})
    write_csv(layout.logs / "summarize.csv", rows,
              ["project", "bug", "trigger", "status", "candidate_count"])
    return rows
