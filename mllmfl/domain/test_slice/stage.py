from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_java

from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace

from .analysis import extract_statements
from .metadata import failure_line, validate_slice_metadata
from .model import SLICE_SCHEMA, SLICE_SCHEMA_VERSION
from .selection import select_statements


def slice_execution(
    execution: Dict[str, Any], source_path: Path | None, class_name: str, method: str
) -> Dict[str, Any]:
    validate_trace(execution, EXECUTION_SCHEMA)
    test_started = any(
        invocation.get("class") == class_name and invocation.get("method") == method
        for invocation in execution["invocations"]
    )
    line = failure_line(execution)
    statements = (
        extract_statements(
            source_path.read_text(encoding="utf-8", errors="ignore"), class_name, method
        )
        if test_started and source_path is not None
        else []
    )
    selected = select_statements(statements, line)
    metadata: Dict[str, Any] = {
        "schema": SLICE_SCHEMA,
        "schema_version": SLICE_SCHEMA_VERSION,
        "strategy": "tree-sitter-test-method-backward-slice",
        "parser": "tree-sitter-java",
        "source_file": str(source_path) if source_path is not None else "",
        "failure_line": line,
        "fixture_policy": "retain-unmapped-calls",
        "selected_statements": [
            {
                "start_line": statement.start_line,
                "end_line": statement.end_line,
                "kind": statement.kind,
                "definitions": sorted(statement.definitions),
                "references": sorted(statement.references),
                "code": statement.code,
            }
            for statement in selected
        ],
    }
    if not selected:
        result = dict(execution)
        metadata.update({
            "applied": False,
            "reason": (
                "target test method was not entered"
                if not test_started
                else "failure line or parseable test method unavailable"
            ),
            "original_call_count": len(execution["calls"]),
            "retained_call_count": len(execution["calls"]),
        })
        validate_slice_metadata(metadata)
        result["slice"] = metadata
        return result

    ranges = [(item.start_line, item.end_line) for item in selected]

    def selected_line(value: Any) -> bool:
        origin = int(value or 0)
        return origin <= 0 or any(start <= origin <= end for start, end in ranges)

    retained_calls = [
        dict(call) for call in execution["calls"]
        if selected_line(call.get("origin_test_line"))
    ]
    mapped_calls = [
        call for call in retained_calls
        if int(call.get("origin_test_line") or 0) > 0
    ]
    if not mapped_calls:
        result = dict(execution)
        metadata.update({
            "applied": False,
            "reason": "selected statements matched no runtime calls",
            "original_call_count": len(execution["calls"]),
            "retained_call_count": len(execution["calls"]),
        })
        validate_slice_metadata(metadata)
        result["slice"] = metadata
        return result

    retained_ids: Set[int] = set()
    for call in retained_calls:
        retained_ids.add(int(call["invocation_id"]))
        retained_ids.add(int(call["parent_invocation_id"]))
        retained_ids.update(int(value) for value in call.get("parent_chain") or [])
    retained_ids.update(
        int(invocation["invocation_id"])
        for invocation in execution["invocations"]
        if invocation.get("class") == class_name and invocation.get("method") == method
    )
    retained_invocations = [
        dict(invocation) for invocation in execution["invocations"]
        if int(invocation["invocation_id"]) in retained_ids
    ]
    metadata.update({
        "applied": True,
        "reason": "selected failure AST statement and conservative test dependencies",
        "original_call_count": len(execution["calls"]),
        "retained_call_count": len(retained_calls),
    })
    validate_slice_metadata(metadata)
    result = dict(execution)
    result.update({
        "invocations": retained_invocations,
        "calls": retained_calls,
        "call_count": len(retained_calls),
        "slice": metadata,
    })
    validate_trace(result, EXECUTION_SCHEMA)
    return result
