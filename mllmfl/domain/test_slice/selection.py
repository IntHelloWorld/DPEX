from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_java

from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace

from .model import SourceStatement


def select_statements(
    statements: Sequence[SourceStatement], failure_line: int
) -> List[SourceStatement]:
    if not statements or failure_line <= 0:
        return []
    failure_candidates = [
        (index, statement)
        for index, statement in enumerate(statements)
        if statement.kind == "statement" and statement.contains(failure_line)
    ]
    if failure_candidates:
        failure_index, failure = min(
            failure_candidates,
            key=lambda item: (item[1].end_byte - item[1].start_byte, item[1].start_byte),
        )
    else:
        preceding = [
            (index, statement)
            for index, statement in enumerate(statements)
            if statement.kind == "statement" and statement.end_line <= failure_line
        ]
        if not preceding:
            return []
        failure_index, failure = max(preceding, key=lambda item: item[1].end_byte)

    by_key = {statement.key: index for index, statement in enumerate(statements)}
    selected = {failure_index}
    relevant = set(failure.references)
    changed = True
    while changed:
        changed = False
        for index, statement in enumerate(statements):
            if statement.start_byte >= failure.start_byte or statement.kind != "statement":
                continue
            if statement.assertion:
                continue
            defines_relevant = bool(set(statement.definitions) & relevant)
            call_on_relevant = statement.has_call and bool(set(statement.references) & relevant)
            if not defines_relevant and not call_on_relevant:
                continue
            if index not in selected:
                selected.add(index)
                changed = True
            before = len(relevant)
            relevant.update(statement.definitions)
            relevant.update(statement.references)
            changed = changed or len(relevant) != before

        for index in tuple(selected):
            for control_key in statements[index].control_dependencies:
                control_index = by_key.get(control_key)
                if control_index is None:
                    continue
                if control_index not in selected:
                    selected.add(control_index)
                    changed = True
                before = len(relevant)
                relevant.update(statements[control_index].references)
                changed = changed or len(relevant) != before
            for dependency_key in statements[index].exception_dependencies:
                dependency_index = by_key.get(dependency_key)
                if dependency_index is not None and dependency_index not in selected:
                    selected.add(dependency_index)
                    changed = True
    return [statements[index] for index in sorted(selected)]
