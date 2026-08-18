from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Invocation:
    invocation_id: int
    parent_id: int
    class_name: str
    method: str
    descriptor: str
    thread_id: int
    thread_name: str
    enter_seq: int
    enter_ns: int
    origin_test_line: int = 0
    exit_seq: Optional[int] = None
    exit_ns: Optional[int] = None
    exit_type: Optional[str] = None
    duration_ns: Optional[int] = None
    exception_class: str = ""
    message: str = ""

    @property
    def function(self) -> str:
        return f"{self.class_name}.{self.method}"

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["class"] = value.pop("class_name")
        return value


@dataclass(frozen=True)
class Call:
    caller: str
    callee: str
    caller_class: str
    callee_class: str
    caller_method: str
    callee_method: str
    caller_descriptor: str
    callee_descriptor: str
    parent_invocation_id: int
    invocation_id: int
    parent_chain: List[int]
    thread_id: int
    enter_seq: int
    exit_seq: int
    exit_type: str
    origin_test_line: int = 0
    count: int = 1
    context: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Failure:
    exception_class: str = ""
    message: str = ""
    stack_trace: str = ""

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Candidate:
    function: str
    class_name: str
    method: str
    summary: str
    status: str
    source_file: str = ""
    signature: str = ""
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    called_methods: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["class"] = value.pop("class_name")
        return value


@dataclass(frozen=True)
class Ranking:
    function: str
    signature: str
    rank: int
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
