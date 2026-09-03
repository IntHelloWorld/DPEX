import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Sequence

from mllmfl.domain.schemas import (
    validate_refinement,
    validate_trace_suite,
)
from mllmfl.infrastructure.io import read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout
from .agent import finalization_limits, run_agent
from .client import invalid_final_json_retries
from .context import (
    build_prompt,
    defect_evidence,
    runtime_method_ids,
    selected_trace_tests,
)
from .graphs import MethodExecutionGraphs
from .input import load_localization_input


def _bug_items(
    layout: RunLayout, projects: Sequence[str], bugs: set[str] | None
) -> list[tuple[str, str]]:
    return sorted({
        (project, bug)
        for project, bug, _, _ in layout.discover_triggers(projects, bugs)
    })


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
) -> Dict[str, object]:
    bug_dir = layout.artifacts / project / f"bug_{bug}"
    result_path = bug_dir / "refinement.json"
    error_log = layout.stage_log_dir("refine", project, bug) / "error.log"
    error_log.unlink(missing_ok=True)
    try:
        workspace = layout.workspace_dir(project, bug)
        localization_input = load_localization_input(
            locator_results, project, bug, workspace
        )
        suite = validate_trace_suite(
            read_json(bug_dir / "trace_suite.json")
        )
        candidates = list(localization_input["ranking"])
        cfg = config.get("mllm", config)
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
        input_fingerprint = hashlib.sha256(json.dumps(
            fingerprint_input, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        configuration_fingerprint = hashlib.sha256(json.dumps(
            {
                "config": config,
                "refinement_viewport": {
                    "max_upstream_calls": max_upstream_calls,
                    "max_downstream_calls": max_downstream_calls,
                    "max_internal_calls": max_internal_calls,
                },
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        suite_fingerprint = str(suite["method_catalog_fingerprint"])
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
            ):
                raise ValueError(
                    "existing refinement does not match input, config, trace suite, or "
                    "top_k; rerun with --force"
                )
            return {
                "project": project, "bug": bug, "status": "SKIPPED", "top1": "",
            }
        invalid_final_json_retries(config)
        finalization_limits(config)
        selected_tests = selected_trace_tests(localization_input, suite)
        selected_test_ids = [str(item["test_id"]) for item in selected_tests]
        failures = defect_evidence(
            layout, project, bug, suite, selected_test_ids
        )
        prompt = build_prompt(
            project,
            bug,
            localization_input["locator"],
            candidates,
            failures,
            workspace,
        )
        graphs = MethodExecutionGraphs(
            bug_dir,
            config,
            timeout,
            max_upstream_calls=max_upstream_calls,
            max_downstream_calls=max_downstream_calls,
            max_internal_calls=max_internal_calls,
            workspace=workspace,
            allowed_test_ids=selected_test_ids,
        )
        method_ids = runtime_method_ids(candidates, graphs.catalog, workspace)
        available_method_ids = graphs.available_method_ids()
        method_ids = {
            candidate_id: method_id
            for candidate_id, method_id in method_ids.items()
            if method_id in available_method_ids
        }
        if dry_run:
            return {
                "project": project,
                "bug": bug,
                "status": "DRY_RUN",
                "top1": "",
            }
        if force:
            inspection_dir = bug_dir / "inspection_graphs"
            if inspection_dir.is_dir():
                shutil.rmtree(inspection_dir)
        conversation_path = bug_dir / "refine_conversation.jsonl"
        (bug_dir / "refine_response_usage.jsonl").unlink(missing_ok=True)
        (bug_dir / "refine_render_errors.jsonl").unlink(missing_ok=True)
        agent_result = run_agent(
            config,
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
            "schema_version": 5,
            "project": project,
            "bug": bug,
            "status": "OK",
            "model": agent_result["model"],
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
            "finalization_attempts": agent_result["finalization_attempts"],
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
    max_upstream_calls: int = 6,
    max_downstream_calls: int = 6,
    max_internal_calls: int = 10,
) -> List[Dict[str, object]]:
    if trigger is not None:
        raise ValueError("refinement runs once per bug and does not accept --trigger")
    if workers <= 0:
        raise ValueError("workers must be positive")
    if max_upstream_calls < 1:
        raise ValueError("max_upstream_calls must be positive")
    if max_downstream_calls < 1:
        raise ValueError("max_downstream_calls must be positive")
    if max_internal_calls < 1:
        raise ValueError("max_internal_calls must be positive")
    config = read_json(config_path)
    items = _bug_items(layout, projects, bugs)

    def refine_item(item: tuple[str, str]) -> Dict[str, object]:
        return _refine_bug(
            layout, item[0], item[1], locator_results, config, timeout,
            top_k, dry_run, force,
            max_upstream_calls, max_downstream_calls, max_internal_calls,
        )

    if workers == 1 or len(items) <= 1:
        rows = [refine_item(item) for item in items]
    else:
        with ThreadPoolExecutor(
            max_workers=min(workers, len(items)),
            thread_name_prefix="mllmfl-refine",
        ) as executor:
            rows = list(executor.map(refine_item, items))
    write_csv(
        layout.logs / "refine.csv", rows,
        ["project", "bug", "status", "top1"],
    )
    return rows
