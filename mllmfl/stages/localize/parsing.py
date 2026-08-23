import base64
import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import requests

from mllmfl.domain.models import Ranking
from mllmfl.domain.interaction import IMAGE_ONLY_MODE, TEXT_INDEX_MODE
from mllmfl.infrastructure.method_location import resolve_method_location
from mllmfl.domain.failure import extract_error_stack
from mllmfl.domain.schemas import (
    validate_candidates,
    validate_defect_context,
    validate_localization,
    validate_uml_index,
)
from mllmfl.domain.test_slice import validate_slice_metadata
from mllmfl.infrastructure.io import append_jsonl, read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout


def parse_model_response(text: str) -> Dict[str, Any] | None:
    try:
        parsed = json.loads(text.strip())
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def validate_model_ranking_payload(
    value: Dict[str, Any],
    top_k: int,
    interaction_mode: str = TEXT_INDEX_MODE,
) -> List[Dict[str, str]]:
    if set(value) != {"ranked"} or not isinstance(value["ranked"], list):
        raise ValueError("model ranking JSON must contain only a ranked array")
    if not 1 <= len(value["ranked"]) <= top_k:
        raise ValueError(
            f"model ranking must contain between 1 and {top_k} entries"
        )
    result = []
    for index, item in enumerate(value["ranked"]):
        required_fields = (
            {"method_id", "method_signature", "reason"}
            if interaction_mode == IMAGE_ONLY_MODE
            else {"signature", "reason"}
        )
        if not isinstance(item, dict) or set(item) != required_fields:
            raise ValueError(f"invalid model ranking entry at index {index}")
        identifier_field = (
            "method_id" if interaction_mode == IMAGE_ONLY_MODE else "signature"
        )
        identifier = item[identifier_field]
        reason = item["reason"]
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError(f"empty model ranking {identifier_field} at index {index}")
        if (
            interaction_mode == IMAGE_ONLY_MODE
            and re.fullmatch(r"M\d{3,}", identifier.strip()) is None
        ):
            raise ValueError(f"invalid model ranking method_id at index {index}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"empty model ranking reason at index {index}")
        normalized = {identifier_field: identifier.strip(), "reason": reason.strip()}
        if interaction_mode == IMAGE_ONLY_MODE:
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


def gate_method_id_ranking(
    raw: Any,
    candidates: Sequence[str],
    method_catalog: Sequence[Dict[str, str]],
    viewed_method_ids: Sequence[str],
    top_k: int,
) -> Tuple[List[Ranking], List[str]]:
    """Resolve image-visible methods by ID+signature, ID, then unique signature."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    candidate_set = set(candidates)
    viewed_set = set(viewed_method_ids)
    id_lookup = {
        str(item.get("method_id") or ""): item
        for item in method_catalog
        if str(item.get("method_id") or "") in viewed_set
        and str(item.get("function") or "") in candidate_set
    }
    signature_lookup: Dict[str, List[Dict[str, str]]] = {}
    for catalog_item in id_lookup.values():
        method_signature = _catalog_method_signature(catalog_item)
        if method_signature:
            signature_lookup.setdefault(method_signature, []).append(catalog_item)
    result, dropped, seen = [], [], set()
    if not isinstance(raw, list):
        return result, dropped
    for item in raw:
        if not isinstance(item, dict):
            continue
        method_id = str(item.get("method_id") or "").strip()
        method_signature = str(item.get("method_signature") or "").strip()
        id_match = id_lookup.get(method_id)
        signature_matches = signature_lookup.get(method_signature, [])
        catalog_item = None
        if (
            id_match is not None
            and _catalog_method_signature(id_match) == method_signature
        ):
            catalog_item = id_match
        elif id_match is not None:
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


def gate_ranking(
    raw: Any,
    candidates: Sequence[str],
    viewed_signatures: Sequence[str],
    top_k: int,
) -> Tuple[List[Ranking], List[str]]:
    """Accept only exact signatures from viewed diagrams whose methods are candidates."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    candidate_set = set(candidates)
    lookup = {
        signature: signature.rsplit("(", 1)[0]
        for signature in viewed_signatures
        if signature.rsplit("(", 1)[0] in candidate_set
    }
    result, dropped, seen = [], [], set()
    if not isinstance(raw, list):
        return result, dropped
    for item in raw:
        if not isinstance(item, dict):
            continue
        supplied = str(item.get("signature") or "").strip()
        candidate = lookup.get(supplied)
        if candidate is None:
            dropped.append(supplied)
            continue
        if supplied in seen:
            continue
        seen.add(supplied)
        result.append(Ranking(
            function=candidate,
            signature=supplied,
            rank=len(result) + 1,
            reason=str(item.get("reason") or "")[:300],
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
