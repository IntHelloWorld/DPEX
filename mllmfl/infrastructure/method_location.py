import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

from tree_sitter import Language, Node, Parser
import tree_sitter_java

from mllmfl.infrastructure.java_source import find_java_file, split_function


JAVA_LANGUAGE = Language(tree_sitter_java.language())
TYPE_DECLARATIONS = {
    "annotation_type_declaration",
    "class_declaration",
    "enum_declaration",
    "interface_declaration",
    "record_declaration",
}
PRIMITIVE_DESCRIPTORS = {
    "Z": "boolean",
    "B": "byte",
    "C": "char",
    "S": "short",
    "I": "int",
    "J": "long",
    "F": "float",
    "D": "double",
}


@dataclass(frozen=True)
class JavaExecutable:
    function: str
    parameter_types: tuple[str, ...]
    start_line: int
    end_line: int


@dataclass(frozen=True)
class MethodLocation:
    function: str
    source_file: str
    start_line: int
    end_line: int

    def to_dict(self) -> dict[str, object]:
        return {
            "function": self.function,
            "source_file": self.source_file,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


def _walk(node: Node) -> Iterable[Node]:
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(reversed(current.named_children))


def _text(source: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _type_names(source: bytes, node: Node) -> List[str]:
    names = []
    current = node.parent
    while current is not None:
        if current.type in TYPE_DECLARATIONS:
            name = _text(source, current.child_by_field_name("name"))
            if name:
                names.append(name)
        current = current.parent
    names.reverse()
    return names


def _without_generics(value: str) -> str:
    output = []
    depth = 0
    for character in value:
        if character == "<":
            depth += 1
        elif character == ">" and depth:
            depth -= 1
        elif depth == 0:
            output.append(character)
    return "".join(output)


def normalize_java_type(value: str) -> str:
    normalized = _without_generics(value).strip()
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.replace("...", "[]").replace("$", ".")
    return normalized


def _parameter_types(source: bytes, node: Node) -> tuple[str, ...]:
    parameters = node.child_by_field_name("parameters")
    if parameters is None:
        return ()
    result = []
    for parameter in parameters.named_children:
        if parameter.type == "receiver_parameter":
            continue
        type_node = parameter.child_by_field_name("type")
        if type_node is None:
            continue
        value = normalize_java_type(_text(source, type_node))
        dimensions = parameter.child_by_field_name("dimensions")
        if dimensions is not None:
            value += re.sub(r"[^\[\]]", "", _text(source, dimensions))
        if parameter.type == "spread_parameter":
            value += "[]"
        result.append(value)
    return tuple(result)


def java_executables(java_text: str) -> List[JavaExecutable]:
    source = java_text.encode("utf-8")
    tree = Parser(JAVA_LANGUAGE).parse(source)
    if tree.root_node.has_error:
        raise ValueError("cannot parse buggy Java source")
    package_match = re.search(
        r"^\s*package\s+([A-Za-z_$][\w.$]*)\s*;", java_text, re.MULTILINE
    )
    package = package_match.group(1) if package_match else ""
    result = []
    for node in _walk(tree.root_node):
        if node.type not in {"method_declaration", "constructor_declaration"}:
            continue
        types = _type_names(source, node)
        if not types:
            continue
        class_name = "$".join(types)
        if package:
            class_name = f"{package}.{class_name}"
        method = (
            "<init>"
            if node.type == "constructor_declaration"
            else _text(source, node.child_by_field_name("name"))
        )
        if not method:
            continue
        result.append(JavaExecutable(
            function=f"{class_name}.{method}",
            parameter_types=_parameter_types(source, node),
            start_line=node.start_point.row + 1,
            end_line=node.end_point.row + 1,
        ))
    return result


def descriptor_parameter_types(descriptor: str) -> tuple[str, ...]:
    if not descriptor.startswith("("):
        raise ValueError("invalid JVM method descriptor")

    def parse(index: int) -> tuple[str, int]:
        arrays = 0
        while index < len(descriptor) and descriptor[index] == "[":
            arrays += 1
            index += 1
        if index >= len(descriptor):
            raise ValueError("invalid JVM method descriptor")
        marker = descriptor[index]
        if marker == "L":
            end = descriptor.find(";", index)
            if end < 0:
                raise ValueError("invalid JVM method descriptor")
            value = descriptor[index + 1 : end].replace("/", ".")
            index = end + 1
        elif marker in PRIMITIVE_DESCRIPTORS:
            value = PRIMITIVE_DESCRIPTORS[marker]
            index += 1
        else:
            raise ValueError("invalid JVM method descriptor")
        return normalize_java_type(value) + "[]" * arrays, index

    result = []
    index = 1
    while index < len(descriptor) and descriptor[index] != ")":
        value, index = parse(index)
        result.append(value)
    if index >= len(descriptor) or descriptor[index] != ")":
        raise ValueError("invalid JVM method descriptor")
    return tuple(result)


def signature_parameter_types(signature: str) -> tuple[str, ...]:
    start = signature.rfind("(")
    if start < 0 or not signature.endswith(")"):
        raise ValueError("invalid readable method signature")
    content = signature[start + 1 : -1].strip()
    if not content:
        return ()
    return tuple(normalize_java_type(item) for item in content.split(","))


def _same_type(left: str, right: str) -> bool:
    if left == right:
        return True
    return left.rsplit(".", 1)[-1] == right.rsplit(".", 1)[-1]


def same_parameters(
    left: Sequence[str], right: Sequence[str]
) -> bool:
    return len(left) == len(right) and all(
        _same_type(first, second) for first, second in zip(left, right)
    )


def exact_parameters(
    left: Sequence[str], right: Sequence[str]
) -> bool:
    return tuple(left) == tuple(right)


def resolve_method_location(
    workspace: Path,
    function: str,
    *,
    descriptor: str = "",
    signature: str = "",
) -> MethodLocation:
    class_name, _ = split_function(function)
    source_path = find_java_file(workspace, class_name)
    if source_path is None:
        raise ValueError(f"source file not found for method: {function}")
    executables = [
        item for item in java_executables(
            source_path.read_text(encoding="utf-8", errors="replace")
        )
        if item.function == function
    ]
    if not executables:
        raise ValueError(f"source method not found: {function}")
    expected = (
        descriptor_parameter_types(descriptor)
        if descriptor
        else signature_parameter_types(signature)
    )
    exact_matches = [
        item for item in executables
        if exact_parameters(item.parameter_types, expected)
    ]
    compatible_matches = [
        item for item in executables
        if same_parameters(item.parameter_types, expected)
    ]
    if len(exact_matches) == 1:
        selected = exact_matches[0]
    elif not exact_matches and len(compatible_matches) == 1:
        selected = compatible_matches[0]
    elif not compatible_matches and len(executables) == 1:
        selected = executables[0]
    else:
        raise ValueError(f"cannot uniquely resolve source method: {function}{descriptor}")
    relative = source_path.resolve().relative_to(workspace.resolve()).as_posix()
    return MethodLocation(
        function=function,
        source_file=relative,
        start_line=selected.start_line,
        end_line=selected.end_line,
    )
