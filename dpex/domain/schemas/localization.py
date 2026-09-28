import re
from typing import Any, Dict

from .refinement import _aggregate_usage, _identity, _ranking


def validate_localization_result(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != "fault-localization-result" \
            or value.get("schema_version") not in {1, 2}:
        raise ValueError("unsupported fault-localization result schema")
    _identity(value, "fault-localization result")
    if (
        value.get("status") != "OK"
        or not isinstance(value.get("model"), str) or not value["model"].strip()
        or value.get("agent_variant") not in {
            "dynamic-graph", "dynamic-text", "bash-only", "no-values",
            "raw-trace", "no-assertion-folding",
        }
        or value.get("prompt_version") != {
            "dynamic-graph": "localization-method-lines-dynamic-v1",
            "dynamic-text": "localization-method-lines-dynamic-v1",
            "bash-only": "localization-method-lines-bash-only-v1",
            "no-values": "localization-method-lines-no-values-v1",
            "raw-trace": "localization-method-lines-raw-trace-v1",
            "no-assertion-folding":
                "localization-method-lines-no-assertion-folding-v1",
        }.get(value.get("agent_variant"))
        or not isinstance(value.get("top_k"), int) or isinstance(value["top_k"], bool)
        or value["top_k"] <= 0
        or re.fullmatch(r"[0-9a-f]{64}", str(value.get("input_fingerprint"))) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(value.get("configuration_fingerprint"))) is None
    ):
        raise ValueError("invalid fault-localization result configuration")
    tests = value.get("tests")
    if (
        not isinstance(tests, list) or not tests
        or value.get("test_count") != len(tests)
        or any(
            not isinstance(item, dict)
            or set(item) != {"test_id", "test", "failure_file", "failure_sha256"}
            or item.get("test_id") != f"T{index}"
            or not isinstance(item.get("test"), str) or "::" not in item["test"]
            or item.get("failure_file") != f"failing-tests/T{index}.txt"
            or re.fullmatch(r"[0-9a-f]{64}", str(item.get("failure_sha256"))) is None
            for index, item in enumerate(tests, 1)
        )
    ):
        raise ValueError("invalid fault-localization failing-test evidence")
    ranking = _ranking(value.get("ranking"), "localization", refined=True)
    if len(ranking) > value["top_k"] or any(
        item["candidate_id"] != f"N{index:03d}" or item["original_rank"] is not None
        for index, item in enumerate(ranking, 1)
    ):
        raise ValueError("invalid standalone localization ranking provenance")
    for field in ("tool_rounds", "diagram_view_count", "terminal_command_count"):
        if not isinstance(value.get(field), int) or isinstance(value[field], bool) or value[field] < 0:
            raise ValueError(f"invalid fault-localization {field}")
    for field in ("viewed_diagrams", "inspected_invocation_ids", "queried_methods"):
        if not isinstance(value.get(field), list):
            raise ValueError(f"invalid fault-localization {field}")
    if (
        value["terminal_command_count"] > value["tool_rounds"]
        or value["diagram_view_count"] != len(value["viewed_diagrams"])
        or value["agent_variant"] == "bash-only" and (
            value["diagram_view_count"] or value["viewed_diagrams"]
            or value["inspected_invocation_ids"] or value["queried_methods"]
        )
        or value["agent_variant"] in {
            "dynamic-text", "no-values", "raw-trace", "no-assertion-folding",
        } and value["diagram_view_count"]
    ):
        raise ValueError("invalid fault-localization tool audit")
    if (
        not isinstance(value.get("request_count"), int) or value["request_count"] <= 0
        or not isinstance(value.get("finalization_attempt_count"), int)
        or not 1 <= value["finalization_attempt_count"] <= value["request_count"]
        or not isinstance(value.get("final_length_retry_count"), int)
        or not 0 <= value["final_length_retry_count"] < value["finalization_attempt_count"]
        or not isinstance(value.get("usage"), dict)
        or value.get("final_finish_reason") is not None
        and (not isinstance(value["final_finish_reason"], str) or not value["final_finish_reason"].strip())
    ):
        raise ValueError("invalid fault-localization usage audit")
    _aggregate_usage(value["usage"])
    if not isinstance(value.get("failure_cache_hit"), bool):
        raise ValueError("invalid failing-test cache audit")
    if value["schema_version"] == 2:
        expected_policy = {
            "dynamic-graph": ("shown", "structured-invocations", "enabled"),
            "dynamic-text": ("shown", "structured-invocations", "enabled"),
            "bash-only": ("unavailable", "none", "unavailable"),
            "no-values": ("hidden", "structured-invocations", "enabled"),
            "raw-trace": ("shown", "continuous-events", "enabled"),
            "no-assertion-folding": ("shown", "structured-invocations", "disabled"),
        }[value["agent_variant"]]
        policy = value.get("execution_policy")
        if not isinstance(policy, dict) or set(policy) != {
            "value_visibility", "window_mode", "assertion_folding",
        } or tuple(policy[field] for field in (
            "value_visibility", "window_mode", "assertion_folding",
        )) != expected_policy:
            raise ValueError("invalid localization execution policy")
        sources = value.get("trace_sources")
        windows = value.get("inspection_windows")
        if not isinstance(sources, list) or not isinstance(windows, list):
            raise ValueError("invalid localization trace audit")
        if value["agent_variant"] == "bash-only" and (sources or windows):
            raise ValueError("bash-only localization contains trace audit")
        if value["agent_variant"] == "no-assertion-folding" and any(
            not isinstance(item, dict) or item.get("folded_call_count") != 0
            for item in sources
        ):
            raise ValueError("no-assertion-folding used a folded trace")
    return value
