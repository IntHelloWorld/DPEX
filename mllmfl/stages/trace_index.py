import hashlib
import json
from collections import OrderedDict
from typing import Any, Dict, Iterable

from mllmfl.infrastructure.sequence_diagram import readable_signature


def method_key(invocation: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(invocation["class"]),
        str(invocation["method"]),
        str(invocation.get("descriptor") or ""),
    )


def method_signature(key: tuple[str, str, str]) -> str:
    class_name, method, descriptor = key
    return f"{class_name}.{readable_signature(method, descriptor)}"


def build_method_catalog(
    executions: Iterable[Dict[str, Any]],
) -> tuple[list[Dict[str, str]], Dict[tuple[str, str, str], str], str]:
    recorded_keys = set()
    for execution in executions:
        call_ids = {
            int(call["invocation_id"]) for call in execution["calls"]
        }
        recorded_keys.update(
            method_key(invocation)
            for invocation in execution["invocations"]
            if int(invocation.get("invocation_id") or 0) in call_ids
        )
    keys = sorted(recorded_keys)
    method_ids = {key: f"M{index}" for index, key in enumerate(keys, 1)}
    catalog = [
        {
            "method_id": method_ids[key],
            "function": f"{key[0]}.{key[1]}",
            "signature": method_signature(key),
            "descriptor": key[2],
        }
        for key in keys
    ]
    material = json.dumps(catalog, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return catalog, method_ids, fingerprint


def build_trace_index(
    execution: Dict[str, Any],
    *,
    test_id: str,
    test: str,
    method_ids: Dict[tuple[str, str, str], str],
    catalog_fingerprint: str,
    fold_by_invocation: Dict[int, str] | None = None,
    default_execution: str = "execution.json",
) -> Dict[str, Any]:
    fold_by_invocation = fold_by_invocation or {}
    invocations = {
        int(item["invocation_id"]): item for item in execution["invocations"]
    }
    ordered_calls = sorted(
        execution["calls"],
        key=lambda item: (
            int(item.get("enter_seq") or 0), int(item["invocation_id"]),
        ),
    )
    methods: "OrderedDict[str, list[Dict[str, Any]]]" = OrderedDict()
    for call in ordered_calls:
        invocation_id = int(call["invocation_id"])
        invocation = invocations[invocation_id]
        key = method_key(invocation)
        method_id = method_ids[key]
        parent_id = int(call["parent_invocation_id"])
        parent = invocations.get(parent_id)
        caller_key = method_key(parent) if parent is not None else (
            str(call.get("caller_class") or "external"),
            str(call.get("caller_method") or "entry"),
            str(call.get("caller_descriptor") or ""),
        )
        occurrence = {
            "invocation_id": invocation_id,
            "successful_assertion_fold_id": fold_by_invocation.get(invocation_id),
            "caller_signature": method_signature(caller_key),
        }
        methods.setdefault(method_id, []).append(occurrence)
    return {
        "schema": "execution-trace-index",
        "schema_version": 2,
        "test_id": test_id,
        "test": test,
        "method_catalog_fingerprint": catalog_fingerprint,
        "execution": "execution.json",
        "default_execution": default_execution,
        "method_count": len(methods),
        "occurrence_count": len(ordered_calls),
        "default_occurrence_count": sum(
            1 for call in ordered_calls
            if int(call["invocation_id"]) not in fold_by_invocation
        ),
        "folded_occurrence_count": sum(
            1 for call in ordered_calls
            if int(call["invocation_id"]) in fold_by_invocation
        ),
        "methods": [
            {"method_id": method_id, "occurrences": occurrences}
            for method_id, occurrences in methods.items()
        ],
    }
