from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_java

from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace

from .model import (
    ATOMIC_STATEMENTS,
    CONTROL_FIELDS,
    JAVA_LANGUAGE,
    NESTED_EXECUTABLE_SCOPES,
    TYPE_DECLARATIONS,
    SourceStatement,
)


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


def _enclosing_try_ids(node: Node, body: Node) -> Set[int]:
    result: Set[int] = set()
    current = node.parent
    while current is not None and current.id != body.id:
        if current.type == "try_statement":
            result.add(current.id)
        current = current.parent
    return result


def _statement_from_node(
    source: bytes,
    node: Node,
    kind: str,
    controls: Sequence[Node] = (),
    exception_dependencies: Sequence[Tuple[int, int]] = (),
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
        exception_dependencies=tuple(exception_dependencies),
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
    try_ids = {node.id: _enclosing_try_ids(node, body) for node in atomic}
    call_keys = {
        node.id: (node.start_byte, node.end_byte)
        for node in atomic
        if _contains_call(node)
    }
    for node in atomic:
        parents = _control_ancestors(node, body)
        for condition in parents:
            controls[(condition.start_byte, condition.end_byte)] = condition
        exception_dependencies = [
            call_keys[candidate.id]
            for candidate in atomic
            if candidate.id in call_keys
            and candidate.start_byte < node.start_byte
            and try_ids[node.id].intersection(try_ids[candidate.id])
        ]
        statements.append(_statement_from_node(
            source, node, "statement", parents, exception_dependencies
        ))
    statements.extend(
        _statement_from_node(source, node, "control")
        for _, node in sorted(controls.items())
    )
    return sorted(statements, key=lambda item: (item.start_byte, item.end_byte, item.kind))
