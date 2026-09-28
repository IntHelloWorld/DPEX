from dataclasses import replace
from pathlib import Path
import re
from typing import Any, Dict, Sequence

from dpex.domain.models import Ranking
from dpex.domain.schemas import validate_localization_input
from dpex.infrastructure.method_location import (
    java_executables,
    resolve_method_location,
    same_parameters,
    signature_parameter_types,
)

from .adapters import adapt_locator_result


def _canonical_function(function: str) -> str:
    normalized = function.strip()
    class_name, method = normalized.rsplit(".", 1)
    if method == class_name.rsplit(".", 1)[-1]:
        method = "<init>"
    return f"{class_name}.{method}"


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
    workspace: Path, function: str, signature: str, descriptor: str
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
    for path in workspace.rglob("*.java"):
        try:
            executables = java_executables(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except (OSError, ValueError):
            continue
        for executable in executables:
            normalized = executable.function.replace("$", ".")
            if not (
                normalized == canonical_function
                or normalized.endswith("." + canonical_function)
            ):
                continue
            name_matches.append(executable)
            if same_parameters(executable.parameter_types, expected):
                matches.append(executable)
    matches = list(dict.fromkeys(matches))
    if len(matches) == 1:
        selected = matches[0]
    elif len(name_matches) == 1:
        # Locator signatures describe erased generic parameters (for example
        # ``T`` as ``java.lang.Object``), while source parsing retains the type
        # variable.  A single declaration is still unambiguous without making
        # overload resolution permissive.
        selected = name_matches[0]
    else:
        erased_generic_matches = [
            executable
            for executable in name_matches
            if _matches_erased_generic_parameters(
                executable.parameter_types, expected
            )
        ]
        if len(erased_generic_matches) != 1:
            raise ValueError(
                f"cannot uniquely resolve locator method: {signature}"
            )
        selected = erased_generic_matches[0]
    resolved_function = selected.function
    canonical_signature = resolved_function + suffix
    source_signature = (
        resolved_function + "(" + ",".join(selected.parameter_types) + ")"
    )
    return (
        resolve_method_location(
            workspace, resolved_function, signature=source_signature
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
    if not function:
        raise ValueError(f"locator candidate at rank {rank} has no function")
    if not signature:
        raise ValueError(f"locator candidate at rank {rank} has no signature")
    location, canonical_signature = _resolve_locator_method(
        workspace, function, signature, descriptor
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
    for index, raw in enumerate(ranking, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"invalid locator ranking at index {index - 1}")
        items = []
        if raw.get("parameter_types_unknown") is True:
            source_file = str(raw.get("source_file") or "").strip()
            function = str(raw.get("function") or "").strip().replace("$", ".")
            matching_files = [
                path for path in workspace.rglob("*.java")
                if path.relative_to(workspace).as_posix().endswith(source_file)
            ]
            if len(matching_files) != 1:
                raise ValueError(
                    f"cannot uniquely resolve Agentless source file: {source_file}"
                )
            executables = java_executables(
                matching_files[0].read_text(encoding="utf-8", errors="replace")
            )
            matches = [
                executable for executable in executables
                if executable.function.replace("$", ".").endswith("." + function)
                or executable.function.replace("$", ".") == function
            ]
            if not matches:
                raise ValueError(
                    f"cannot resolve Agentless locator method: {function}"
                )
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
                    source_file=matching_files[0].relative_to(workspace).as_posix(),
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
