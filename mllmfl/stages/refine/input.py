from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Sequence

from mllmfl.domain.models import Ranking
from mllmfl.domain.schemas import validate_localization_input
from mllmfl.infrastructure.method_location import (
    java_executables,
    resolve_method_location,
    same_parameters,
    signature_parameter_types,
)

from .adapters import adapt_locator_result


def _canonical_function(function: str) -> str:
    normalized = function.strip().replace("$", ".")
    class_name, method = normalized.rsplit(".", 1)
    if method == class_name.rsplit(".", 1)[-1]:
        method = "<init>"
    return f"{class_name}.{method}"


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
                matches.append(executable.function)
    matches = list(dict.fromkeys(matches))
    if len(matches) == 1:
        resolved_function = matches[0]
    elif len(name_matches) == 1:
        # Locator signatures describe erased generic parameters (for example
        # ``T`` as ``java.lang.Object``), while source parsing retains the type
        # variable.  A single declaration is still unambiguous without making
        # overload resolution permissive.
        resolved_function = name_matches[0].function
    else:
        raise ValueError(
            f"cannot uniquely resolve locator method: {signature}"
        )
    canonical_signature = resolved_function + suffix
    return resolve_method_location(
        workspace, resolved_function, signature=canonical_signature
    ), canonical_signature


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
        item = _location_ranking(raw, workspace, index)
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
