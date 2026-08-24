import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from mllmfl.domain.models import Ranking
from mllmfl.infrastructure.method_location import resolve_method_location


def parse_model_response(text: str) -> Dict[str, Any] | None:
    try:
        parsed = json.loads(text.strip())
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def validate_model_ranking_payload(
    value: Dict[str, Any],
    top_k: int,
) -> List[Dict[str, str]]:
    if set(value) != {"ranked"} or not isinstance(value["ranked"], list):
        raise ValueError("model ranking JSON must contain only a ranked array")
    if not 1 <= len(value["ranked"]) <= top_k:
        raise ValueError(
            f"model ranking must contain between 1 and {top_k} entries"
        )
    result = []
    for index, item in enumerate(value["ranked"]):
        required_fields = {"method_id", "method_signature", "reason"}
        if not isinstance(item, dict) or set(item) != required_fields:
            raise ValueError(f"invalid model ranking entry at index {index}")
        identifier_field = "method_id"
        identifier = item[identifier_field]
        reason = item["reason"]
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError(f"empty model ranking {identifier_field} at index {index}")
        if re.fullmatch(r"M\d{3,}", identifier.strip()) is None:
            raise ValueError(f"invalid model ranking method_id at index {index}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"empty model ranking reason at index {index}")
        normalized = {identifier_field: identifier.strip(), "reason": reason.strip()}
        method_signature = item["method_signature"]
        if not isinstance(method_signature, str) or not method_signature.strip():
            raise ValueError(
                f"empty model ranking method_signature at index {index}"
            )
        normalized["method_signature"] = method_signature.strip()
        result.append(normalized)
    return result


def _catalog_method_signature(item: Dict[str, str]) -> str:
    function = str(item.get("function") or "")
    signature = str(item.get("signature") or "")
    if not function or not signature.startswith(function):
        return ""
    return function.rsplit(".", 1)[-1] + signature[len(function):]


def _normalized_method_signature(signature: str) -> str:
    """Normalize insignificant spacing after parameter-separating commas."""
    return re.sub(r",\s*", ", ", signature.strip())


def gate_method_id_ranking(
    raw: Any,
    method_catalog: Sequence[Dict[str, str]],
    viewed_method_ids: Sequence[str],
    top_k: int,
) -> Tuple[List[Ranking], List[str]]:
    """Resolve visible methods by normalized ID+signature or unique signature."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    viewed_set = set(viewed_method_ids)
    id_lookup = {
        str(item.get("method_id") or ""): item
        for item in method_catalog
        if str(item.get("method_id") or "") in viewed_set
    }
    signature_lookup: Dict[str, List[Dict[str, str]]] = {}
    for catalog_item in id_lookup.values():
        method_signature = _catalog_method_signature(catalog_item)
        if method_signature:
            normalized_signature = _normalized_method_signature(method_signature)
            signature_lookup.setdefault(normalized_signature, []).append(catalog_item)
    result, dropped, seen = [], [], set()
    if not isinstance(raw, list):
        return result, dropped
    for item in raw:
        if not isinstance(item, dict):
            continue
        method_id = str(item.get("method_id") or "").strip()
        method_signature = str(item.get("method_signature") or "").strip()
        normalized_signature = _normalized_method_signature(method_signature)
        id_match = id_lookup.get(method_id)
        signature_matches = signature_lookup.get(normalized_signature, [])
        catalog_item = None
        if (
            id_match is not None
            and _normalized_method_signature(_catalog_method_signature(id_match))
            == normalized_signature
        ):
            catalog_item = id_match
        elif len(signature_matches) == 1:
            catalog_item = signature_matches[0]
        if catalog_item is None:
            dropped.append(method_id)
            continue
        resolved_method_id = str(catalog_item["method_id"])
        if resolved_method_id in seen:
            continue
        seen.add(resolved_method_id)
        result.append(Ranking(
            function=str(catalog_item["function"]),
            signature=str(catalog_item["signature"]),
            rank=len(result) + 1,
            reason=str(item.get("reason") or "")[:300],
            method_id=resolved_method_id,
            descriptor=str(catalog_item.get("descriptor") or ""),
        ))
        if len(result) >= top_k:
            break
    return result, dropped


def attach_source_locations(
    ranking: Sequence[Ranking], workspace: Path
) -> Tuple[List[Ranking], List[str]]:
    """Resolve ranked runtime methods to exact ranges in the buggy source tree."""
    result, dropped = [], []
    for item in ranking:
        try:
            location = resolve_method_location(
                workspace,
                item.function,
                descriptor=item.descriptor,
                signature=item.signature,
            )
        except (OSError, UnicodeError, ValueError):
            dropped.append(item.method_id or item.signature)
            continue
        result.append(replace(
            item,
            rank=len(result) + 1,
            source_file=location.source_file,
            start_line=location.start_line,
            end_line=location.end_line,
        ))
    return result, dropped
