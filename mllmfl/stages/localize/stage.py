from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Sequence

from mllmfl.domain.schemas import (
    validate_localization,
    validate_uml_index,
    validate_uml_suite,
)
from mllmfl.infrastructure.io import read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.plantuml import renderer_available, runtime_settings

from . import run_agent
from .context import (
    build_prompt,
    build_system_prompt,
    defect_output_context,
    invalid_final_json_retries,
    test_code_context,
)
from .parsing import (
    attach_source_locations,
    gate_method_id_ranking,
    parse_model_response,
    validate_model_ranking_payload,
)


def _group_triggers(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
) -> Dict[tuple[str, str], List[tuple[str, Path]]]:
    grouped: Dict[tuple[str, str], List[tuple[str, Path]]] = {}
    for project, bug, number, directory in layout.discover_triggers(projects, bugs):
        grouped.setdefault((project, bug), []).append((number, directory))
    return grouped


def _runtime_bundle(
    layout: RunLayout,
    project: str,
    bug: str,
    bug_dir: Path,
    suite: Dict[str, Any],
) -> Dict[str, Any]:
    nodes = []
    runtime_tests = []
    catalog_by_id = {
        str(item["method_id"]): item for item in suite["method_catalog"]
    }

    for expected_trigger, spec in enumerate(suite["tests"], 1):
        number = str(spec["trigger"])
        if int(number) != expected_trigger:
            raise ValueError("UML suite triggers are not contiguous")
        graph_path = bug_dir / Path(*Path(str(spec["uml"])).parts)
        trigger_dir = graph_path.parent
        graph = validate_uml_index(read_json(graph_path), trigger_dir)
        if (
            graph.get("schema_version") not in {3, 4}
            or graph.get("test_id") != spec["test_id"]
            or graph.get("entry_diagram_id") != spec["entry_diagram_id"]
            or graph.get("method_catalog_fingerprint")
            != suite["method_catalog_fingerprint"]
        ):
            raise ValueError(f"UML suite graph mismatch: {spec['test_id']}")
        for item in graph["method_catalog"]:
            if catalog_by_id.get(str(item["method_id"])) != item:
                raise ValueError(f"UML graph method catalog mismatch: {spec['test_id']}")

        test = (trigger_dir / "trigger_test.txt").read_text(
            encoding="utf-8"
        ).strip()
        if test != spec["test"]:
            raise ValueError(f"failing test does not match UML suite: {spec['test_id']}")
        test_code = test_code_context(layout, project, bug, trigger_dir, test)
        error_stack, test_output = defect_output_context(
            layout, project, bug, number, trigger_dir, test
        )
        runtime_tests.append({
            **spec,
            "call_count": int(graph["trace_call_count"]),
            "diagram_count": int(graph["diagram_count"]),
            "test_code": test_code,
            "error_stack": error_stack,
            "test_output": test_output,
        })

        for source_node in graph["nodes"]:
            node = dict(source_node)
            node["_rendering"] = dict(
                graph.get("rendering") or {"mode": "eager", "format": "png"}
            )
            node["image"] = (
                trigger_dir / Path(*Path(str(source_node["image"])).parts)
            ).relative_to(bug_dir).as_posix()
            node["puml"] = (
                trigger_dir / Path(*Path(str(source_node["puml"])).parts)
            ).relative_to(bug_dir).as_posix()
            nodes.append(node)

    diagram_ids = [str(node["diagram_id"]) for node in nodes]
    if len(diagram_ids) != len(set(diagram_ids)):
        raise ValueError("duplicate diagram ID across failing tests")
    if len(nodes) != int(suite["diagram_count"]):
        raise ValueError("UML suite diagram count mismatch")
    return {
        "nodes": nodes,
        "tests": runtime_tests,
        "method_catalog": suite["method_catalog"],
    }


def _localize_bug(
    layout: RunLayout,
    project: str,
    bug: str,
    config: Dict[str, Any],
    timeout: int,
    selected_top_k: int,
    dry_run: bool,
    force: bool,
) -> Dict[str, object]:
    bug_dir = layout.artifacts / project / f"bug_{bug}"
    result_path = bug_dir / "localization.json"
    if result_path.exists() and not force:
        try:
            existing = validate_localization(read_json(result_path))
            if (
                existing.get("schema_version") != 5
                or existing.get("project") != project
                or existing.get("bug") != bug
            ):
                raise ValueError("existing localization is not bug-level v5")
            return {
                "project": project,
                "bug": bug,
                "status": "SKIPPED",
                "top1": "",
            }
        except Exception as error:
            write_text(
                layout.stage_log_dir("localize", project, bug) / "error.log",
                str(error) + "\n",
            )
            return {
                "project": project, "bug": bug, "status": "ERROR", "top1": "",
            }

    try:
        suite = validate_uml_suite(read_json(bug_dir / "uml_suite.json"), bug_dir)
        if suite["project"] != project or suite["bug"] != bug:
            raise ValueError("UML suite identity does not match its path")
        bundle = _runtime_bundle(layout, project, bug, bug_dir, suite)
        if dry_run and any(
            (node.get("_rendering") or {}).get("mode") == "on_demand"
            for node in bundle["nodes"]
        ):
            rendering = next(
                node["_rendering"]
                for node in bundle["nodes"]
                if node["_rendering"].get("mode") == "on_demand"
            )
            renderer_available(*runtime_settings(config, rendering))
        public_tests = [
            {
                "test_id": item["test_id"],
                "test": item["test"],
                "entry_diagram_id": item["entry_diagram_id"],
            }
            for item in bundle["tests"]
        ]
        prompt = build_prompt(bundle["tests"])
        write_text(
            bug_dir / "prompt.txt",
            build_system_prompt(selected_top_k) + "\n\n" + prompt + "\n",
        )
        conversation_path = bug_dir / "conversation.jsonl"
        conversation_path.unlink(missing_ok=True)
        (bug_dir / "response_usage.jsonl").unlink(missing_ok=True)
        (bug_dir / "render_errors.jsonl").unlink(missing_ok=True)

        if dry_run:
            status, ranking, dropped, model = "DRY_RUN", [], [], ""
            viewed, tool_rounds, diagram_view_count = [], 0, 0
            returned_method_ids: List[str] = []
            location_dropped: List[str] = []
        else:
            raw, model, viewed, tool_rounds, diagram_view_count = run_agent(
                config,
                prompt,
                bundle,
                bug_dir,
                timeout,
                conversation_path,
                top_k=selected_top_k,
            )
            parsed = parse_model_response(raw)
            if parsed is None:
                raise ValueError("model returned invalid final ranking JSON")
            model_ranking = validate_model_ranking_payload(parsed, selected_top_k)
            viewed_set = set(viewed)
            viewed_method_ids = [
                method_id
                for node in bundle["nodes"]
                if node["diagram_id"] in viewed_set
                for method_id in node["method_ids"]
            ]
            ranking, dropped = gate_method_id_ranking(
                model_ranking,
                suite["method_catalog"],
                viewed_method_ids,
                selected_top_k,
            )
            ranking, location_dropped = attach_source_locations(
                ranking, layout.workspace_dir(project, bug)
            )
            returned_method_ids = [item.method_id for item in ranking]
            status = "OK" if ranking else "EMPTY_RANKING"

        entry_to_test = {
            str(item["entry_diagram_id"]): str(item["test_id"])
            for item in bundle["tests"]
        }
        viewed_test_ids = [
            entry_to_test[diagram_id]
            for diagram_id in viewed
            if diagram_id in entry_to_test
        ]
        output = {
            "schema": "fault-localization",
            "schema_version": 5,
            "project": project,
            "bug": bug,
            "status": status,
            "model": model,
            "top_k": selected_top_k,
            "test_count": len(public_tests),
            "tests": public_tests,
            "viewed_test_ids": viewed_test_ids,
            "candidate_count": len(suite["method_catalog"]),
            "diagram_count": int(suite["diagram_count"]),
            "tool_rounds": tool_rounds,
            "diagram_view_count": diagram_view_count,
            "viewed_diagrams": viewed,
            "ranking": [item.to_dict() for item in ranking],
            "returned_method_ids": returned_method_ids,
            "dropped_invalid_method_ids": dropped,
            "dropped_unresolved_source_methods": location_dropped,
        }
        validate_localization(output)
        write_json(result_path, output)
        top1 = ranking[0].function if ranking else ""
        return {
            "project": project, "bug": bug, "status": status, "top1": top1,
        }
    except Exception as error:
        write_text(
            layout.stage_log_dir("localize", project, bug) / "error.log",
            str(error) + "\n",
        )
        return {
            "project": project, "bug": bug, "status": "ERROR", "top1": "",
        }


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
    config_path: Path,
    timeout: int,
    top_k: int | None,
    dry_run: bool,
    force: bool = False,
    workers: int = 1,
) -> List[Dict[str, object]]:
    if trigger is not None:
        raise ValueError("localization runs once per bug and does not accept --trigger")
    if workers <= 0:
        raise ValueError("workers must be positive")
    config = read_json(config_path)
    cfg = config.get("mllm", config)
    if "interaction_mode" in cfg:
        raise ValueError("mllm.interaction_mode was removed; localization is image-only")
    selected_top_k = top_k if top_k is not None else int(cfg.get("top_k", 5))
    if selected_top_k <= 0:
        raise ValueError("top_k must be positive")
    invalid_final_json_retries(config)

    bug_items = sorted(_group_triggers(layout, projects, bugs))

    def localize_item(item: tuple[str, str]) -> Dict[str, object]:
        project, bug = item
        return _localize_bug(
            layout,
            project,
            bug,
            config,
            timeout,
            selected_top_k,
            dry_run,
            force,
        )

    if workers == 1 or len(bug_items) <= 1:
        rows = [localize_item(item) for item in bug_items]
    else:
        with ThreadPoolExecutor(
            max_workers=min(workers, len(bug_items)),
            thread_name_prefix="mllmfl-localize",
        ) as executor:
            rows = list(executor.map(localize_item, bug_items))

    write_csv(
        layout.logs / "localize.csv",
        rows,
        ["project", "bug", "status", "top1"],
    )
    return rows
