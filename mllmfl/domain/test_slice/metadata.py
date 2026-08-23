from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_java

from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace

from .model import SLICE_SCHEMA, SLICE_SCHEMA_VERSION


def failure_line(execution: Dict[str, Any]) -> int:
    for failure in execution.get("test_failures") or []:
        line = int(failure.get("source_line") or 0)
        if line > 0:
            return line
    return 0


def validate_slice_metadata(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("test slice metadata must be an object")
    if value.get("schema") != SLICE_SCHEMA:
        raise ValueError(f"unsupported test slice schema: {value.get('schema')!r}")
    if value.get("schema_version") != SLICE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported test slice schema_version: {value.get('schema_version')!r}"
        )
    if not isinstance(value.get("applied"), bool):
        raise ValueError("test slice applied must be boolean")
    statements = value.get("selected_statements")
    if not isinstance(statements, list):
        raise ValueError("test slice selected_statements must be an array")
    for index, statement in enumerate(statements):
        if not isinstance(statement, dict):
            raise ValueError(f"invalid selected statement at index {index}")
        if statement.get("kind") not in {"statement", "control"}:
            raise ValueError(f"invalid selected statement kind at index {index}")
        if not isinstance(statement.get("definitions"), list) or not isinstance(
            statement.get("references"), list
        ):
            raise ValueError(f"invalid selected statement symbols at index {index}")
        start = statement.get("start_line")
        end = statement.get("end_line")
        if not isinstance(start, int) or not isinstance(end, int) or start <= 0 or end < start:
            raise ValueError(f"invalid selected statement lines at index {index}")
    return value
