import hashlib
import re
from typing import Any, Dict, Iterable, List, Sequence

from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace


def minimal_class_labels(classes: Iterable[str]) -> Dict[str, str]:
    values = list(dict.fromkeys(classes))
    split = {value: value.split(".") for value in values}
    result = {}
    for value in values:
        parts = split[value]
        label = parts[-1]
        for width in range(1, len(parts) + 1):
            candidate = ".".join(parts[-width:])
            if sum(
                ".".join(other[-width:]) == candidate
                for other in split.values()
            ) == 1:
                label = candidate
                break
        result[value] = label
    return result


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', "'").replace("\n", " ")


def _escape_message(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\r", "\\r").replace(
        "\n", "\\n"
    )


_QUALIFIED_OBJECT_PLACEHOLDER = re.compile(
    r"<([A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+)>"
)
_FOCUS_ARROW_COLOR = "#C62828"
_FOCUS_TEXT_COLOR = "#B71C1C"
_FOCUS_ACTIVATION_COLOR = "#FFCDD2"


def _display_value(value: Dict[str, Any]) -> str:
    text = str(value.get("text") or "")
    kind = str(value.get("kind") or "")
    runtime_type = str(value.get("runtime_type") or "")
    if kind == "object" and runtime_type:
        return f"<{runtime_type.rsplit('.', 1)[-1]}>"
    if kind == "enum" and runtime_type:
        constant = text.rsplit(".", 1)[-1]
        return f"{runtime_type.rsplit('.', 1)[-1]}.{constant}"
    if kind in {"string", "char"}:
        return text
    return _QUALIFIED_OBJECT_PLACEHOLDER.sub(
        lambda match: f"<{match.group(1).rsplit('.', 1)[-1]}>", text
    )


def _label_with_arguments(label: str, invocation: Dict[str, Any]) -> str:
    arguments = invocation.get("arguments")
    if not isinstance(arguments, dict) or int(arguments.get("count") or 0) == 0:
        return label
    parts = [_display_value(item) for item in arguments.get("items") or []]
    omitted = int(arguments.get("omitted_count") or 0)
    if omitted:
        parts.append(f"… (+{omitted} omitted)")
    lines: List[str] = [label]
    current = "args=["
    for item in parts:
        separator = "" if current.endswith("[") else ", "
        if not current.endswith("[") and len(current + separator + item) > 120:
            lines.append(current + ",")
            current = "  " + item
        else:
            current += separator + item
    lines.append(current + "]")
    return "\n".join(lines)


def _return_label(invocation: Dict[str, Any]) -> str:
    if invocation.get("exit_type") == "THROW":
        return "throws"
    value = invocation.get("return_value")
    if not isinstance(value, dict) or value.get("kind") == "void":
        return "return"
    return f"return value={_display_value(value)}"


def _alias(class_name: str) -> str:
    return "p_" + hashlib.sha1(class_name.encode("utf-8")).hexdigest()[:10]


def _diagram_filename(diagram_id: str, extension: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", str(diagram_id).strip()).strip(
        "._-"
    )
    return f"{safe or 'diagram'}.{extension}"


def readable_signature(method: str, descriptor: str) -> str:
    primitives = {
        "V": "void",
        "Z": "boolean",
        "B": "byte",
        "C": "char",
        "S": "short",
        "I": "int",
        "J": "long",
        "F": "float",
        "D": "double",
    }
    if not descriptor.startswith("("):
        return method + "()"

    def parse(index: int) -> tuple[str, int]:
        arrays = 0
        while descriptor[index] == "[":
            arrays += 1
            index += 1
        if descriptor[index] == "L":
            end = descriptor.index(";", index)
            value = descriptor[index + 1:end].replace("/", ".").rsplit(".", 1)[-1]
            index = end + 1
        else:
            value = primitives.get(descriptor[index], descriptor[index])
            index += 1
        return value + "[]" * arrays, index

    arguments = []
    index = 1
    try:
        while descriptor[index] != ")":
            value, index = parse(index)
            arguments.append(value)
    except (IndexError, ValueError):
        return method + "()"
    return f"{method}({', '.join(arguments)})"


def method_signatures(execution: Dict[str, Any]) -> List[str]:
    result = []
    for call in sorted(
        execution["calls"],
        key=lambda item: (
            int(item.get("enter_seq") or 0),
            int(item["invocation_id"]),
        ),
    ):
        class_name = str(
            call.get("callee_class") or str(call["callee"]).rsplit(".", 1)[0]
        )
        method = str(
            call.get("callee_method") or str(call["callee"]).rsplit(".", 1)[-1]
        )
        signature = (
            f"{class_name}."
            f"{readable_signature(method, str(call.get('callee_descriptor') or ''))}"
        )
        if signature not in result:
            result.append(signature)
    return result


def make_puml(
    execution: Dict[str, Any],
    *,
    title: str,
    boundary_invocations: Sequence[Dict[str, Any]],
    graph_folds: Sequence[Dict[str, Any]],
    invocation_labels: Dict[int, str],
    highlighted_invocation_ids: Iterable[int],
) -> str:
    """Render one self-contained refinement execution graph."""
    validate_trace(execution, EXECUTION_SCHEMA)
    boundaries = [dict(item) for item in boundary_invocations]
    folds = [dict(item) for item in graph_folds]
    if any(item.get("kind") != "OMITTED_CALLS" for item in folds):
        raise ValueError("refinement graphs support only omitted-call folds")
    highlighted_ids = {int(value) for value in highlighted_invocation_ids}
    calls = [dict(call) for call in execution["calls"]]
    invocation_by_id = {
        int(invocation["invocation_id"]): invocation
        for invocation in execution.get("invocations") or []
    }

    classes = []
    for call in calls:
        classes.append(call.get("caller_class") or call["caller"].rsplit(".", 1)[0])
        classes.append(call.get("callee_class") or call["callee"].rsplit(".", 1)[0])
    for boundary in boundaries:
        classes.append(str(boundary["class"]))
        if boundary.get("caller_class"):
            classes.append(str(boundary["caller_class"]))
    classes.extend(str(fold["anchor_class"]) for fold in folds)
    labels = minimal_class_labels(classes)

    lines = [
        "@startuml",
        "hide footbox",
        "skinparam sequenceMessageAlign center",
        f"title {_escape(title)}",
    ]
    for class_name in labels:
        lines.append(
            f'participant "{_escape(labels[class_name])}" as {_alias(class_name)}'
        )

    def call_label(item: Dict[str, Any], signature: str) -> str:
        invocation_id = int(item["invocation_id"])
        label = invocation_labels.get(invocation_id)
        if label is None:
            raise ValueError(
                f"missing invocation label for invocation {invocation_id}"
            )
        return _label_with_arguments(
            f"{label} {signature}",
            invocation_by_id.get(invocation_id) or item,
        )

    def rendered_label(item: Dict[str, Any], label: str) -> str:
        rendered = _escape_message(label)
        if int(item["invocation_id"]) not in highlighted_ids:
            return rendered
        return "\\n".join(
            f"<color:{_FOCUS_TEXT_COLOR}><b>{line}</b></color>"
            for line in rendered.split("\\n")
        )

    def arrow(item: Dict[str, Any]) -> str:
        return (
            f"-[{_FOCUS_ARROW_COLOR}]>"
            if int(item["invocation_id"]) in highlighted_ids
            else "->"
        )

    def activation(item: Dict[str, Any], class_name: str) -> str:
        color = (
            f" {_FOCUS_ACTIVATION_COLOR}"
            if int(item["invocation_id"]) in highlighted_ids
            else ""
        )
        return f"activate {_alias(class_name)}{color}"

    events = []
    for order, boundary in enumerate(boundaries):
        events.append((int(boundary.get("enter_seq") or 0), -1, order, "root_enter", boundary))
        events.append((int(boundary.get("exit_seq") or 0), 2, order, "root_exit", boundary))
    for order, call in enumerate(calls):
        events.append((int(call.get("enter_seq") or 0), 0, order, "enter", call))
        events.append((int(call.get("exit_seq") or call.get("enter_seq") or 0), 1, order, "exit", call))
    for order, fold in enumerate(
        item for item in folds if not item.get("boundary_context")
    ):
        events.append((int(fold.get("enter_seq") or 0), 1, order, "fold", fold))

    for _, _, _, event_type, item in sorted(events):
        if event_type == "fold":
            anchor = _alias(str(item["anchor_class"]))
            lines.append(
                f"{anchor} -> {anchor}: ... omit "
                f"{int(item['represented_call_count'])} calls ..."
            )
            continue
        if event_type == "root_enter":
            class_name = str(item["class"])
            omitted = int(item.get("omitted_context_enter_count") or 0)
            if omitted:
                label = f"... omit {omitted} calls ..."
            else:
                signature = readable_signature(
                    str(item["method"]), str(item.get("descriptor") or "")
                )
                label = (
                    _label_with_arguments(f"TEST {signature}", item)
                    if item.get("layout_root")
                    else call_label(item, signature)
                )
            caller = str(item.get("caller_class") or "")
            rendered = rendered_label(item, label)
            if caller:
                lines.append(
                    f"{_alias(caller)} {arrow(item)} {_alias(class_name)}: {rendered}"
                )
            else:
                lines.append(f"[-> {_alias(class_name)}: {rendered}")
            lines.append(activation(item, class_name))
            continue
        if event_type == "root_exit":
            class_name = str(item["class"])
            omitted = int(item.get("omitted_context_exit_count") or 0)
            result = (
                f"... omit {omitted} calls ..."
                if omitted
                else _return_label(item)
            )
            caller = str(item.get("caller_class") or "")
            if caller:
                lines.append(
                    f"{_alias(class_name)} --> {_alias(caller)}: "
                    f"{_escape_message(result)}"
                )
            else:
                lines.append(
                    f"{_alias(class_name)} -->]: {_escape_message(result)}"
                )
            lines.append(f"deactivate {_alias(class_name)}")
            continue

        caller = str(
            item.get("caller_class") or item["caller"].rsplit(".", 1)[0]
        )
        callee = str(
            item.get("callee_class") or item["callee"].rsplit(".", 1)[0]
        )
        repeat = item.get("repeat_sequence") or {}
        if event_type == "enter":
            if int(repeat.get("position") or 0) == 1:
                lines.append(
                    f"loop repeated sequence ×{int(repeat['repeat_count'])}"
                )
            method = item.get("callee_method") or item["callee"].rsplit(".", 1)[-1]
            signature = readable_signature(
                str(method), str(item.get("callee_descriptor") or "")
            )
            label = call_label(item, signature)
            if int(item.get("count", 1)) > 1 and not repeat:
                label += f" ×{int(item['count'])}"
            lines.append(
                f"{_alias(caller)} {arrow(item)} {_alias(callee)}: "
                f"{rendered_label(item, label)}"
            )
            lines.append(activation(item, callee))
        else:
            lines.append(
                f"{_alias(callee)} --> {_alias(caller)}: "
                f"{_escape_message(_return_label(invocation_by_id.get(int(item['invocation_id'])) or item))}"
            )
            lines.append(f"deactivate {_alias(callee)}")
            if (
                repeat
                and int(repeat.get("position") or 0)
                == int(repeat.get("pattern_length") or 0)
            ):
                lines.append("end")
    lines.extend(["@enduml", ""])
    return "\n".join(lines)
