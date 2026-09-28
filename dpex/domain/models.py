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
    arguments: Optional[Dict[str, Any]] = None
    return_value: Optional[Dict[str, Any]] = None

    @property
    def function(self) -> str:
        return f"{self.class_name}.{self.method}"

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["class"] = value.pop("class_name")
        if value["arguments"] is None:
            value.pop("arguments")
        if value["return_value"] is None:
            value.pop("return_value")
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
    thread_id: int
    enter_seq: int
    exit_seq: int
    exit_type: str
    origin_test_line: int = 0
    parent_chain: Optional[List[int]] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        if value["parent_chain"] is None:
            value.pop("parent_chain")
        return value


@dataclass(frozen=True)
class Ranking:
    function: str
    signature: str
    rank: int
    reason: str = ""
    method_id: str = field(default="", repr=False)
    descriptor: str = field(default="", repr=False)
    source_file: str = ""
    start_line: Optional[int] = None
    end_line: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        value = {
            "function": self.function,
            "signature": self.signature,
            "rank": self.rank,
            "reason": self.reason,
        }
        if self.source_file:
            value.update({
                "source_file": self.source_file,
                "start_line": self.start_line,
                "end_line": self.end_line,
            })
        return value
