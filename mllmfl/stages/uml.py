import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from mllmfl.domain.trace import WINDOW_SCHEMA, validate_trace
from mllmfl.infrastructure.io import read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.plantuml import render


def minimal_class_labels(classes: Iterable[str]) -> Dict[str, str]:
    values = list(dict.fromkeys(classes))
    split = {value: value.split(".") for value in values}
    result = {}
    for value in values:
        parts = split[value]
        label = parts[-1]
        for width in range(1, len(parts) + 1):
            candidate = ".".join(parts[-width:])
            if sum(".".join(other[-width:]) == candidate for other in split.values()) == 1:
                label = candidate
                break
        result[value] = label
    return result


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', "'").replace("\n", " ")


def _alias(class_name: str) -> str:
    return "p_" + hashlib.sha1(class_name.encode("utf-8")).hexdigest()[:10]


def readable_signature(method: str, descriptor: str) -> str:
    primitives = {"V": "void", "Z": "boolean", "B": "byte", "C": "char", "S": "short",
                  "I": "int", "J": "long", "F": "float", "D": "double"}
    if not descriptor.startswith("("):
        return method + "()"

    def parse(index: int) -> tuple[str, int]:
        arrays = 0
        while descriptor[index] == "[":
            arrays += 1
            index += 1
        if descriptor[index] == "L":
            end = descriptor.index(";", index)
            value = descriptor[index + 1 : end].replace("/", ".").rsplit(".", 1)[-1]
            index = end + 1
        else:
            value, index = primitives.get(descriptor[index], descriptor[index]), index + 1
        return value + "[]" * arrays, index

    args, index = [], 1
    try:
        while descriptor[index] != ")":
            value, index = parse(index)
            args.append(value)
    except (IndexError, ValueError):
        return method + "()"
    return f"{method}({', '.join(args)})"


def _root_test_invocation(window: Dict[str, Any]) -> Dict[str, Any] | None:
    test = window.get("test") or {}
    matches = [
        invocation for invocation in window.get("invocations") or []
        if invocation.get("class") == test.get("class")
        and invocation.get("method") == test.get("method")
    ]
    if not matches:
        return None
    return min(matches, key=lambda item: (int(item.get("parent_id") or 0) != 0,
                                           int(item.get("enter_seq") or 0)))


def make_puml(
    window: Dict[str, Any],
    project: str = "",
    bug: str = "",
    trigger: str = "",
) -> str:
    validate_trace(window, WINDOW_SCHEMA)
    calls = window["calls"]
    classes = []
    for call in calls:
        classes.extend([call.get("caller_class") or call["caller"].rsplit(".", 1)[0],
                        call.get("callee_class") or call["callee"].rsplit(".", 1)[0]])
    labels = minimal_class_labels(classes)
    test_class = str((window.get("test") or {}).get("class") or "")
    ordered = ([test_class] if test_class in labels else []) + [
        value for value in labels if value != test_class
    ]
    lines = ["@startuml", "hide footbox", "skinparam sequenceMessageAlign center",
             f"title {_escape(project)} bug {_escape(bug)} trigger {_escape(trigger)}"]
    for class_name in ordered:
        lines.append(f'participant "{_escape(labels[class_name])}" as {_alias(class_name)}')
    events = []
    root_test = _root_test_invocation(window)
    if root_test is not None:
        events.append((int(root_test.get("enter_seq") or 0), -1, -1, "root_enter", root_test))
        events.append((int(root_test.get("exit_seq") or 0), 2, -1, "root_exit", root_test))
    for order, call in enumerate(calls):
        events.append((int(call.get("enter_seq") or 0), 0, order, "enter", call))
        events.append(
            (
                int(call.get("exit_seq") or call.get("enter_seq") or 0),
                1,
                order,
                "exit",
                call,
            )
        )
    message_number = 0
    for _, _, _, event_type, call in sorted(events):
        if event_type == "root_enter":
            message_number += 1
            class_name = str(call["class"])
            signature = readable_signature(str(call["method"]), str(call.get("descriptor") or ""))
            label = f"M{message_number:03d} {signature}"
            lines.append(f"[-> {_alias(class_name)}: {_escape(label)}")
            lines.append(f"activate {_alias(class_name)}")
            continue
        if event_type == "root_exit":
            class_name = str(call["class"])
            result = "throws" if call.get("exit_type") == "THROW" else "return"
            lines.append(f"{_alias(class_name)} -->]: {result}")
            lines.append(f"deactivate {_alias(class_name)}")
            continue
        caller_class = call.get("caller_class") or call["caller"].rsplit(".", 1)[0]
        callee_class = call.get("callee_class") or call["callee"].rsplit(".", 1)[0]
        if event_type == "enter":
            message_number += 1
            method = call.get("callee_method") or call["callee"].rsplit(".", 1)[-1]
            signature = readable_signature(method, str(call.get("callee_descriptor") or ""))
            label = f"M{message_number:03d} {signature}"
            if int(call.get("count", 1)) > 1:
                label += f" ×{int(call['count'])}"
            lines.append(f"{_alias(caller_class)} -> {_alias(callee_class)}: {_escape(label)}")
            lines.append(f"activate {_alias(callee_class)}")
        else:
            result = "throws" if call.get("exit_type") == "THROW" else "return"
            lines.append(f"{_alias(callee_class)} --> {_alias(caller_class)}: {result}")
            lines.append(f"deactivate {_alias(callee_class)}")
    lines.extend(["@enduml", ""])
    return "\n".join(lines)


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
    plantuml_command: str,
    plantuml_jar: Path | None,
    timeout: int,
    force: bool = False,
    limit_size: int = 16384,
) -> List[Dict[str, object]]:
    rows = []
    for project, bug, number, directory in layout.discover_triggers(projects, bugs, trigger):
        puml_path = directory / "sequence.puml"
        png_path = directory / "sequence.png"
        if png_path.exists() and not force:
            rows.append({"project": project, "bug": bug, "trigger": number, "status": "SKIPPED"})
            continue
        try:
            window = read_json(directory / "window.json")
            puml = make_puml(window, project, bug, number)
            write_text(puml_path, puml)
            render(puml_path, plantuml_command, plantuml_jar, timeout, limit_size)
            write_json(directory / "uml.json", {
                "schema": "fault-focused-uml", "schema_version": 1,
                "source_schema": window["schema"], "call_count": len(window["calls"]),
                "puml": puml_path.name, "image": png_path.name,
            })
            rows.append({"project": project, "bug": bug, "trigger": number, "status": "OK"})
        except Exception as error:
            write_text(layout.stage_log_dir("uml", project, bug, number) / "error.log",
                       str(error) + "\n")
            rows.append({"project": project, "bug": bug, "trigger": number, "status": "ERROR"})
    write_csv(layout.logs / "uml.csv", rows, ["project", "bug", "trigger", "status"])
    return rows
