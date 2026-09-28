"""Standalone fault localization without an upstream ranking."""
import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Sequence

from dpex.domain.schemas import validate_localization_result
from dpex.infrastructure.checkouts import temporary_checkout
from dpex.infrastructure.failure_cache import (
    build_failure_cache,
    failure_cache_dir,
    load_failure_cache,
)
from dpex.infrastructure.io import read_json, write_csv, write_json, write_text
from dpex.infrastructure.layout import RunLayout
from dpex.stages.refine.agent import finalization_limits, run_agent
from dpex.stages.refine.client import invalid_final_response_retries
from dpex.stages.refine.context import (
    AGENT_VARIANT_BASH_ONLY,
    LOCALIZATION_PROMPT_VERSIONS,
    build_localization_prompt,
    agent_variant_policy,
    refinement_agent_variant,
)
from dpex.stages.refine.graphs import MethodExecutionGraphs
from dpex.stages.refine.stage import refinement_viewport_configuration


def _items(
    trace_layout: RunLayout,
    cache_root: Path,
    projects: Sequence[str],
    bugs: set[str] | None,
) -> list[tuple[str, str]]:
    if bugs is not None:
        return sorted(
            {(project, bug) for project in set(projects) for bug in bugs},
            key=lambda item: (item[0], int(item[1])),
        )
    found = set()
    for project in set(projects):
        for path in (trace_layout.artifacts / project).glob("bug_*/trace_suite.json"):
            bug = path.parent.name.removeprefix("bug_")
            if bug.isdigit():
                found.add((project, bug))
        cache_project = cache_root / "artifacts" / project
        for path in cache_project.glob("bug_*/failing_tests/manifest.json"):
            bug = path.parents[1].name.removeprefix("bug_")
            if bug.isdigit():
                found.add((project, bug))
    return sorted(found, key=lambda item: (item[0], int(item[1])))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _localize_bug(
    layout: RunLayout,
    trace_layout: RunLayout,
    cache_root: Path,
    project: str,
    bug: str,
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
    bug_dir.mkdir(parents=True, exist_ok=True)
    error_log = layout.stage_log_dir("localize", project, bug) / "error.log"
    error_log.unlink(missing_ok=True)
    result_path = bug_dir / "localization.json"
    try:
        trace_bug_dir = trace_layout.artifacts / project / f"bug_{bug}"
        if (trace_bug_dir / "trace_suite.json").is_file():
            manifest = build_failure_cache(
                trace_bug_dir, cache_root, project, bug
            )
        else:
            cached = load_failure_cache(cache_root, project, bug)
            if cached is None:
                raise ValueError(
                    "no reusable failing-test cache or trace suite is available"
                )
            manifest = {**cached, "cache_hit": True}
        effective = config
        cfg = effective.get("dpex", effective)
        if not isinstance(cfg, dict):
            raise ValueError("dpex configuration must be an object")
        agent_variant = refinement_agent_variant(effective)
        suite_available = (trace_bug_dir / "trace_suite.json").is_file()
        trace_suite = (
            read_json(trace_bug_dir / "trace_suite.json") if suite_available else None
        )
        if agent_variant != AGENT_VARIANT_BASH_ONLY and not suite_available:
            raise ValueError(
                f"{agent_variant} standalone localization requires a trace suite; "
                "use --trace-root or configure bash-only"
            )
        top_k = requested_top_k if requested_top_k is not None else int(
            cfg.get("top_k", 5)
        )
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        invalid_final_response_retries(effective)
        finalization_limits(effective)
        tests = [{
            "test_id": str(item["test_id"]),
            "test": str(item["test"]),
            "failure_file": f"failing-tests/{item['file']}",
            "failure_sha256": str(item["sha256"]),
        } for item in manifest["tests"]]
        input_fingerprint = _fingerprint({
            "project": project,
            "bug": bug,
            "source_fingerprint": manifest["source_fingerprint"],
            "tests": tests,
            "trace_sources": [
                {
                    "test_id": item["test_id"],
                    "trace_fingerprint": item["trace_fingerprint"],
                }
                for item in (trace_suite or {}).get("tests", [])
            ],
        })
        configuration_fingerprint = _fingerprint({
            "config": effective,
            "viewport": {
                "max_upstream_calls": max_upstream_calls,
                "max_downstream_calls": max_downstream_calls,
                "max_internal_calls": max_internal_calls,
            },
        })
        if result_path.is_file() and not force:
            existing = validate_localization_result(read_json(result_path))
            if (
                existing["project"] != project or existing["bug"] != bug
                or existing["input_fingerprint"] != input_fingerprint
                or existing["configuration_fingerprint"] != configuration_fingerprint
                or existing["top_k"] != top_k
            ):
                raise ValueError(
                    "existing localization does not match input or config; rerun with --force"
                )
            return {"project": project, "bug": bug, "status": "SKIPPED", "top1": ""}
        if dry_run:
            if agent_variant != AGENT_VARIANT_BASH_ONLY:
                validation_graphs = MethodExecutionGraphs(
                    trace_bug_dir, effective, timeout,
                    output_dir=bug_dir,
                    max_upstream_calls=max_upstream_calls,
                    max_downstream_calls=max_downstream_calls,
                    max_internal_calls=max_internal_calls,
                    allowed_test_ids=[item["test_id"] for item in tests],
                )
                validation_graphs.close()
            return {"project": project, "bug": bug, "status": "DRY_RUN", "top1": ""}

        graphs = None
        with temporary_checkout(layout, project, bug, timeout) as workspace:
            prompt = build_localization_prompt(project, bug, tests)
            cache_dir = failure_cache_dir(cache_root, project, bug)
            evidence_files = {
                str(item["failure_file"]): cache_dir / Path(str(source["file"]))
                for item, source in zip(tests, manifest["tests"])
            }
            conversation_path = bug_dir / "localize_conversation.jsonl"
            inspection_dir = bug_dir / "inspection_graphs"
            if inspection_dir.is_dir():
                shutil.rmtree(inspection_dir)
            conversation_path.unlink(missing_ok=True)
            (bug_dir / "localize_response_usage.jsonl").unlink(missing_ok=True)
            try:
                if agent_variant != AGENT_VARIANT_BASH_ONLY:
                    graphs = MethodExecutionGraphs(
                        trace_bug_dir, effective, timeout,
                        output_dir=bug_dir,
                        max_upstream_calls=max_upstream_calls,
                        max_downstream_calls=max_downstream_calls,
                        max_internal_calls=max_internal_calls,
                        workspace=workspace,
                        allowed_test_ids=[item["test_id"] for item in tests],
                        retain_debug_artifacts=False,
                    )
                agent_result = run_agent(
                    effective, prompt, [], {}, graphs, workspace, timeout,
                    conversation_path, top_k,
                    standalone=True,
                    evidence_files=evidence_files,
                    artifact_stem="localize",
                )
            finally:
                if graphs is not None:
                    graphs.close()
        ranking = [{
            "candidate_id": f"N{index:03d}",
            "rank": index,
            "original_rank": None,
            "function": item["function"],
            "signature": item["signature"],
            "source_file": item["source_file"],
            "start_line": item["start_line"],
            "end_line": item["end_line"],
            "reason": item["reason"],
        } for index, item in enumerate(agent_result["ranking"], 1)]
        output = {
            "schema": "fault-localization-result",
            "schema_version": 2,
            "project": project,
            "bug": bug,
            "status": "OK",
            "model": agent_result["model"],
            "agent_variant": agent_variant,
            "prompt_version": LOCALIZATION_PROMPT_VERSIONS[agent_variant],
            "execution_policy": agent_result.get(
                "execution_policy", agent_variant_policy(agent_variant).to_dict()
            ),
            "trace_sources": agent_result.get(
                "trace_sources", [],
            ),
            "inspection_windows": agent_result.get(
                "inspection_windows", [],
            ),
            "top_k": top_k,
            "input_fingerprint": input_fingerprint,
            "configuration_fingerprint": configuration_fingerprint,
            "failure_cache_hit": bool(manifest["cache_hit"]),
            "test_count": len(tests),
            "tests": tests,
            "ranking": ranking,
            "tool_rounds": agent_result["tool_rounds"],
            "diagram_view_count": agent_result["diagram_view_count"],
            "viewed_diagrams": agent_result["viewed_diagrams"],
            "inspected_invocation_ids": agent_result["inspected_invocation_ids"],
            "queried_methods": agent_result["queried_methods"],
            "terminal_command_count": agent_result["terminal_command_count"],
            "request_count": agent_result["request_count"],
            "usage": agent_result["usage"],
            "finalization_attempt_count": agent_result["finalization_attempt_count"],
            "final_length_retry_count": agent_result["final_length_retry_count"],
            "final_finish_reason": agent_result["final_finish_reason"],
        }
        validate_localization_result(output)
        write_json(result_path, output)
        return {"project": project, "bug": bug, "status": "OK", "top1": ranking[0]["function"]}
    except Exception as error:
        write_text(error_log, str(error) + "\n")
        return {"project": project, "bug": bug, "status": "ERROR", "top1": ""}


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    config_path: Path,
    timeout: int,
    top_k: int | None,
    dry_run: bool,
    force: bool = False,
    workers: int = 1,
    trace_layout: RunLayout | None = None,
    cache_root: Path | None = None,
    max_upstream_calls: int | None = None,
    max_downstream_calls: int | None = None,
    max_internal_calls: int | None = None,
) -> List[Dict[str, object]]:
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
    source_layout = trace_layout or layout
    shared_cache = cache_root or layout.root.parent / ".dpex-cache"
    items = _items(source_layout, shared_cache, projects, bugs)

    def localize_item(item: tuple[str, str]) -> Dict[str, object]:
        return _localize_bug(
            layout, source_layout, shared_cache, item[0], item[1], config,
            timeout, top_k, dry_run, force,
            max_upstream_calls, max_downstream_calls, max_internal_calls,
        )

    if workers == 1 or len(items) <= 1:
        rows = [localize_item(item) for item in items]
    else:
        with ThreadPoolExecutor(
            max_workers=min(workers, len(items)),
            thread_name_prefix="dpex-localize",
        ) as executor:
            rows = list(executor.map(localize_item, items))
    write_csv(layout.logs / "localize.csv", rows, ["project", "bug", "status", "top1"])
    return rows
