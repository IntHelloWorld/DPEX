from typing import Any, Dict


def validate_candidates(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("candidates artifact must be a JSON object")
    if value.get("schema") != "fault-candidates" or value.get("schema_version") != 1:
        raise ValueError("unsupported candidates schema")
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidates must be an array")
    seen = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("function"), str):
            raise ValueError(f"invalid candidate at index {index}")
        function = candidate["function"].strip()
        if not function:
            raise ValueError(f"empty candidate function at index {index}")
        if function in seen:
            raise ValueError(f"duplicate candidate function: {function}")
        seen.add(function)
    return value


def validate_localization(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("localization artifact must be a JSON object")
    if value.get("schema") != "fault-localization" or value.get("schema_version") != 1:
        raise ValueError("unsupported localization schema")
    ranking = value.get("ranking")
    if not isinstance(ranking, list):
        raise ValueError("ranking must be an array")
    seen = set()
    for index, item in enumerate(ranking):
        if not isinstance(item, dict) or not str(item.get("function") or "").strip():
            raise ValueError(f"invalid ranking at index {index}")
        function = str(item["function"])
        if function in seen:
            raise ValueError(f"duplicate ranking function: {function}")
        seen.add(function)
        try:
            rank = int(item.get("rank") or 0)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"invalid ranking rank at index {index}") from error
        if rank != index + 1:
            raise ValueError(f"non-contiguous rank at index {index}")
    return value
