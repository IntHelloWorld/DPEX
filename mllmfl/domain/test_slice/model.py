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
    exception_dependencies: Tuple[Tuple[int, int], ...] = ()

    def contains(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line

    @property
    def key(self) -> Tuple[int, int]:
        return self.start_byte, self.end_byte
