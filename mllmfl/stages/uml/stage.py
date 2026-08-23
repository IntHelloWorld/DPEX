import hashlib
import re
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from mllmfl.domain.interaction import IMAGE_ONLY_MODE, TEXT_INDEX_MODE
from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace
from mllmfl.domain.diagram_graph import (
    EXPAND_CALL,
    plan_diagram_graph,
)
from mllmfl.domain.schemas import validate_uml_index
from mllmfl.domain.test_slice import validate_slice_metadata
from mllmfl.infrastructure.io import read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.plantuml import render

from .adaptive import adaptive_graph_diagram_nodes
from .execution_compression import compress_execution
from .rendering import _root_test_invocation, _top_level_invocations


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
    plantuml_command: str,
    plantuml_jar: Path | None,
    timeout: int,
    force: bool = False,
    limit_size: int = 32768,
    max_visible_units: int = 24,
    max_participants: int = 8,
    batch_size: int = 100,
    interaction_mode: str = TEXT_INDEX_MODE,
) -> List[Dict[str, object]]:
    rows = []
    for project, bug, number, directory in layout.discover_triggers(projects, bugs, trigger):
        index_path = directory / "uml.json"
        error_log = layout.stage_log_dir("uml", project, bug, number) / "error.log"
        error_log.unlink(missing_ok=True)
        if index_path.exists() and not force:
            try:
                existing_index = validate_uml_index(read_json(index_path), directory)
                existing_mode = (
                    IMAGE_ONLY_MODE
                    if existing_index.get("schema_version") == 2
                    else TEXT_INDEX_MODE
                )
                if existing_mode != interaction_mode:
                    raise ValueError(
                        "existing UML interaction mode does not match the configuration; "
                        "rerun UML with --force"
                    )
                rows.append({"project": project, "bug": bug, "trigger": number,
                             "status": "SKIPPED",
                             "segment_count": existing_index["diagram_count"]})
            except Exception as error:
                write_text(error_log, str(error) + "\n")
                rows.append({"project": project, "bug": bug, "trigger": number,
                             "status": "ERROR", "segment_count": 0})
            continue
        try:
            missing_log = (
                layout.stage_log_dir("uml", project, bug, number)
                / "missing_images.log"
            )
            missing_log.unlink(missing_ok=True)
            complete_path = directory / "execution.json"
            complete_execution = read_json(complete_path)
            complete_trace = _root_test_invocation(complete_execution) is None
            if complete_trace:
                execution_path = complete_path
                execution = complete_execution
                roots = _top_level_invocations(execution)
                if not roots:
                    raise ValueError("complete trace has no top-level invocation")
                root, excluded_count = roots[0], 0
            else:
                sliced_path = directory / "execution_sliced.json"
                execution_path = sliced_path if sliced_path.exists() else complete_path
                execution = read_json(execution_path)
                if execution_path == sliced_path:
                    validate_slice_metadata(execution.get("slice"))
                root = _root_test_invocation(execution)
                if root is None:
                    raise ValueError("test root invocation was not found")
            index_path.unlink(missing_ok=True)
            segment_dir = directory / "sequence_diagrams"
            if segment_dir.exists():
                shutil.rmtree(segment_dir)
            segment_dir.mkdir(parents=True)
            (directory / "sequence.puml").unlink(missing_ok=True)
            (directory / "sequence.png").unlink(missing_ok=True)
            compressed = compress_execution(
                execution,
                max_sequence_pattern_length=max(1, max_visible_units - 2),
                max_sequence_participants=max_participants,
            )
            from mllmfl.domain.schemas import validate_compressed_execution
            validate_compressed_execution(compressed)
            write_json(directory / "execution_compressed.json", compressed)
            compressed_by_parent = {
                int(group["parent_invocation_id"]): group["calls"]
                for group in compressed["root_groups"]
            }
            root_class = "mllmfl.synthetic.ExecutionRoot"
            if complete_trace:
                roots = _top_level_invocations(execution)
                synthetic_children: List[Dict[str, Any]] = []
                for invocation in roots:
                    invocation_id = int(invocation["invocation_id"])
                    children = compressed_by_parent.get(invocation_id, [])
                    participants = {root_class, str(invocation["class"])}
                    for child in children:
                        participants.update(child["participant_classes"])
                    method = str(invocation["method"])
                    descriptor = str(invocation.get("descriptor") or "")
                    synthetic_children.append({
                        "representative_invocation_id": invocation_id,
                        "call": {
                            "caller": f"{root_class}.executionRoot",
                            "callee": f"{invocation['class']}.{method}",
                            "caller_class": root_class,
                            "callee_class": str(invocation["class"]),
                            "caller_method": "executionRoot",
                            "callee_method": method,
                            "caller_descriptor": "()V",
                            "callee_descriptor": descriptor,
                            "parent_invocation_id": 0,
                            "invocation_id": invocation_id,
                            "parent_chain": [],
                            "thread_id": int(invocation.get("thread_id") or 0),
                            "enter_seq": int(invocation.get("enter_seq") or 0),
                            "exit_seq": int(invocation.get("exit_seq") or 0),
                            "exit_type": str(invocation.get("exit_type") or "RETURN"),
                            "origin_test_line": 0,
                            "count": 1,
                            "context": True,
                            "invocation_ids": [invocation_id],
                        },
                        "invocation": dict(invocation),
                        "repeat_count": 1,
                        "represented_call_count": 1 + sum(
                            int(child["represented_call_count"])
                            for child in children
                        ),
                        "displayed_subtree_call_count": 1 + sum(
                            int(child["displayed_subtree_call_count"])
                            for child in children
                        ),
                        "participant_classes": sorted(participants),
                        "subtree_fingerprint": "top-level-boundary",
                        "children": children,
                    })
                focus = {
                    "representative_invocation_id": 0,
                    "call": None,
                    "invocation": {
                        "invocation_id": 0,
                        "parent_id": 0,
                        "class": root_class,
                        "method": "executionRoot",
                        "descriptor": "()V",
                        "enter_seq": 0,
                        "exit_seq": max(
                            [int(item.get("exit_seq") or 0) for item in roots] or [0]
                        ) + 1,
                        "exit_type": "RETURN",
                    },
                    "repeat_count": 1,
                    "represented_call_count": sum(
                        int(item["represented_call_count"])
                        for item in synthetic_children
                    ),
                    "displayed_subtree_call_count": 1 + sum(
                        int(item["displayed_subtree_call_count"])
                        for item in synthetic_children
                    ),
                    "participant_classes": sorted(
                        {root_class}
                        | {
                            str(value)
                            for item in synthetic_children
                            for value in item["participant_classes"]
                        }
                    ),
                    "subtree_fingerprint": "synthetic-execution-root",
                    "children": synthetic_children,
                }
                entry_reason = "synthetic_execution_root"
                excluded_count = 0
            else:
                root_id = int(root["invocation_id"])
                compressed_roots = compressed_by_parent.get(root_id, [])
                represented = sum(
                    int(item["represented_call_count"]) for item in compressed_roots
                )
                excluded_count = len(execution["calls"]) - represented
                participants = {str(root["class"])}
                for item in compressed_roots:
                    participants.update(item["participant_classes"])
                focus = {
                    "representative_invocation_id": root_id,
                    "call": None,
                    "invocation": dict(root),
                    "repeat_count": 1,
                    "represented_call_count": represented,
                    "displayed_subtree_call_count": 1 + sum(
                        int(item["displayed_subtree_call_count"])
                        for item in compressed_roots
                    ),
                    "participant_classes": sorted(participants),
                    "subtree_fingerprint": "test-execution-root",
                    "children": compressed_roots,
                }
                entry_reason = "test_invocation"
            nodes, entry_diagram_id, render_failures, method_catalog = (
                adaptive_graph_diagram_nodes(
                execution, focus, entry_reason, segment_dir,
                project, bug, number, plantuml_command, plantuml_jar, timeout,
                limit_size, max_visible_units, max_participants, batch_size,
                interaction_mode,
                )
            )
            if render_failures:
                write_text(
                    missing_log,
                    "\n".join(
                        f"{item['diagram_id']}\t{item['image']}\t{item['error']}"
                        for item in render_failures
                    ) + "\n",
                )
                raise RuntimeError(
                    f"PlantUML did not successfully generate {len(render_failures)} diagram images; "
                    f"see {missing_log}"
                )
            missing_log.unlink(missing_ok=True)
            trace_call_count = len(execution["calls"])
            layout_root_call_count = len(roots) if complete_trace else 0
            partitioned_count = (
                trace_call_count + layout_root_call_count
                if complete_trace
                else trace_call_count - excluded_count
            )
            index = {
                "schema": "execution-uml-graph",
                "schema_version": 2 if interaction_mode == IMAGE_ONLY_MODE else 1,
                "source_schema": execution["schema"],
                "source_file": execution_path.name,
                "compressed_source_file": "execution_compressed.json",
                "compression_schema": compressed["schema"],
                "test": dict(execution.get("test") or {}),
                "root_invocation_id": 0 if complete_trace else int(root["invocation_id"]),
                "strategy": (
                    "synthetic-root-adaptive-graph"
                    if complete_trace else "test-root-adaptive-graph"
                ),
                "slice_applied": (
                    False if complete_trace
                    else bool((execution.get("slice") or {}).get("applied"))
                ),
                "trace_call_count": trace_call_count,
                "layout_root_call_count": layout_root_call_count,
                "source_call_count": trace_call_count + layout_root_call_count,
                "partitioned_call_count": partitioned_count,
                "excluded_call_count": excluded_count,
                "max_visible_units": max_visible_units,
                "max_participants_per_image": max_participants,
                "plantuml_batch_size": batch_size,
                "entry_diagram_id": entry_diagram_id,
                "entry_reason": entry_reason,
                "node_count": len(nodes),
                "diagram_count": len(nodes),
                "nodes": nodes,
            }
            if interaction_mode == IMAGE_ONLY_MODE:
                index["interaction_mode"] = IMAGE_ONLY_MODE
                index["method_catalog"] = method_catalog
            validate_uml_index(index, directory)
            write_json(index_path, index)
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "OK", "segment_count": len(nodes)})
        except Exception as error:
            write_text(error_log, str(error) + "\n")
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "ERROR", "segment_count": 0})
    write_csv(layout.logs / "uml.csv", rows,
              ["project", "bug", "trigger", "status", "segment_count"])
    return rows
