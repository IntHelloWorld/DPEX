import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Sequence

from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace
from mllmfl.domain.schemas import validate_uml_index, validate_uml_suite
from mllmfl.domain.test_slice import validate_slice_metadata
from mllmfl.infrastructure.io import read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout

from .adaptive import adaptive_graph_diagram_nodes
from .execution_compression import compress_execution
from .rendering import _root_test_invocation, _top_level_invocations, readable_signature


def _test_id(trigger: str) -> str:
    number = int(trigger)
    if number <= 0:
        raise ValueError("trigger number must be positive")
    return f"T{number:03d}"


def _bug_method_catalog(
    trigger_items: Sequence[tuple[str, str, str, Path]],
) -> tuple[
    List[Dict[str, str]], Dict[tuple[str, str, str], str], str
]:
    keys = set()
    for _, _, _, directory in trigger_items:
        execution = read_json(directory / "execution.json")
        validate_trace(execution, EXECUTION_SCHEMA)
        for invocation in execution["invocations"]:
            if int(invocation.get("invocation_id") or 0) <= 0:
                continue
            keys.add((
                str(invocation["class"]),
                str(invocation["method"]),
                str(invocation.get("descriptor") or ""),
            ))
    ordered = sorted(keys)
    method_ids = {
        key: f"M{index:03d}" for index, key in enumerate(ordered, 1)
    }
    catalog = [
        {
            "method_id": method_ids[(class_name, method, descriptor)],
            "function": f"{class_name}.{method}",
            "signature": f"{class_name}.{readable_signature(method, descriptor)}",
            "descriptor": descriptor,
        }
        for class_name, method, descriptor in ordered
    ]
    material = json.dumps(catalog, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return catalog, method_ids, fingerprint


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
) -> List[Dict[str, object]]:
    if trigger is not None:
        raise ValueError("UML generation requires all failing tests for each bug")
    trigger_items = list(layout.discover_triggers(projects, bugs))
    grouped: Dict[tuple[str, str], List[tuple[str, str, str, Path]]] = {}
    for item in trigger_items:
        grouped.setdefault((item[0], item[1]), []).append(item)
    catalogs: Dict[
        tuple[str, str],
        tuple[List[Dict[str, str]], Dict[tuple[str, str, str], str], str],
    ] = {}
    catalog_errors: Dict[tuple[str, str], Exception] = {}
    for key, items in grouped.items():
        try:
            catalogs[key] = _bug_method_catalog(items)
        except Exception as error:
            catalog_errors[key] = error

    rows = []
    for project, bug, number, directory in trigger_items:
        index_path = directory / "uml.json"
        error_log = layout.stage_log_dir("uml", project, bug, number) / "error.log"
        error_log.unlink(missing_ok=True)
        key = (project, bug)
        if key in catalog_errors:
            write_text(error_log, str(catalog_errors[key]) + "\n")
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "ERROR", "segment_count": 0})
            continue
        _, global_method_ids, catalog_fingerprint = catalogs[key]
        test_id = _test_id(number)
        if index_path.exists() and not force:
            try:
                existing_index = validate_uml_index(read_json(index_path), directory)
                if (
                    existing_index.get("schema_version") not in {3, 4}
                    or existing_index.get("test_id") != test_id
                    or existing_index.get("method_catalog_fingerprint")
                    != catalog_fingerprint
                ):
                    raise ValueError(
                        "existing UML graph does not match the bug-level image-only catalog; "
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
                    test_id, global_method_ids,
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
                "schema_version": 4,
                "test_id": test_id,
                "method_catalog_fingerprint": catalog_fingerprint,
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
                "rendering": {
                    "mode": "on_demand",
                    "format": "png",
                    "limit_size": limit_size,
                    "plantuml_command": plantuml_command,
                    "plantuml_jar": str(plantuml_jar) if plantuml_jar else None,
                },
                "entry_diagram_id": entry_diagram_id,
                "entry_reason": entry_reason,
                "node_count": len(nodes),
                "diagram_count": len(nodes),
                "nodes": nodes,
                "method_catalog": method_catalog,
            }
            validate_uml_index(index, directory)
            write_json(index_path, index)
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "OK", "segment_count": len(nodes)})
        except Exception as error:
            write_text(error_log, str(error) + "\n")
            rows.append({"project": project, "bug": bug, "trigger": number,
                         "status": "ERROR", "segment_count": 0})
    for (project, bug), items in grouped.items():
        bug_dir = layout.artifacts / project / f"bug_{bug}"
        suite_path = bug_dir / "uml_suite.json"
        try:
            if (project, bug) in catalog_errors:
                raise catalog_errors[(project, bug)]
            full_catalog, _, catalog_fingerprint = catalogs[(project, bug)]
            tests = []
            diagram_count = 0
            for _, _, number, directory in items:
                graph = validate_uml_index(read_json(directory / "uml.json"), directory)
                test_id = _test_id(number)
                if (
                    graph.get("schema_version") not in {3, 4}
                    or graph.get("test_id") != test_id
                    or graph.get("method_catalog_fingerprint") != catalog_fingerprint
                ):
                    raise ValueError(f"incompatible UML graph for {test_id}")
                test = (directory / "trigger_test.txt").read_text(
                    encoding="utf-8"
                ).strip()
                tests.append({
                    "test_id": test_id,
                    "test": test,
                    "trigger": int(number),
                    "entry_diagram_id": str(graph["entry_diagram_id"]),
                    "uml": (directory / "uml.json").relative_to(bug_dir).as_posix(),
                })
                diagram_count += int(graph["diagram_count"])
            suite = {
                "schema": "execution-uml-suite",
                "schema_version": 1,
                "project": project,
                "bug": bug,
                "method_catalog_fingerprint": catalog_fingerprint,
                "method_catalog": full_catalog,
                "test_count": len(tests),
                "diagram_count": diagram_count,
                "tests": tests,
            }
            validate_uml_suite(suite, bug_dir)
            write_json(suite_path, suite)
        except Exception as error:
            suite_path.unlink(missing_ok=True)
            write_text(
                layout.stage_log_dir("uml", project, bug) / "error.log",
                str(error) + "\n",
            )
            for row in rows:
                if row["project"] == project and row["bug"] == bug:
                    row["status"] = "ERROR"
                    row["segment_count"] = 0

    write_csv(layout.logs / "uml.csv", rows,
              ["project", "bug", "trigger", "status", "segment_count"])
    return rows
