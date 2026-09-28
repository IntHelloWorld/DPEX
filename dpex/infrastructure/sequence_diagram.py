import hashlib
import re
from typing import Any, Dict, Iterable, List, Sequence

from dpex.domain.trace import EXECUTION_SCHEMA, validate_trace


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
# Persisted protocol identifier; independent of the Python package name.
RUNTIME_EVENT_TEXT_FORMAT = "mllmfl-runtime-events-v2"


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


def _display_enter_seq(item: Dict[str, Any]) -> int:
    value = item.get("display_enter_seq")
    if value is None:
        value = item.get("enter_seq")
    return int(value or 0)


def _display_exit_seq(item: Dict[str, Any]) -> int:
    value = item.get("display_exit_seq")
    if value is None:
        value = item.get("exit_seq")
    if value is None:
        value = item.get("display_enter_seq")
    if value is None:
        value = item.get("enter_seq")
    return int(value or 0)


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
            _display_enter_seq(item),
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


def _text_atom(value: Any) -> str:
    """Keep one event on one physical line without losing delimiter characters."""
    return str(value).replace("\\", "\\\\").replace("\r", "\\r").replace(
        "\n", "\\n"
    ).replace("|", "\\|")


def _text_arguments(invocation: Dict[str, Any]) -> str:
    arguments = invocation.get("arguments")
    if not isinstance(arguments, dict):
        return "[]"
    parts = [_text_atom(_display_value(item)) for item in arguments.get("items") or []]
    omitted = int(arguments.get("omitted_count") or 0)
    if omitted:
        parts.append(f"… (+{omitted} omitted)")
    return "[" + ", ".join(parts) + "]"


def make_execution_text(
    execution: Dict[str, Any],
    *,
    boundary_invocations: Sequence[Dict[str, Any]],
    graph_folds: Sequence[Dict[str, Any]],
    invocation_labels: Dict[int, str],
    show_values: bool = True,
) -> str:
    """Render the same bounded runtime events as the sequence-diagram image."""
    validate_trace(execution, EXECUTION_SCHEMA)
    boundaries = [dict(item) for item in boundary_invocations]
    folds = [dict(item) for item in graph_folds]
    if any(
        item.get("kind") not in {"OMITTED_CALLS", "REPEATED_SEQUENCE"}
        for item in folds
    ):
        raise ValueError("unsupported refinement graph fold")
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
    classes.extend(
        str(fold["anchor_class"])
        for fold in folds if fold.get("kind") == "OMITTED_CALLS"
    )
    labels = minimal_class_labels(classes)

    def participant(class_name: str) -> str:
        return _text_atom(labels[class_name])

    def invocation_label(item: Dict[str, Any]) -> str:
        invocation_id = int(item["invocation_id"])
        label = invocation_labels.get(invocation_id)
        if label is None:
            raise ValueError(
                f"missing invocation label for invocation {invocation_id}"
            )
        if re.fullmatch(r"T[1-9]\d*-C[1-9]\d*", label) is None:
            raise ValueError(f"invalid test-scoped invocation label: {label}")
        return _text_atom(label)

    def boundary_label(item: Dict[str, Any]) -> str:
        if item.get("synthetic") or item.get("layout_root"):
            return "TEST"
        return invocation_label(item)

    def result_fields(invocation: Dict[str, Any]) -> list[str]:
        if invocation.get("exit_type") == "THROW":
            return []
        value = invocation.get("return_value")
        if not isinstance(value, dict) or value.get("kind") == "void":
            return []
        if not show_values:
            return ["value=<hidden-by-no-values>"]
        return [f"value={_text_atom(_display_value(value))}"]

    def rendered_arguments(invocation: Dict[str, Any]) -> str:
        arguments = invocation.get("arguments")
        if not show_values:
            if not isinstance(arguments, dict):
                return "<not-captured>"
            count = int(arguments.get("count") or 0)
            if count == 0:
                return "[]"
            return f"[<hidden-by-no-values>; count={count}]"
        return _text_arguments(invocation)

    events = []
    for order, boundary in enumerate(boundaries):
        events.append(
            (_display_enter_seq(boundary), -1, order, "root_enter", boundary)
        )
        events.append(
            (_display_exit_seq(boundary), 2, order, "root_exit", boundary)
        )
    for order, call in enumerate(calls):
        events.append((_display_enter_seq(call), 0, order, "enter", call))
        events.append((_display_exit_seq(call), 1, order, "exit", call))
    for order, fold in enumerate(folds):
        if fold.get("kind") == "REPEATED_SEQUENCE":
            events.append(
                (_display_enter_seq(fold), -2, order, "repeat_start", fold)
            )
            events.append(
                (_display_exit_seq(fold), 2, order, "repeat_end", fold)
            )
        elif not fold.get("boundary_context"):
            events.append((_display_enter_seq(fold), 1, order, "fold", fold))

    lines = []
    for _, _, _, event_type, item in sorted(events):
        if event_type == "repeat_start":
            lines.append(f"LOOP_START | repetitions={int(item['repeat_count'])}")
            continue
        if event_type == "repeat_end":
            lines.append("LOOP_END")
            continue
        if event_type == "fold":
            anchor = participant(str(item["anchor_class"]))
            count = item.get("represented_call_count")
            rendered_count = str(int(count)) if count is not None else "unknown"
            lines.append(f"OMIT {anchor} -> {anchor} | calls={rendered_count}")
            continue
        if event_type == "root_enter":
            callee = participant(str(item["class"]))
            caller_class = str(item.get("caller_class") or "")
            caller = participant(caller_class) if caller_class else "external"
            omitted = int(item.get("omitted_context_enter_count") or 0)
            omitted_unknown = bool(item.get("omitted_context_enter"))
            if omitted or omitted_unknown:
                rendered_count = str(omitted) if omitted else "unknown"
                lines.append(f"OMIT {caller} -> {callee} | calls={rendered_count}")
                continue
            signature = readable_signature(
                str(item["method"]), str(item.get("descriptor") or "")
            )
            label = boundary_label(item)
            lines.append(
                f"CALL {caller} -> {callee} | {label} {_text_atom(signature)} | "
                f"args={rendered_arguments(item)}"
            )
            continue
        if event_type == "root_exit":
            caller_class = str(item.get("caller_class") or "")
            caller = participant(caller_class) if caller_class else "external"
            callee = participant(str(item["class"]))
            omitted = int(item.get("omitted_context_exit_count") or 0)
            omitted_unknown = bool(item.get("omitted_context_exit"))
            if omitted or omitted_unknown:
                rendered_count = str(omitted) if omitted else "unknown"
                lines.append(f"OMIT {callee} -> {caller} | calls={rendered_count}")
                continue
            event = "THROW" if item.get("exit_type") == "THROW" else "RETURN"
            key = "throw_of" if event == "THROW" else "return_of"
            label = boundary_label(item)
            fields = [f"{key}={label}", *result_fields(item)]
            if event == "THROW":
                fields.extend([
                    f"exception={_text_atom(item.get('exception_class') or '')}",
                    f"message={_text_atom(item.get('message') or '')}",
                ])
            lines.append(f"{event} {callee} -> {caller} | " + " | ".join(fields))
            continue

        caller_class = str(
            item.get("caller_class") or item["caller"].rsplit(".", 1)[0]
        )
        callee_class = str(
            item.get("callee_class") or item["callee"].rsplit(".", 1)[0]
        )
        caller = participant(caller_class)
        callee = participant(callee_class)
        invocation = invocation_by_id.get(int(item["invocation_id"])) or item
        label = invocation_label(item)
        if event_type == "enter":
            method = item.get("callee_method") or item["callee"].rsplit(".", 1)[-1]
            signature = readable_signature(
                str(method), str(item.get("callee_descriptor") or "")
            )
            lines.append(
                f"CALL {caller} -> {callee} | {label} {_text_atom(signature)} | "
                f"args={rendered_arguments(invocation)}"
            )
        else:
            event = "THROW" if invocation.get("exit_type") == "THROW" else "RETURN"
            key = "throw_of" if event == "THROW" else "return_of"
            fields = [f"{key}={label}", *result_fields(invocation)]
            if event == "THROW":
                fields.extend([
                    f"exception={_text_atom(invocation.get('exception_class') or '')}",
                    f"message={_text_atom(invocation.get('message') or '')}",
                ])
            lines.append(f"{event} {callee} -> {caller} | " + " | ".join(fields))
    return "\n".join(
        f"{index} {line}" for index, line in enumerate(lines, 1)
    )


def make_continuous_execution_text(
    events: Sequence[Dict[str, Any]], *, test_id: str, show_values: bool,
    omitted_before: int, omitted_after: int,
) -> str:
    """Render an event-order slice without adding out-of-window partner events."""
    if omitted_before < 0 or omitted_after < 0:
        raise ValueError("omitted event counts must be non-negative")

    def participant(invocation: Dict[str, Any]) -> str:
        return _text_atom(str(invocation["class"]).rsplit(".", 1)[-1])

    def arguments(invocation: Dict[str, Any]) -> str:
        value = invocation.get("arguments")
        if show_values:
            return _text_arguments(invocation)
        if not isinstance(value, dict):
            return "<not-captured>"
        count = int(value.get("count") or 0)
        return "[]" if count == 0 else f"[<hidden-by-no-values>; count={count}]"

    lines = []
    if omitted_before:
        lines.append(f"TRUNCATED_START | omitted_events={omitted_before}")
    for item in events:
        invocation = item["invocation"]
        caller = item["caller"]
        label = f"{test_id}-C{int(invocation['invocation_id'])}"
        event_type = str(item["type"])
        source = participant(caller)
        target = participant(invocation)
        if event_type == "CALL":
            signature = readable_signature(
                str(invocation["method"]), str(invocation.get("descriptor") or "")
            )
            lines.append(
                f"CALL {source} -> {target} | {label} {_text_atom(signature)} | "
                f"args={arguments(invocation)}"
            )
        elif event_type == "THROW":
            exception_class = _text_atom(str(invocation.get("exception_class") or ""))
            message = _text_atom(str(invocation.get("message") or ""))
            lines.append(
                f"THROW {target} -> {source} | throw_of={label} | "
                f"exception={exception_class} | message={message}"
            )
        else:
            fields = [f"return_of={label}"]
            value = invocation.get("return_value")
            if isinstance(value, dict) and value.get("kind") != "void":
                fields.append(
                    "value=<hidden-by-no-values>" if not show_values
                    else f"value={_text_atom(_display_value(value))}"
                )
            lines.append(f"RETURN {target} -> {source} | " + " | ".join(fields))
    if omitted_after:
        lines.append(f"TRUNCATED_END | omitted_events={omitted_after}")
    return "\n".join(f"{index} {line}" for index, line in enumerate(lines, 1))


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
    if any(
        item.get("kind") not in {"OMITTED_CALLS", "REPEATED_SEQUENCE"}
        for item in folds
    ):
        raise ValueError("unsupported refinement graph fold")
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
    classes.extend(
        str(fold["anchor_class"])
        for fold in folds if fold.get("kind") == "OMITTED_CALLS"
    )
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
        events.append(
            (_display_enter_seq(boundary), -1, order, "root_enter", boundary)
        )
        events.append(
            (_display_exit_seq(boundary), 2, order, "root_exit", boundary)
        )
    for order, call in enumerate(calls):
        events.append((_display_enter_seq(call), 0, order, "enter", call))
        events.append((_display_exit_seq(call), 1, order, "exit", call))
    for order, fold in enumerate(folds):
        if fold.get("kind") == "REPEATED_SEQUENCE":
            events.append(
                (_display_enter_seq(fold), -2, order, "repeat_start", fold)
            )
            events.append(
                (_display_exit_seq(fold), 2, order, "repeat_end", fold)
            )
        elif not fold.get("boundary_context"):
            events.append((_display_enter_seq(fold), 1, order, "fold", fold))

    for _, _, _, event_type, item in sorted(events):
        if event_type == "fold":
            anchor = _alias(str(item["anchor_class"]))
            count = item.get("represented_call_count")
            label = (
                f"... omit {int(count)} calls ..."
                if count is not None else "... omitted calls ..."
            )
            lines.append(
                f"{anchor} -> {anchor}: {label}"
            )
            continue
        if event_type == "repeat_start":
            lines.append(f"loop repeated sequence ×{int(item['repeat_count'])}")
            continue
        if event_type == "repeat_end":
            lines.append("end")
            continue
        if event_type == "root_enter":
            class_name = str(item["class"])
            omitted = int(item.get("omitted_context_enter_count") or 0)
            omitted_unknown = bool(item.get("omitted_context_enter"))
            if omitted or omitted_unknown:
                label = (
                    f"... omit {omitted} calls ..."
                    if omitted else "... omitted calls ..."
                )
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
            omitted_unknown = bool(item.get("omitted_context_exit"))
            if omitted:
                result = f"... omit {omitted} calls ..."
            elif omitted_unknown:
                result = "... omitted calls ..."
            else:
                result = _return_label(item)
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
        if event_type == "enter":
            method = item.get("callee_method") or item["callee"].rsplit(".", 1)[-1]
            signature = readable_signature(
                str(method), str(item.get("callee_descriptor") or "")
            )
            label = call_label(item, signature)
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
    lines.extend(["@enduml", ""])
    return "\n".join(lines)
