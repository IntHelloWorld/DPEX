from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_java

from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace


SLICE_SCHEMA = "test-boundary-slice"
SLICE_SCHEMA_VERSION = 2
JAVA_LANGUAGE = Language(tree_sitter_java.language())

TYPE_DECLARATIONS = {
    "annotation_type_declaration",
    "class_declaration",
    "enum_declaration",
    "interface_declaration",
    "record_declaration",
}
ATOMIC_STATEMENTS = {
    "assert_statement",
    "break_statement",
    "continue_statement",
    "explicit_constructor_invocation",
    "expression_statement",
    "local_variable_declaration",
    "return_statement",
    "throw_statement",
    "yield_statement",
}
NESTED_EXECUTABLE_SCOPES = {
    "annotation_type_body",
    "class_body",
    "constructor_declaration",
    "enum_body",
    "interface_body",
    "lambda_expression",
    "method_declaration",
}
CONTROL_FIELDS = {
    "do_statement": ("condition",),
    "enhanced_for_statement": ("value",),
    "for_statement": ("condition",),
    "if_statement": ("condition",),
    "switch_expression": ("condition",),
    "switch_statement": ("condition",),
    "synchronized_statement": ("expression",),
    "while_statement": ("condition",),
}


@dataclass(frozen=True)
class SourceStatement:
    start_line: int
    end_line: int
    code: str
    start_byte: int = 0
    end_byte: int = 0
    kind: str = "statement"
    definitions: frozenset[str] = field(default_factory=frozenset)
    references: frozenset[str] = field(default_factory=frozenset)
    has_call: bool = False
    assertion: bool = False
    control_dependencies: Tuple[Tuple[int, int], ...] = ()

    def contains(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line

    @property
    def key(self) -> Tuple[int, int]:
        return self.start_byte, self.end_byte


def _parser() -> Parser:
    return Parser(JAVA_LANGUAGE)


def _walk(node: Node) -> Iterable[Node]:
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(reversed(current.named_children))


def _semantic_walk(node: Node) -> Iterable[Node]:
    pending = [node]
    while pending:
        current = pending.pop()
        if current.id != node.id and current.type in NESTED_EXECUTABLE_SCOPES:
            continue
        yield current
        pending.extend(reversed(current.named_children))


def _text(source: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _enclosing_type_names(source: bytes, node: Node) -> List[str]:
    names: List[str] = []
    current = node.parent
    while current is not None:
        if current.type in TYPE_DECLARATIONS:
            name = current.child_by_field_name("name")
            if name is not None:
                names.append(_text(source, name))
        current = current.parent
    names.reverse()
    return names


def _find_test_method(root: Node, source: bytes, class_name: str, method: str) -> Node | None:
    target_types = class_name.rsplit(".", 1)[-1].split("$")
    candidates: List[Node] = []
    for node in _walk(root):
        if node.type != "method_declaration":
            continue
        if _text(source, node.child_by_field_name("name")) != method:
            continue
        enclosing = _enclosing_type_names(source, node)
        if len(enclosing) < len(target_types) or enclosing[-len(target_types) :] != target_types:
            continue
        candidates.append(node)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda node: (
            len((node.child_by_field_name("parameters") or node).named_children),
            node.start_byte,
        ),
    )


def _normalized_expression(source: bytes, node: Node) -> str:
    return "".join(_text(source, node).split())


def _is_named_field(parent: Node, field: str, node: Node) -> bool:
    value = parent.child_by_field_name(field)
    return value is not None and value.id == node.id


def _reference_symbols(source: bytes, root: Node) -> Set[str]:
    result: Set[str] = set()

    def visit(node: Node, is_root: bool = False) -> None:
        if not is_root and node.type in NESTED_EXECUTABLE_SCOPES:
            return
        if node.type == "field_access":
            result.add(_normalized_expression(source, node))
            object_node = node.child_by_field_name("object")
            if object_node is not None:
                visit(object_node)
            return
        if node.type == "identifier":
            parent = node.parent
            if parent is None:
                return
            excluded = (
                _is_named_field(parent, "name", node)
                and parent.type
                in {
                    "annotation_type_declaration",
                    "class_declaration",
                    "constructor_declaration",
                    "enum_declaration",
                    "interface_declaration",
                    "method_declaration",
                    "method_invocation",
                    "record_declaration",
                    "variable_declarator",
                }
            ) or (
                parent.type == "field_access" and _is_named_field(parent, "field", node)
            )
            if not excluded:
                result.add(_text(source, node))
            return
        for child in node.named_children:
            visit(child)

    visit(root, True)
    return result


def _target_symbols(source: bytes, node: Node | None) -> Set[str]:
    if node is None:
        return set()
    if node.type == "identifier":
        return {_text(source, node)}
    if node.type == "field_access":
        return {_normalized_expression(source, node)} | _reference_symbols(source, node)
    if node.type == "array_access":
        return _reference_symbols(source, node.child_by_field_name("array") or node)
    return _reference_symbols(source, node)


def _definition_symbols(source: bytes, statement: Node) -> Set[str]:
    result: Set[str] = set()
    for node in _semantic_walk(statement):
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is not None:
                result.add(_text(source, name))
        elif node.type == "assignment_expression":
            result.update(_target_symbols(source, node.child_by_field_name("left")))
        elif node.type == "update_expression":
            operand = next(iter(node.named_children), None)
            result.update(_target_symbols(source, operand))
    return result


def _write_only_symbols(source: bytes, statement: Node) -> Set[str]:
    result: Set[str] = set()
    for node in _semantic_walk(statement):
        if node.type == "variable_declarator":
            result.update(_target_symbols(source, node.child_by_field_name("name")))
        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and right is not None:
                operator = source[left.end_byte : right.start_byte].decode(
                    "utf-8", errors="ignore"
                ).strip()
                if operator == "=":
                    result.update(_target_symbols(source, left))
    return result


def _contains_call(statement: Node) -> bool:
    return any(
        node.type in {"method_invocation", "object_creation_expression"}
        for node in _semantic_walk(statement)
    )


def _is_assertion(source: bytes, statement: Node) -> bool:
    if statement.type == "assert_statement":
        return True
    for node in _semantic_walk(statement):
        if node.type != "method_invocation":
            continue
        name = _text(source, node.child_by_field_name("name"))
        if name == "fail" or name.startswith("assert"):
            return True
    return False


def _control_conditions(node: Node) -> List[Node]:
    result: List[Node] = []
    for field_name in CONTROL_FIELDS.get(node.type, ()):
        condition = node.child_by_field_name(field_name)
        if condition is not None:
            result.append(condition)
    return result


def _control_ancestors(node: Node, body: Node) -> List[Node]:
    result: List[Node] = []
    current = node.parent
    while current is not None and current.id != body.id:
        result.extend(_control_conditions(current))
        current = current.parent
    result.reverse()
    return result


def _statement_from_node(
    source: bytes,
    node: Node,
    kind: str,
    controls: Sequence[Node] = (),
) -> SourceStatement:
    definitions = _definition_symbols(source, node) if kind == "statement" else set()
    references = _reference_symbols(source, node)
    references.difference_update(_write_only_symbols(source, node))
    return SourceStatement(
        start_line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        code=_text(source, node).strip(),
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        kind=kind,
        definitions=frozenset(definitions),
        references=frozenset(references),
        has_call=_contains_call(node),
        assertion=_is_assertion(source, node),
        control_dependencies=tuple((item.start_byte, item.end_byte) for item in controls),
    )


def extract_statements(java_text: str, class_name: str, method: str) -> List[SourceStatement]:
    source = java_text.encode("utf-8")
    tree = _parser().parse(source)
    test_method = _find_test_method(tree.root_node, source, class_name, method)
    if test_method is None or test_method.has_error:
        return []
    body = test_method.child_by_field_name("body")
    if body is None:
        return []

    atomic: List[Node] = []

    def collect(node: Node) -> None:
        if node.type in ATOMIC_STATEMENTS:
            atomic.append(node)
            return
        if node.id != body.id and node.type in NESTED_EXECUTABLE_SCOPES:
            return
        for child in node.named_children:
            collect(child)

    collect(body)
    controls: Dict[Tuple[int, int], Node] = {}
    statements: List[SourceStatement] = []
    for node in atomic:
        parents = _control_ancestors(node, body)
        for condition in parents:
            controls[(condition.start_byte, condition.end_byte)] = condition
        statements.append(_statement_from_node(source, node, "statement", parents))
    statements.extend(
        _statement_from_node(source, node, "control")
        for _, node in sorted(controls.items())
    )
    return sorted(statements, key=lambda item: (item.start_byte, item.end_byte, item.kind))


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
    return [statements[index] for index in sorted(selected)]


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


def slice_execution(
    execution: Dict[str, Any], source_path: Path | None, class_name: str, method: str
) -> Dict[str, Any]:
    validate_trace(execution, EXECUTION_SCHEMA)
    line = failure_line(execution)
    statements = (
        extract_statements(
            source_path.read_text(encoding="utf-8", errors="ignore"), class_name, method
        )
        if source_path is not None
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
            "reason": "failure line or parseable test method unavailable",
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
    retained_ids: Set[int] = set()
    for call in retained_calls:
        retained_ids.add(int(call["invocation_id"]))
        retained_ids.add(int(call["parent_invocation_id"]))
        retained_ids.update(int(value) for value in call.get("parent_chain") or [])
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
