import copy
import hashlib
import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Sequence

from dpex.domain.schemas import (
    validate_refinement,
    validate_trace_suite,
)
from dpex.infrastructure.io import read_json, write_csv, write_json, write_text
from dpex.infrastructure.layout import RunLayout
from dpex.infrastructure.checkouts import temporary_checkout
from dpex.infrastructure.defects4j import defects4j_environment, trigger_tests
from .agent import finalization_limits, run_agent
from .client import invalid_final_response_retries
from .context import (
    AGENT_VARIANT_BASH_ONLY,
    PROMPT_VERSIONS,
    build_prompt,
    agent_variant_policy,
    refinement_agent_variant,
    runtime_method_ids,
    selected_trace_tests,
)
from .graphs import MethodExecutionGraphs
from .input import load_localization_input


VIEWPORT_DEFAULTS = {
    "max_upstream_calls": 6,
    "max_downstream_calls": 6,
    "max_internal_calls": 10,
}
# Preserve result identity across the package rename.
NO_TRACE_SUITE_FINGERPRINT = hashlib.sha256(
    b"mllmfl:no-trace-suite:v1"
).hexdigest()


def refinement_viewport_configuration(
    config: Dict[str, Any],
    *,
    max_upstream_calls: int | None = None,
    max_downstream_calls: int | None = None,
    max_internal_calls: int | None = None,
) -> tuple[int, int, int]:
    """Resolve JSON viewport budgets, with explicit CLI values as overrides."""
    uml = config.get("uml") or {}
    if not isinstance(uml, dict):
        raise ValueError("uml configuration must be an object")
    for field in ("raw_trace_events_before", "raw_trace_events_after"):
        value = uml.get(field, 20)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"uml.{field} must be a non-negative integer")
    overrides = {
        "max_upstream_calls": max_upstream_calls,
        "max_downstream_calls": max_downstream_calls,
        "max_internal_calls": max_internal_calls,
    }
    resolved = []
    for field, default in VIEWPORT_DEFAULTS.items():
        value = overrides[field]
        if value is None:
            value = uml.get(field, default)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"uml.{field} must be a positive integer")
        resolved.append(value)
    return tuple(resolved)


def _locator_bug_items(
    locator_results: Path, projects: Sequence[str]
) -> set[tuple[str, str]]:
    """Discover identities represented by the supported locator layouts."""
    selected_projects = set(projects)
    result: set[tuple[str, str]] = set()
    if locator_results.is_file():
        value = read_json(locator_results)
        if isinstance(value, dict):
            project = str(value.get("project") or "")
            bug = str(value.get("bug") or "")
            if project in selected_projects and bug.isdigit():
                result.add((project, bug))
        match = re.fullmatch(r"XFL-([^_]+)_([1-9]\d*)\.json", locator_results.name)
        if match is not None and match.group(1) in selected_projects:
            result.add((match.group(1), match.group(2)))
        return result

    for project in selected_projects:
        for parent in (
            locator_results / project,
            locator_results / "artifacts" / project,
        ):
            for path in parent.glob("bug_*/locator_result.json"):
                bug = path.parent.name.removeprefix("bug_")
                if bug.isdigit():
                    result.add((project, bug))
        for path in locator_results.glob(f"{project}-*.json"):
            bug = path.stem.removeprefix(f"{project}-")
            if bug.isdigit():
                result.add((project, bug))
        for parent in (locator_results, locator_results / "predictions"):
            for path in parent.glob(f"XFL-{project}_*.json"):
                bug = path.stem.removeprefix(f"XFL-{project}_")
                if bug.isdigit():
                    result.add((project, bug))
    return result


def _bug_items(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    locator_results: Path | None = None,
) -> list[tuple[str, str]]:
    if bugs is not None:
        return sorted(
            {
                (project, bug)
                for project in set(projects)
                for bug in bugs
                if bug.isdigit()
            },
            key=lambda item: (item[0], int(item[1])),
        )
    result = set()
    for project in sorted(set(projects)):
        project_dir = layout.artifacts / project
        for path in project_dir.glob("bug_*/trace_suite.json"):
            bug = path.parent.name.removeprefix("bug_")
            if bug.isdigit() and (bugs is None or bug in bugs):
                result.add((project, bug))
    if locator_results is not None:
        result.update(_locator_bug_items(locator_results, projects))
    return sorted(result, key=lambda item: (item[0], int(item[1])))


def _bash_only_configuration(config: Dict[str, Any]) -> Dict[str, Any]:
    effective = copy.deepcopy(config)
    if "dpex" in effective:
        cfg = effective.get("dpex")
        if not isinstance(cfg, dict):
            raise ValueError("dpex configuration must be an object")
    else:
        cfg = effective
    cfg["agent_variant"] = AGENT_VARIANT_BASH_ONLY
    return effective


def _tests_without_trace(
    localization_input: Dict[str, Any], workspace: Path
) -> list[Dict[str, Any]]:
    tests = localization_input.get("failing_tests")
    if tests is None:
        tests = trigger_tests(workspace, defects4j_environment(None, None))
    tests = list(dict.fromkeys(str(test) for test in tests))
    if not tests:
        raise ValueError(
            "trace suite is unavailable and no failing tests can be selected"
        )
    return [
        {"test_id": f"T{index}", "test": test}
        for index, test in enumerate(tests, 1)
    ]


def _refine_bug(
    layout: RunLayout,
    project: str,
    bug: str,
    locator_results: Path,
    config: Dict[str, Any],
    timeout: int,
    requested_top_k: int | None,
    dry_run: bool,
    force: bool,
    max_upstream_calls: int,
    max_downstream_calls: int,
    max_internal_calls: int,
    retain_debug_artifacts: bool,
    trace_layout: RunLayout | None = None,
) -> Dict[str, object]:
    bug_dir = layout.artifacts / project / f"bug_{bug}"
    bug_dir.mkdir(parents=True, exist_ok=True)
    trace_bug_dir = (trace_layout or layout).artifacts / project / f"bug_{bug}"
    result_path = bug_dir / "refinement.json"
    error_log = layout.stage_log_dir("refine", project, bug) / "error.log"
    error_log.unlink(missing_ok=True)
    graphs = None
    try:
        with temporary_checkout(layout, project, bug, timeout) as workspace:
            localization_input = load_localization_input(
                locator_results, project, bug, workspace
            )
            suite_path = trace_bug_dir / "trace_suite.json"
            suite = (
                validate_trace_suite(read_json(suite_path))
                if suite_path.is_file() else None
            )
            effective_config = (
                config if suite is not None else _bash_only_configuration(config)
            )
            candidates = list(localization_input["ranking"])
            cfg = effective_config.get("dpex", effective_config)
            selected_top_k = (
                requested_top_k if requested_top_k is not None else int(
                    cfg.get("top_k", 5)
                )
            )
            if selected_top_k <= 0:
                raise ValueError("top_k must be positive")
            fingerprint_input = {
                key: value for key, value in localization_input.items()
                if key != "source_path"
            }
            if suite is not None:
                fingerprint_input["trace_sources"] = [
                    {
                        "test_id": item["test_id"],
                        "trace_fingerprint": item["trace_fingerprint"],
                    }
                    for item in suite["tests"]
                ]
            input_fingerprint = hashlib.sha256(json.dumps(
                fingerprint_input, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            configuration_fingerprint = hashlib.sha256(json.dumps(
                {
                    "config": effective_config,
                    "refinement_viewport": {
                        "max_upstream_calls": max_upstream_calls,
                        "max_downstream_calls": max_downstream_calls,
                        "max_internal_calls": max_internal_calls,
                    },
                },
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            suite_fingerprint = (
                hashlib.sha256(json.dumps(
                    suite, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")).hexdigest()
                if suite is not None else NO_TRACE_SUITE_FINGERPRINT
            )
            agent_variant = refinement_agent_variant(effective_config)
            if result_path.is_file() and not force:
                existing = validate_refinement(read_json(result_path))
                if (
                    existing["project"] != project
                    or existing["bug"] != bug
                    or existing["input_fingerprint"] != input_fingerprint
                    or existing["configuration_fingerprint"]
                    != configuration_fingerprint
                    or existing["suite_fingerprint"] != suite_fingerprint
                    or existing["top_k"] != selected_top_k
                    or existing.get("agent_variant") != agent_variant
                    or existing.get("prompt_version")
                    != PROMPT_VERSIONS[agent_variant]
                ):
                    raise ValueError(
                        "existing refinement does not match input, config, trace suite, or "
                        "current agent contract; rerun with --force"
                    )
                return {
                    "project": project, "bug": bug, "status": "SKIPPED", "top1": "",
                }
            invalid_final_response_retries(effective_config)
            finalization_limits(effective_config)
            selected_tests = (
                selected_trace_tests(localization_input, suite)
                if suite is not None
                else _tests_without_trace(localization_input, workspace)
            )
            selected_test_ids = [str(item["test_id"]) for item in selected_tests]
            if suite is not None:
                graphs = MethodExecutionGraphs(
                    trace_bug_dir,
                    effective_config,
                    timeout,
                    output_dir=bug_dir,
                    max_upstream_calls=max_upstream_calls,
                    max_downstream_calls=max_downstream_calls,
                    max_internal_calls=max_internal_calls,
                    workspace=workspace,
                    allowed_test_ids=selected_test_ids,
                    retain_debug_artifacts=retain_debug_artifacts,
                )
                failures = graphs.failure_evidence(selected_test_ids)
                method_ids = runtime_method_ids(
                    candidates, graphs.catalog, workspace
                )
                available_method_ids = graphs.available_method_ids()
                method_ids = {
                    candidate_id: method_id
                    for candidate_id, method_id in method_ids.items()
                    if method_id in available_method_ids
                }
            else:
                unavailable = "Unavailable because trace collection failed."
                failures = [
                    {
                        "test_id": str(item["test_id"]),
                        "test": str(item["test"]),
                        "error_stack": unavailable,
                        "test_output": unavailable,
                    }
                    for item in selected_tests
                ]
                method_ids = {}
            prompt = build_prompt(
                project,
                bug,
                localization_input["locator"],
                candidates,
                failures,
                workspace,
            )
            if agent_variant == AGENT_VARIANT_BASH_ONLY:
                method_ids = {}
            if dry_run:
                return {
                    "project": project,
                    "bug": bug,
                    "status": "DRY_RUN",
                    "top1": "",
                }
            inspection_dir = bug_dir / "inspection_graphs"
            if inspection_dir.is_dir():
                shutil.rmtree(inspection_dir)
            conversation_path = bug_dir / "refine_conversation.jsonl"
            conversation_path.unlink(missing_ok=True)
            (bug_dir / "refine_response_usage.jsonl").unlink(missing_ok=True)
            (bug_dir / "refine_render_errors.jsonl").unlink(missing_ok=True)
            agent_result = run_agent(
                effective_config,
                prompt,
                candidates,
                method_ids,
                graphs,
                workspace,
                timeout,
                conversation_path,
                selected_top_k,
            )
            candidate_by_id = {str(item["candidate_id"]): item for item in candidates}
            ranking = []
            new_index = 0
            for index, decision in enumerate(agent_result["ranking"], 1):
                input_candidate_id = decision["input_candidate_id"]
                if input_candidate_id is not None:
                    source = candidate_by_id[input_candidate_id]
                    item = dict(source)
                    candidate_id = input_candidate_id
                    original_rank = int(source["rank"])
                else:
                    new_index += 1
                    candidate_id = f"N{new_index:03d}"
                    original_rank = None
                    item = {
                        "function": decision["function"],
                        "signature": decision["signature"],
                        "source_file": decision["source_file"],
                        "start_line": decision["start_line"],
                        "end_line": decision["end_line"],
                    }
                item.update({
                    "candidate_id": candidate_id,
                    "rank": index,
                    "original_rank": original_rank,
                    "reason": decision["reason"],
                })
                ranking.append(item)
            retained_ids = {
                item["candidate_id"] for item in ranking
                if item["candidate_id"].startswith("L")
            }
            rejected = [
                item["candidate_id"] for item in candidates
                if item["candidate_id"] not in retained_ids
            ]
            inspected_method_ids = set(agent_result["inspected_method_ids"])
            inspected_candidate_ids = [
                candidate_id
                for candidate_id, method_id in method_ids.items()
                if method_id in inspected_method_ids
            ]
            output = {
                "schema": "fault-localization-refinement",
                "schema_version": 14,
                "project": project,
                "bug": bug,
                "status": "OK",
                "model": agent_result["model"],
                "agent_variant": agent_result["agent_variant"],
                "prompt_version": agent_result["prompt_version"],
                "execution_policy": agent_result.get(
                    "execution_policy", agent_variant_policy(agent_variant).to_dict()
                ),
                "trace_sources": agent_result.get(
                    "trace_sources", [],
                ),
                "inspection_windows": agent_result.get(
                    "inspection_windows", [],
                ),
                "locator": dict(localization_input["locator"]),
                "top_k": selected_top_k,
                "input_fingerprint": input_fingerprint,
                "configuration_fingerprint": configuration_fingerprint,
                "suite_fingerprint": suite_fingerprint,
                "test_count": len(selected_tests),
                "tests": [
                    {"test_id": item["test_id"], "test": item["test"]}
                    for item in selected_tests
                ],
                "input_ranking": candidates,
                "ranking": ranking,
                "rejected_candidate_ids": rejected,
                "candidate_runtime_method_ids": method_ids,
                "inspected_candidate_ids": inspected_candidate_ids,
                "tool_rounds": agent_result["tool_rounds"],
                "diagram_view_count": agent_result["diagram_view_count"],
                "viewed_diagrams": agent_result["viewed_diagrams"],
                "inspected_invocation_ids": agent_result["inspected_invocation_ids"],
                "queried_methods": agent_result["queried_methods"],
                "terminal_command_count": agent_result["terminal_command_count"],
                "request_count": agent_result["request_count"],
                "usage": agent_result["usage"],
                "finalization_attempt_count": agent_result[
                    "finalization_attempt_count"
                ],
                "final_length_retry_count": agent_result[
                    "final_length_retry_count"
                ],
                "final_finish_reason": agent_result["final_finish_reason"],
            }
            validate_refinement(output)
            write_json(result_path, output)
            return {
                "project": project,
                "bug": bug,
                "status": "OK",
                "top1": ranking[0]["function"],
            }
    except Exception as error:
        write_text(error_log, str(error) + "\n")
        return {
            "project": project, "bug": bug, "status": "ERROR", "top1": "",
        }
    finally:
        if graphs is not None:
            graphs.close()


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
    locator_results: Path,
    config_path: Path,
    timeout: int,
    top_k: int | None,
    dry_run: bool,
    force: bool = False,
    workers: int = 1,
    max_upstream_calls: int | None = None,
    max_downstream_calls: int | None = None,
    max_internal_calls: int | None = None,
    retain_debug_artifacts: bool = False,
    trace_layout: RunLayout | None = None,
) -> List[Dict[str, object]]:
    if trigger is not None:
        raise ValueError("refinement runs once per bug and does not accept --trigger")
    if workers <= 0:
        raise ValueError("workers must be positive")
    config = read_json(config_path)
    (
        max_upstream_calls,
        max_downstream_calls,
        max_internal_calls,
    ) = refinement_viewport_configuration(
        config,
        max_upstream_calls=max_upstream_calls,
        max_downstream_calls=max_downstream_calls,
        max_internal_calls=max_internal_calls,
    )
    items = _bug_items(
        trace_layout or layout, projects, bugs, locator_results
    )

    def refine_item(item: tuple[str, str]) -> Dict[str, object]:
        return _refine_bug(
            layout, item[0], item[1], locator_results, config, timeout,
            top_k, dry_run, force,
            max_upstream_calls, max_downstream_calls, max_internal_calls,
            retain_debug_artifacts, trace_layout,
        )

    if workers == 1 or len(items) <= 1:
        rows = [refine_item(item) for item in items]
    else:
        with ThreadPoolExecutor(
            max_workers=min(workers, len(items)),
            thread_name_prefix="dpex-refine",
        ) as executor:
            rows = list(executor.map(refine_item, items))
    write_csv(
        layout.logs / "refine.csv", rows,
        ["project", "bug", "status", "top1"],
    )
    return rows
