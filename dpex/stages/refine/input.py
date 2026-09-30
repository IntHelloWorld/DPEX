from dataclasses import replace
from pathlib import Path
import re
from typing import Any, Dict, Sequence

from dpex.domain.models import Ranking
from dpex.domain.schemas import validate_localization_input
from dpex.infrastructure.method_location import (
    MethodLocation,
    java_executables,
    normalize_java_type,
    resolve_method_location,
    same_parameters,
    signature_parameter_types,
)
from dpex.infrastructure.java_source import find_java_file

from .adapters import adapt_locator_result


def _canonical_function(function: str) -> str:
    normalized = function.strip()
    class_name, method = normalized.rsplit(".", 1)
    if method in {
        class_name.rsplit(".", 1)[-1],
        class_name.rsplit("$", 1)[-1],
    }:
        method = "<init>"
    return f"{class_name}.{method}"


def _normalized_method_code(value: str) -> str:
    code = value.strip()
    if code.startswith("```"):
        lines = code.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        code = "\n".join(lines)
    return re.sub(r"\s+", "", code)


_GENERIC_TYPE_VARIABLE = re.compile(r"[A-Z][A-Z0-9_]*(?:\[\])*")


def _matches_erased_generic_type(declared: str, expected: str) -> bool:
    match = _GENERIC_TYPE_VARIABLE.fullmatch(declared)
    if match is None:
        return False
    declared_dimensions = declared.count("[]")
    expected_dimensions = expected.count("[]")
    if declared_dimensions != expected_dimensions:
        return False
    expected_base = expected.removesuffix("[]" * expected_dimensions)
    return expected_base not in {
        "boolean", "byte", "char", "short", "int", "long", "float", "double",
    }


def _matches_erased_generic_parameters(
    declared: Sequence[str], expected: Sequence[str]
) -> bool:
    if len(declared) != len(expected):
        return False
    return all(
        same_parameters((source_type,), (locator_type,))
        or (
            _matches_erased_generic_type(source_type, locator_type)
        )
        for source_type, locator_type in zip(declared, expected)
    )


def _resolve_locator_method(
    workspace: Path, function: str, signature: str, descriptor: str,
    method_code: str = "",
):
    canonical_function = _canonical_function(function)
    suffix = signature[signature.rfind("("):]
    canonical_signature = canonical_function + suffix
    try:
        location = resolve_method_location(
            workspace,
            canonical_function,
            descriptor=descriptor,
            signature=canonical_signature,
        )
        return location, canonical_signature
    except ValueError:
        if descriptor:
            raise
    expected = signature_parameter_types(canonical_signature)
    matches = []
    name_matches = []
    canonical_dotted = canonical_function.replace("$", ".")
    for path in workspace.rglob("*.java"):
        try:
            source_text = path.read_text(encoding="utf-8", errors="replace")
            executables = java_executables(source_text)
        except (OSError, ValueError):
            continue
        for executable in executables:
            normalized = executable.function.replace("$", ".")
            if not (
                normalized == canonical_dotted
                or normalized.endswith("." + canonical_dotted)
            ):
                continue
            entry = (path, source_text, executable)
            name_matches.append(entry)
            if same_parameters(executable.parameter_types, expected):
                matches.append(entry)
    matches = list(dict.fromkeys(matches))
    name_matches = list(dict.fromkeys(name_matches))
    normalized_code = _normalized_method_code(method_code)
    if normalized_code:
        code_matches = []
        for entry in name_matches:
            _, source_text, executable = entry
            lines = source_text.splitlines()
            source_code = "\n".join(
                lines[executable.start_line - 1 : executable.end_line]
            )
            if _normalized_method_code(source_code) == normalized_code:
                code_matches.append(entry)
        if len(code_matches) == 1:
            matches = code_matches
    if len(matches) == 1:
        selected_entry = matches[0]
    elif len(name_matches) == 1:
        # Locator signatures describe erased generic parameters (for example
        # ``T`` as ``java.lang.Object``), while source parsing retains the type
        # variable.  A single declaration is still unambiguous without making
        # overload resolution permissive.
        selected_entry = name_matches[0]
    else:
        erased_generic_matches = [
            entry
            for entry in name_matches
            for executable in (entry[2],)
            if _matches_erased_generic_parameters(
                executable.parameter_types, expected
            )
        ]
        if len(erased_generic_matches) != 1:
            raise ValueError(
                f"cannot uniquely resolve locator method: {signature}"
            )
        selected_entry = erased_generic_matches[0]
    selected_path, _, selected = selected_entry
    resolved_function = selected.function
    canonical_signature = resolved_function + suffix
    return (
        MethodLocation(
            function=resolved_function,
            source_file=selected_path.resolve().relative_to(
                workspace.resolve()
            ).as_posix(),
            start_line=selected.start_line,
            end_line=selected.end_line,
        ),
        canonical_signature,
    )


def _location_ranking(
    item: Dict[str, Any], workspace: Path, rank: int
) -> Ranking:
    function = str(item.get("function") or "").strip()
    signature = str(item.get("signature") or "").strip()
    source_file = str(item.get("source_file") or "").strip()
    start_line = item.get("start_line")
    end_line = item.get("end_line")
    descriptor = str(item.get("descriptor") or "")
    method_code = str(item.get("method_code") or "")
    if not function:
        raise ValueError(f"locator candidate at rank {rank} has no function")
    if not signature:
        raise ValueError(f"locator candidate at rank {rank} has no signature")
    location, canonical_signature = _resolve_locator_method(
        workspace, function, signature, descriptor, method_code
    )
    if source_file or start_line is not None or end_line is not None:
        if (
            source_file != location.source_file
            or start_line != location.start_line
            or end_line != location.end_line
        ):
            raise ValueError(
                f"locator candidate at rank {rank} source range does not match "
                "the buggy Java method"
            )
    return replace(
        Ranking(
            function=location.function,
            signature=canonical_signature,
            rank=rank,
            reason=str(item.get("reason") or ""),
            descriptor=descriptor,
        ),
        source_file=location.source_file,
        start_line=location.start_line,
        end_line=location.end_line,
    )


def _canonical_candidates(
    ranking: Sequence[Dict[str, Any]], workspace: Path
) -> list[Dict[str, Any]]:
    result = []
    seen_locations = set()
    source_range_file_cache = {}
    for index, raw in enumerate(ranking, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"invalid locator ranking at index {index - 1}")
        items = []
        if raw.get("source_range_identity") is True:
            raw_function = str(raw.get("function") or "").strip()
            raw_class, method_name = raw_function.rsplit(".", 1)
            class_parts = [
                part for part in raw_class.replace("$", ".").split(".")
                if not part.isdigit()
            ]
            normalized_class = ".".join(class_parts)
            normalized_method = (
                "<init>"
                if class_parts and method_name == class_parts[-1]
                else method_name
            )
            normalized_function = f"{normalized_class}.{normalized_method}"
            candidate_paths = []
            for class_part in reversed(class_parts):
                filename = class_part + ".java"
                paths = source_range_file_cache.get(filename)
                if paths is None:
                    paths = list(workspace.rglob(filename))
                    source_range_file_cache[filename] = paths
                for path in paths:
                    if path not in candidate_paths:
                        candidate_paths.append(path)
            source_range_executables = []
            for path in candidate_paths:
                try:
                    executables = java_executables(
                        path.read_text(encoding="utf-8", errors="replace")
                    )
                except (OSError, ValueError):
                    continue
                relative = path.relative_to(workspace).as_posix()
                source_range_executables.extend(
                    (relative, executable) for executable in executables
                )
            start_line = raw.get("start_line")
            end_line = raw.get("end_line")
            matches = [
                (source_file, executable)
                for source_file, executable in source_range_executables
                if executable.start_line == start_line
                and executable.end_line == end_line
                and normalized_function.endswith(
                    executable.function.replace("$", ".")
                )
            ]
            if len(matches) != 1:
                # Anonymous classes have no declared type name in the source
                # AST, so PingFL's numeric class segment disappears here. The
                # exact method name and source interval remain unambiguous.
                matches = [
                    (source_file, executable)
                    for source_file, executable in source_range_executables
                    if executable.function.rsplit(".", 1)[-1]
                    == normalized_method
                    and executable.start_line == start_line
                    and executable.end_line == end_line
                ]
            if len(matches) != 1:
                raise ValueError(
                    "cannot uniquely resolve PingFL method ID: "
                    f"{raw_function}#{start_line}-{end_line}"
                )
            source_file, executable = matches[0]
            items = [replace(
                Ranking(
                    function=executable.function,
                    signature=(
                        executable.function + "(" +
                        ",".join(executable.parameter_types) + ")"
                    ),
                    rank=index,
                    reason=str(raw.get("reason") or ""),
                ),
                source_file=source_file,
                start_line=executable.start_line,
                end_line=executable.end_line,
            )]
        elif raw.get("parameter_types_unknown") is True:
            source_file = str(raw.get("source_file") or "").strip()
            function = str(raw.get("function") or "").strip().replace("$", ".")
            source_class = source_file.removesuffix(".java").replace("/", ".")
            matching_file = find_java_file(workspace, source_class)
            if matching_file is None:
                suffix_matches = [
                    path for path in workspace.rglob(Path(source_file).name)
                    if path.relative_to(workspace).as_posix().endswith(source_file)
                ]
                suffix_matches.sort(key=lambda path: (
                    0 if path.relative_to(workspace).as_posix().startswith(
                        "src/main/java/"
                    ) else 1,
                    path.relative_to(workspace).as_posix(),
                ))
                matching_file = suffix_matches[0] if suffix_matches else None
            if matching_file is None:
                raise ValueError(
                    f"cannot resolve Agentless source file: {source_file}"
                )
            executables = java_executables(
                matching_file.read_text(encoding="utf-8", errors="replace")
            )
            matches = [
                executable for executable in executables
                if executable.function.replace("$", ".").endswith("." + function)
                or executable.function.replace("$", ".") == function
            ]
            expected_parameters = raw.get("parameter_types")
            if isinstance(expected_parameters, list):
                normalized_expected = tuple(
                    normalize_java_type(str(parameter))
                    for parameter in expected_parameters
                )
                matches = [
                    executable for executable in matches
                    if same_parameters(
                        executable.parameter_types, normalized_expected
                    )
                ]
            if not matches:
                # Agentless can label a field-like or hallucinated member as a
                # method alongside valid physical declarations.  Such entries
                # cannot participate in a method-level refinement ranking;
                # retain the resolvable candidates in their original order.
                continue
            for executable in matches:
                items.append(replace(
                    Ranking(
                        function=executable.function,
                        signature=(
                            executable.function + "(" +
                            ",".join(executable.parameter_types) + ")"
                        ),
                        rank=index,
                        reason=str(raw.get("reason") or ""),
                    ),
                    source_file=matching_file.relative_to(workspace).as_posix(),
                    start_line=executable.start_line,
                    end_line=executable.end_line,
                ))
        else:
            items = [_location_ranking(raw, workspace, index)]
        for item in items:
            location = (item.source_file, item.start_line, item.end_line)
            if location in seen_locations:
                continue
            seen_locations.add(location)
            candidate = item.to_dict()
            candidate["candidate_id"] = f"L{len(result) + 1:03d}"
            candidate["rank"] = len(result) + 1
            if isinstance(raw.get("score"), (int, float)) and not isinstance(
                raw.get("score"), bool
            ):
                candidate["score"] = raw["score"]
            result.append(candidate)
    if not result:
        raise ValueError("locator ranking has no resolvable candidates")
    return result


def load_localization_input(
    source: Path, project: str, bug: str, workspace: Path
) -> Dict[str, Any]:
    adapted = adapt_locator_result(source, project, bug)
    value = adapted.canonical_value
    if value is not None and (
        value.get("project") != project or str(value.get("bug")) != bug
    ):
        raise ValueError("locator result identity does not match selected bug")
    if value is not None and value.get("schema") == "fault-localization-input":
        validated = validate_localization_input(value)
        canonical = dict(validated)
        canonical["ranking"] = _canonical_candidates(
            validated["ranking"], workspace
        )
        validate_localization_input(canonical)
        canonical["source_path"] = str(adapted.path)
        return canonical
    canonical = {
        "schema": "fault-localization-input",
        "schema_version": 1,
        "project": project,
        "bug": bug,
        "locator": dict(adapted.locator),
        "ranking": _canonical_candidates(adapted.ranking, workspace),
    }
    if adapted.failing_tests is not None:
        canonical["failing_tests"] = list(adapted.failing_tests)
    validate_localization_input(canonical)
    canonical["source_path"] = str(adapted.path)
    return canonical
