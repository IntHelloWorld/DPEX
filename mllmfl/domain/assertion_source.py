from typing import Iterator

from tree_sitter import Language, Node, Parser
import tree_sitter_java


JAVA_LANGUAGE = Language(tree_sitter_java.language())
TYPE_DECLARATIONS = {
    "annotation_type_declaration",
    "class_declaration",
    "enum_declaration",
    "interface_declaration",
    "record_declaration",
}
NESTED_EXECUTABLE_SCOPES = {
    "constructor_declaration",
    "lambda_expression",
    "method_declaration",
}


def _text(source: bytes, node: Node | None) -> str:
    return "" if node is None else source[node.start_byte:node.end_byte].decode("utf-8")


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _enclosing_type_names(source: bytes, node: Node) -> list[str]:
    names = []
    current = node.parent
    while current is not None:
        if current.type in TYPE_DECLARATIONS:
            name = current.child_by_field_name("name")
            if name is not None:
                names.append(_text(source, name))
        current = current.parent
    names.reverse()
    return names


def _test_method(
    root: Node, source: bytes, class_name: str, method: str
) -> Node | None:
    target_types = class_name.rsplit(".", 1)[-1].split("$")
    candidates = []
    for node in _walk(root):
        if node.type != "method_declaration":
            continue
        if _text(source, node.child_by_field_name("name")) != method:
            continue
        enclosing = _enclosing_type_names(source, node)
        if (
            len(enclosing) >= len(target_types)
            and enclosing[-len(target_types):] == target_types
        ):
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


def _contains_assertion(source: bytes, statement: Node) -> bool:
    if statement.type == "assert_statement":
        return True
    for node in _walk(statement):
        if node.type != "method_invocation":
            continue
        name = _text(source, node.child_by_field_name("name"))
        if name == "fail" or name.startswith("assert"):
            return True
    return False


def assertion_ranges(
    java_text: str, class_name: str, method: str
) -> list[tuple[int, int]]:
    """Return source ranges for assertion statements in one test method."""
    source = java_text.encode("utf-8")
    parser = Parser(JAVA_LANGUAGE)
    tree = parser.parse(source)
    selected = _test_method(tree.root_node, source, class_name, method)
    if selected is None or selected.has_error:
        return []
    body = selected.child_by_field_name("body")
    if body is None:
        return []

    ranges = set()

    def collect(node: Node) -> None:
        if node.id != body.id and node.type in NESTED_EXECUTABLE_SCOPES:
            return
        if node.type in {"assert_statement", "expression_statement"}:
            if _contains_assertion(source, node):
                ranges.add((node.start_point.row + 1, node.end_point.row + 1))
            return
        for child in node.named_children:
            collect(child)

    collect(body)
    return sorted(ranges)
