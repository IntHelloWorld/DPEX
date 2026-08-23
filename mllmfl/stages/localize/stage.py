import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import requests

from mllmfl.domain.models import Ranking
from mllmfl.domain.failure import extract_error_stack
from mllmfl.domain.interaction import (
    IMAGE_ONLY_MODE,
    TEXT_INDEX_MODE,
    localization_interaction_mode,
)
from mllmfl.domain.schemas import (
    validate_candidates,
    validate_defect_context,
    validate_localization,
    validate_uml_index,
)
from mllmfl.domain.test_slice import validate_slice_metadata
from mllmfl.infrastructure.io import append_jsonl, read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout

from . import run_agent
from .context import (
    build_prompt,
    defect_output_context,
    invalid_final_json_retries,
    system_prompt,
    test_code_context,
)
from .parsing import (
    attach_source_locations,
    gate_method_id_ranking,
    gate_ranking,
    parse_model_response,
    validate_model_ranking_payload,
)


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
) -> List[Dict[str, object]]:
    config = read_json(config_path)
    cfg = config.get("mllm", config)
    interaction_mode = localization_interaction_mode(config)
    selected_top_k = top_k if top_k is not None else int(cfg.get("top_k", 5))
    if selected_top_k <= 0:
        raise ValueError("top_k must be positive")
    invalid_final_json_retries(config)
    rows = []
    for project, bug, number, directory in layout.discover_triggers(projects, bugs, trigger):
        result_path = directory / "localization.json"
        if result_path.exists() and not force:
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "SKIPPED", "top1": ""})
            continue
        try:
            data = read_json(directory / "candidates.json")
            validate_candidates(data)
            uml_index = read_json(directory / "uml.json")
            validate_uml_index(uml_index, directory)
            if uml_index.get("schema") != "execution-uml-graph":
                raise ValueError(
                    "legacy UML index is not supported by localization; regenerate the UML stage"
                )
            artifact_mode = (
                IMAGE_ONLY_MODE
                if uml_index.get("schema_version") == 2
                else TEXT_INDEX_MODE
            )
            if artifact_mode != interaction_mode:
                raise ValueError(
                    "UML interaction mode does not match mllm.interaction_mode; "
                    "regenerate the UML stage with the same configuration"
                )
            candidates = data.get("candidates") or []
            test = (directory / "trigger_test.txt").read_text(encoding="utf-8").strip()
            test_code = test_code_context(layout, project, bug, directory, test)
            error_stack, test_output = defect_output_context(
                layout, project, bug, number, directory, test
            )
            prompt = build_prompt(
                test, test_code, error_stack, test_output, uml_index, selected_top_k,
                interaction_mode,
            )
            write_text(
                directory / "prompt.txt",
                system_prompt(interaction_mode) + "\n\n" + prompt + "\n",
            )
            conversation_path = directory / "conversation.jsonl"
            conversation_path.unlink(missing_ok=True)
            (directory / "response_usage.jsonl").unlink(missing_ok=True)
            if dry_run:
                status, ranking, dropped, model = "DRY_RUN", [], [], ""
                viewed, tool_rounds, diagram_view_count = [], 0, 0
                returned_method_ids: List[str] = []
                location_dropped: List[str] = []
            else:
                raw, model, viewed, tool_rounds, diagram_view_count = run_agent(
                    config,
                    prompt,
                    uml_index,
                    directory,
                    timeout,
                    conversation_path,
                    top_k=selected_top_k,
                )
                parsed = parse_model_response(raw)
                if parsed is None:
                    raise ValueError("model returned invalid final ranking JSON")
                model_ranking = validate_model_ranking_payload(
                    parsed, selected_top_k, interaction_mode
                )
                viewed_set = set(viewed)
                if interaction_mode == IMAGE_ONLY_MODE:
                    viewed_method_ids = [
                        method_id
                        for node in uml_index["nodes"]
                        if node["diagram_id"] in viewed_set
                        for method_id in node["method_ids"]
                    ]
                    ranking, dropped = gate_method_id_ranking(
                        model_ranking,
                        [candidate["function"] for candidate in candidates],
                        uml_index["method_catalog"],
                        viewed_method_ids,
                        selected_top_k,
                    )
                else:
                    viewed_signatures = [
                        signature
                        for node in uml_index.get("nodes") or uml_index.get("segments") or []
                        if node["diagram_id"] in viewed_set
                        for signature in node["method_signatures"]
                    ]
                    ranking, dropped = gate_ranking(
                        model_ranking,
                        [candidate["function"] for candidate in candidates],
                        viewed_signatures,
                        selected_top_k,
                    )
                ranking, location_dropped = attach_source_locations(
                    ranking, layout.workspace_dir(project, bug)
                )
                returned_method_ids = (
                    [item.method_id for item in ranking]
                    if interaction_mode == IMAGE_ONLY_MODE else []
                )
                status = "OK" if ranking else "EMPTY_RANKING"
            output = {
                "schema": "fault-localization",
                "schema_version": 4,
                "project": project,
                "bug": bug,
                "trigger": number,
                "status": status,
                "model": model,
                "interaction_mode": interaction_mode,
                "candidate_count": len(candidates),
                "diagram_count": int(
                    uml_index.get("diagram_count") or len(uml_index.get("nodes") or [])
                ),
                "tool_rounds": tool_rounds,
                "diagram_view_count": diagram_view_count,
                "viewed_diagrams": viewed,
                "ranking": [item.to_dict() for item in ranking],
                "dropped_invalid_signatures": dropped,
                "dropped_unresolved_source_methods": location_dropped,
            }
            if interaction_mode == IMAGE_ONLY_MODE:
                output["returned_method_ids"] = returned_method_ids
                output["dropped_invalid_method_ids"] = dropped
            validate_localization(output)
            write_json(result_path, output)
            top1 = ranking[0].function if ranking else ""
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": status, "top1": top1})
        except Exception as error:
            write_text(layout.stage_log_dir("localize", project, bug, number) / "error.log",
                       str(error) + "\n")
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "ERROR", "top1": ""})
    write_csv(layout.logs / "localize.csv", rows,
              ["project", "bug", "trigger", "status", "top1"])
    return rows
