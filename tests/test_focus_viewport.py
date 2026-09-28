import json
import shutil
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dpex.domain.assertion_folding import fold_successful_assertions
from dpex.domain.focus_viewport import plan_focus_viewport
from dpex.domain.refinement_trace import (
    RefinementTraceTopology,
    build_method_catalog,
    build_refinement_trace,
)
from dpex.infrastructure.io import write_zstd_json
from dpex.infrastructure.trace_store import (
    METHOD_SUMMARY_NAME,
    TRACE_STORE_NAME,
    execution_to_store,
    finalize_trace_store,
)
from dpex.stages.refine.graphs import MethodExecutionGraphs


GENERATED_ROOT = Path(__file__).resolve().parent / "generated/focus_viewport"
PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def tree(
    class_name: str,
    method: str,
    *children: dict[str, Any],
) -> dict[str, Any]:
    return {
        "class": class_name,
        "method": method,
        "children": list(children),
    }


def execution_from_tree(root: dict[str, Any]) -> dict[str, Any]:
    """Build a valid, deterministic execution trace from a compact call tree."""
    invocations: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    next_invocation_id = 1
    next_event_seq = 1

    def visit(spec: dict[str, Any], parent: dict[str, Any] | None) -> dict[str, Any]:
        nonlocal next_invocation_id, next_event_seq
        invocation_id = next_invocation_id
        next_invocation_id += 1
        enter_seq = next_event_seq
        next_event_seq += 1
        invocation = {
            "invocation_id": invocation_id,
            "parent_id": parent["invocation_id"] if parent else None,
            "class": spec["class"],
            "method": spec["method"],
            "descriptor": "()V",
            "thread_id": 1,
            "thread_name": "main",
            "enter_seq": enter_seq,
            "enter_ns": enter_seq * 100,
            # Repeated calls to the same method deliberately share a source line.
            "origin_test_line": 10 + sum(
                ord(value) for value in f"{spec['class']}.{spec['method']}"
            ) % 200,
            "exit_seq": 0,
            "exit_ns": 0,
            "exit_type": "RETURN",
            "duration_ns": 0,
            "exception_class": "",
            "message": "",
        }
        invocations.append(invocation)
        if parent is not None:
            calls.append({
                "caller": f"{parent['class']}.{parent['method']}",
                "callee": f"{invocation['class']}.{invocation['method']}",
                "caller_class": parent["class"],
                "callee_class": invocation["class"],
                "caller_method": parent["method"],
                "callee_method": invocation["method"],
                "caller_descriptor": "()V",
                "callee_descriptor": "()V",
                "parent_invocation_id": parent["invocation_id"],
                "invocation_id": invocation_id,
                "parent_chain": [],
                "thread_id": 1,
                "enter_seq": enter_seq,
                "exit_seq": 0,
                "exit_type": "RETURN",
                "origin_test_line": invocation["origin_test_line"],
            })
        for child in spec["children"]:
            visit(child, invocation)
        exit_seq = next_event_seq
        next_event_seq += 1
        invocation["exit_seq"] = exit_seq
        invocation["exit_ns"] = exit_seq * 100
        invocation["duration_ns"] = (exit_seq - enter_seq) * 100
        if parent is not None:
            call = next(
                value for value in calls
                if value["invocation_id"] == invocation_id
            )
            call["exit_seq"] = exit_seq
        return invocation

    visit(root, None)
    calls.sort(key=lambda item: (item["enter_seq"], item["invocation_id"]))
    return {
        "schema": "fullchain-execution",
        "schema_version": 3,
        "test": {"class": root["class"], "method": root["method"]},
        "original_call_count": len(calls),
        "filtered_call_count": len(calls),
        "call_count": len(calls),
        "test_start": None,
        "test_end": None,
        "test_failures": [],
        "invocations": invocations,
        "calls": calls,
    }


def invocation_id(execution: dict[str, Any], method: str) -> int:
    matches = [
        int(item["invocation_id"])
        for item in execution["invocations"]
        if item["method"] == method
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one invocation for {method}, found {matches}")
    return matches[0]


def normalized_trace(
    execution: dict[str, Any], *, test_id: str = "T1", test: str = "p.T::test",
    project: str = "ViewportFixtures",
) -> tuple[dict[str, Any], list[dict[str, str]], str]:
    pruned, folding = fold_successful_assertions(execution)
    catalog, method_ids, fingerprint = build_method_catalog([pruned])
    trace = build_refinement_trace(
        pruned,
        project=project,
        test_id=test_id,
        test=test,
        method_ids=method_ids,
        catalog_fingerprint=fingerprint,
        assertion_folding=folding,
        error_stack="",
        test_output="",
    )
    return trace, catalog, fingerprint


def topology(execution: dict[str, Any]) -> RefinementTraceTopology:
    trace, _, _ = normalized_trace(execution)
    return RefinementTraceTopology.build(trace)


def write_fixture_sources(
    workspace: Path, execution: dict[str, Any]
) -> dict[str, str]:
    methods: dict[str, list[str]] = {}
    for invocation in execution["invocations"]:
        class_name = str(invocation["class"])
        method = str(invocation["method"])
        methods.setdefault(class_name, [])
        if method not in methods[class_name]:
            methods[class_name].append(method)
    references = {}
    for class_name, names in methods.items():
        package, simple_name = class_name.rsplit(".", 1)
        path = workspace / Path(*package.split(".")) / f"{simple_name}.java"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"package {package};", f"class {simple_name} {{"]
        for name in names:
            lines.append(f"  void {name}() {{}}")
            references[f"{class_name}.{name}()"] = (
                f"{path.relative_to(workspace).as_posix()}:{len(lines)}"
            )
        lines.append("}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return references


class FocusViewportSplitTests(unittest.TestCase):
    maxDiff = None

    def _make_graphs(
        self,
        scenario: str,
        call_tree: dict[str, Any],
        *,
        max_upstream_calls: int = 8,
        max_downstream_calls: int = 8,
        max_internal_calls: int = 8,
        execution_mutator: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[MethodExecutionGraphs, dict[str, Any]]:
        case_dir = GENERATED_ROOT / scenario
        if case_dir.exists():
            shutil.rmtree(case_dir)
        trace_dir = case_dir / "traces"
        trace_dir.mkdir(parents=True)
        execution = execution_from_tree(call_tree)
        if execution_mutator is not None:
            execution_mutator(execution)
        workspace = case_dir / "workspace"
        references = write_fixture_sources(workspace, execution)
        test = f"{call_tree['class']}::{call_tree['method']}"
        pruned, folding = fold_successful_assertions(execution)
        pruned["project"] = "ViewportFixtures"
        catalog, method_ids, fingerprint = build_method_catalog([pruned])
        conversion = case_dir / "conversion"
        conversion.mkdir()
        execution_to_store(
            pruned, conversion / TRACE_STORE_NAME,
            conversion / METHOD_SUMMARY_NAME, test=test,
            assertion_folding=folding,
            defect_context={"error_stack": "", "test_output": ""},
        )
        trace_path = trace_dir / "T1.trace.sqlite3"
        trace_fingerprint = finalize_trace_store(
            conversion / TRACE_STORE_NAME, trace_path,
            project="ViewportFixtures", test_id="T1", test=test,
            method_ids=method_ids, catalog_fingerprint=fingerprint,
        )
        (case_dir / "trace_suite.json").write_text(
            json.dumps({
                "schema": "execution-trace-suite",
                "schema_version": 3,
                "project": "ViewportFixtures",
                "bug": scenario,
                "method_catalog_fingerprint": fingerprint,
                "method_catalog": catalog,
                "test_count": 1,
                "tests": [{
                    "test_id": "T1",
                    "test": test,
                    "trigger": 1,
                    "trace": "traces/T1.trace.sqlite3",
                    "trace_fingerprint": trace_fingerprint,
                }],
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        graphs = MethodExecutionGraphs(
            case_dir,
            {"uml": {
                "plantuml_jar": str(
                    Path(__file__).resolve().parents[1] / "lib/plantuml.jar"
                ),
                "plantuml_limit_size": 32768,
            }},
            30,
            max_upstream_calls=max_upstream_calls,
            max_downstream_calls=max_downstream_calls,
            max_internal_calls=max_internal_calls,
            workspace=workspace,
        )
        graphs._fixture_source_references = references
        return graphs, execution

    def test_agent_focus_viewport_renders_captured_values(self):
        def add_values(execution):
            config = {
                "capture_values": True,
                "value_max_chars": 120,
                "value_max_items": 8,
                "value_max_depth": 2,
                "value_max_arguments_chars": 480,
            }
            execution["schema_version"] = 4
            execution["test_start"] = {
                "agent_protocol_version": 4,
                "value_capture": config,
            }
            by_id = {
                int(item["invocation_id"]): item
                for item in execution["invocations"]
            }
            for invocation in execution["invocations"]:
                if invocation["method"] == "focus":
                    invocation["descriptor"] = "(ILp/Model;)Lp/Result;"
                    invocation["arguments"] = {
                        "count": 2,
                        "items": [
                            {
                                "index": 0,
                                "declared_type": "int",
                                "runtime_type": "java.lang.Integer",
                                "kind": "number",
                                "text": "7",
                                "truncated": False,
                            },
                            {
                                "index": 1,
                                "declared_type": "p.Model",
                                "runtime_type": "p.impl.ModelImpl",
                                "kind": "object",
                                "text": "<p.impl.ModelImpl>",
                                "truncated": False,
                            },
                        ],
                        "omitted_count": 0,
                        "truncated": False,
                    }
                    invocation["return_value"] = {
                        "declared_type": "p.Result",
                        "runtime_type": "p.impl.ResultImpl",
                        "kind": "object",
                        "text": "<p.impl.ResultImpl>",
                        "truncated": False,
                    }
                else:
                    invocation["arguments"] = {
                        "count": 0,
                        "items": [],
                        "omitted_count": 0,
                        "truncated": False,
                    }
                    invocation["return_value"] = {
                        "declared_type": "void",
                        "runtime_type": "",
                        "kind": "void",
                        "text": "",
                        "truncated": False,
                    }
            for call in execution["calls"]:
                call["caller_descriptor"] = by_id[
                    int(call["parent_invocation_id"])
                ]["descriptor"]
                call["callee_descriptor"] = by_id[
                    int(call["invocation_id"])
                ]["descriptor"]

        graphs, execution = self._make_graphs(
            "captured_values",
            tree("p.RootTest", "fails", tree("p.Service", "focus")),
            execution_mutator=add_values,
        )
        focus_id = invocation_id(execution, "focus")
        result, image = graphs.inspect({"invocation_id": f"T1-C{focus_id}"})

        self.assertTrue(result["ok"], result)
        self.assertIsNotNone(image)
        assert image is not None
        self.assertEqual(image.read_bytes()[:8], PNG_HEADER)
        diagram_id = graphs.viewed_diagram_id(f"T1-C{focus_id}")
        node = graphs._nodes[diagram_id]
        puml = (
            graphs._node_roots[diagram_id] / str(node["puml"])
        ).read_text(encoding="utf-8")
        self.assertIn(f"T1-C{focus_id} focus(int, Model)", puml)
        self.assertIn("args=[7, <ModelImpl>]", puml)
        self.assertIn("return value=<ResultImpl>", puml)
        self.assertNotIn("-[#C62828]>", puml)
        self.assertNotIn("<color:#B71C1C>", puml)
        self.assertNotIn("#FFCDD2", puml)
        self.assertNotIn("arg0=", puml)
        self.assertNotIn("p.impl.", puml)
        self.assertNotIn("note ", puml)

    def _render_all(
        self,
        scenario: str,
        graphs: MethodExecutionGraphs,
        focus_signature: str = "p.Service.focus()",
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        lookup = graphs.find_invocation_ids({
            "test_id": "T1",
            "name": focus_signature.rsplit(".", 1)[-1].split("(", 1)[0],
            "line": graphs._fixture_source_references[focus_signature],
        })
        self.assertTrue(lookup["ok"], lookup)
        selected_invocation_id = str(
            lookup["invocations"][0]["invocation_id"]
        )
        self.assertRegex(selected_invocation_id, r"^T1-C[1-9]\d*$")
        self.assertNotIn("-I", selected_invocation_id)
        first_result, first_image = graphs.inspect({
            "invocation_id": selected_invocation_id
        })
        self.assertTrue(first_result["ok"], first_result)
        self.assertNotIn("linked_diagram_ids", first_result)
        entry_id = graphs.viewed_diagram_id(selected_invocation_id)
        self.assertRegex(
            entry_id,
            r"^T1-M[1-9]\d*-C[1-9]\d*-D1$",
        )
        self.assertIsNotNone(first_image)
        assert first_image is not None
        self.assertTrue(first_image.is_file(), first_image)
        self.assertEqual(first_image.read_bytes()[:8], PNG_HEADER)
        self.assertTrue(
            first_image.resolve().is_relative_to(
                (GENERATED_ROOT / scenario).resolve()
            )
        )
        node = graphs._nodes[entry_id]
        self.assertLessEqual(
            int(node["visible_unit_count"]),
            1
            + graphs.max_upstream_calls
            + graphs.max_downstream_calls
            + graphs.max_internal_calls,
        )
        self.assertLessEqual(
            int(node["upstream_visible_call_count"]),
            graphs.max_upstream_calls,
        )
        self.assertLessEqual(
            int(node["downstream_visible_call_count"]),
            graphs.max_downstream_calls,
        )
        self.assertLessEqual(
            int(node["internal_visible_call_count"]),
            graphs.max_internal_calls,
        )
        puml = graphs._node_roots[entry_id] / node["puml"]
        self.assertTrue(puml.is_file(), puml)
        rendered = {
            entry_id: {"node": node, "image": first_image, "puml": puml}
        }

        manifest = {
            "scenario": scenario,
            "entry_diagram_id": entry_id,
            "diagram_count": len(rendered),
            "diagrams": [{
                "diagram_id": diagram_id,
                "image": value["image"].relative_to(
                    GENERATED_ROOT / scenario
                ).as_posix(),
                "puml": value["puml"].relative_to(
                    GENERATED_ROOT / scenario
                ).as_posix(),
                "visible_unit_count": value["node"]["visible_unit_count"],
                "participant_count": value["node"]["participant_count"],
                "upstream_visible_call_count": value["node"][
                    "upstream_visible_call_count"
                ],
                "downstream_visible_call_count": value["node"][
                    "downstream_visible_call_count"
                ],
                "internal_visible_call_count": value["node"][
                    "internal_visible_call_count"
                ],
                "structural_context_call_count": value["node"][
                    "structural_context_call_count"
                ],
                "method_signatures": value["node"]["method_signatures"],
                "fold_kinds": [
                    fold["kind"] for fold in value["node"]["folds"]
                ],
                "has_omitted_calls": value["node"]["has_omitted_calls"],
                "omitted_region_count": value["node"]["omitted_region_count"],
                "links": value["node"]["links"],
            } for diagram_id, value in sorted(rendered.items())],
        }
        (GENERATED_ROOT / scenario / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return entry_id, rendered

    def _assert_omission_regions_consistent(
        self,
        rendered: dict[str, dict[str, Any]],
    ) -> None:
        for value in rendered.values():
            node = value["node"]
            self.assertEqual(node["has_omitted_calls"], bool(node["folds"]))
            self.assertEqual(node["omitted_region_count"], len(node["folds"]))
            self.assertEqual(
                node["visible_represented_call_count"]
                + node["omitted_call_count"],
                node["represented_call_count"],
            )
            self.assertEqual(
                sum(fold["represented_call_count"] for fold in node["folds"]),
                node["omitted_call_count"],
            )

    def test_small_graph_fits_one_image_without_navigation(self):
        graphs, _ = self._make_graphs(
            "small_graph",
            tree(
                "p.RootTest", "fails",
                tree(
                    "p.Service", "focus",
                    tree("p.Helper", "first"),
                    tree("p.Helper", "second"),
                ),
            ),
        )

        entry_id, rendered = self._render_all("small_graph", graphs)

        self.assertEqual(set(rendered), {entry_id})
        entry = rendered[entry_id]["node"]
        self.assertEqual(entry["links"], [])
        self.assertEqual(entry["folds"], [])
        self.assertEqual(entry["upstream_visible_call_count"], 1)
        self.assertEqual(entry["downstream_visible_call_count"], 0)
        self.assertEqual(entry["internal_visible_call_count"], 2)
        self.assertEqual(entry["structural_context_call_count"], 1)
        puml = rendered[entry_id]["puml"].read_text(encoding="utf-8")
        self.assertIn("title Invocation ID: T1-C2", puml)
        self.assertIn("T1-C2 focus()", puml)
        self.assertNotRegex(puml, r"\b[TMCD]0\d")
        self._assert_omission_regions_consistent(rendered)

    def test_deep_chain_uses_one_omission_self_arrow(self):
        graphs, _ = self._make_graphs(
            "deep_chain",
            tree(
                "p.RootTest", "fails",
                tree(
                    "p.Service", "focus",
                    tree(
                        "p.Layer1", "one",
                        tree(
                            "p.Layer2", "two",
                            tree("p.Layer3", "three"),
                        ),
                    ),
                ),
            ),
            max_internal_calls=2,
        )

        entry_id, rendered = self._render_all("deep_chain", graphs)

        self.assertEqual(set(rendered), {entry_id})
        entry = rendered[entry_id]["node"]
        self.assertEqual(entry["links"], [])
        self.assertTrue(entry["has_omitted_calls"])
        self.assertEqual(entry["omitted_region_count"], 1)
        self.assertEqual([fold["kind"] for fold in entry["folds"]], [
            "OMITTED_CALLS"
        ])
        puml = rendered[entry_id]["puml"].read_text(encoding="utf-8")
        self.assertEqual(puml.count("... omit "), 1)
        self.assertNotIn("note ", puml)
        self.assertNotIn("Note", puml)
        self.assertNotIn("TO ", puml)
        self.assertNotIn("FROM ", puml)
        self.assertNotIn("VIEW ", puml)
        self._assert_omission_regions_consistent(rendered)

    def test_call_budget_marks_prefix_and_suffix_omissions(self):
        graphs, execution = self._make_graphs(
            "call_budget_siblings",
            tree(
                "p.RootTest", "fails",
                tree("p.Worker", "beforeOne"),
                tree("p.Worker", "beforeTwo"),
                tree("p.Service", "focus"),
                tree("p.Worker", "afterOne"),
                tree("p.Worker", "afterTwo"),
            ),
            max_upstream_calls=2,
            max_downstream_calls=1,
        )

        entry_id, rendered = self._render_all("call_budget_siblings", graphs)

        self.assertEqual(set(rendered), {entry_id})
        self.assertEqual(
            rendered[entry_id]["node"]["method_signatures"],
            [
                "p.Worker.beforeTwo()",
                "p.Service.focus()",
                "p.Worker.afterOne()",
            ],
        )
        self.assertEqual(
            rendered[entry_id]["node"]["upstream_visible_call_count"], 2
        )
        self.assertEqual(
            rendered[entry_id]["node"]["downstream_visible_call_count"], 1
        )
        self.assertEqual(rendered[entry_id]["node"]["links"], [])
        self.assertTrue(rendered[entry_id]["node"]["has_omitted_calls"])
        self.assertEqual(rendered[entry_id]["node"]["omitted_region_count"], 2)
        value = rendered[entry_id]
        puml = value["puml"].read_text(encoding="utf-8")
        self.assertEqual(puml.count("... omit "), 2)
        self.assertNotIn("note right of ", puml)
        self.assertNotIn("note left of ", puml)
        self.assertNotIn("VIEW ", puml)
        self.assertNotIn("    Padding 5", puml)
        self._assert_omission_regions_consistent(rendered)

    def test_call_budget_is_independent_of_participant_count(self):
        graphs, _ = self._make_graphs(
            "participant_independent_siblings",
            tree(
                "p.RootTest", "fails",
                tree("p.WorkerA", "before"),
                tree("p.Service", "focus"),
                tree("p.WorkerB", "afterB"),
                tree("p.WorkerC", "afterC"),
                tree("p.WorkerD", "afterD"),
            ),
            max_upstream_calls=3,
            max_downstream_calls=10,
        )

        entry_id, rendered = self._render_all(
            "participant_independent_siblings", graphs
        )

        self.assertEqual(set(rendered), {entry_id})
        self.assertEqual(
            rendered[entry_id]["node"]["downstream_visible_call_count"], 3
        )
        self.assertGreater(rendered[entry_id]["node"]["participant_count"], 3)
        self._assert_omission_regions_consistent(rendered)

    def test_context_search_moves_to_higher_level_after_siblings_are_exhausted(self):
        graphs, _ = self._make_graphs(
            "higher_level_context",
            tree(
                "p.RootTest", "fails",
                tree("p.Worker", "outerBefore"),
                tree(
                    "p.Controller", "controller",
                    tree("p.Service", "focus"),
                ),
                tree("p.Worker", "outerAfter"),
            ),
            max_upstream_calls=3,
            max_downstream_calls=1,
        )

        entry_id, rendered = self._render_all("higher_level_context", graphs)

        self.assertEqual(set(rendered), {entry_id})
        self.assertEqual(
            rendered[entry_id]["node"]["method_signatures"],
            [
                "p.Worker.outerBefore()",
                "p.Controller.controller()",
                "p.Service.focus()",
                "p.Worker.outerAfter()",
            ],
        )
        self.assertEqual(
            rendered[entry_id]["node"]["structural_context_call_count"], 2
        )
        self.assertEqual(
            rendered[entry_id]["node"]["upstream_visible_call_count"], 3
        )
        self._assert_omission_regions_consistent(rendered)

    def test_parent_chain_consumes_upstream_budget(self):
        current = tree("p.Service", "focus")
        for level in range(1, 13):
            current = tree(f"p.Layer{level}", f"level{level}", current)
        graphs, execution = self._make_graphs(
            "bounded_parent_chain",
            tree(
                "p.RootTest", "fails",
                current,
                tree("p.After", "after"),
            ),
            max_upstream_calls=6,
            max_downstream_calls=6,
            max_internal_calls=10,
        )

        entry_id, rendered = self._render_all(
            "bounded_parent_chain", graphs
        )

        node = rendered[entry_id]["node"]
        self.assertGreater(execution["call_count"], 6)
        self.assertEqual(node["upstream_visible_call_count"], 6)
        self.assertEqual(node["structural_context_call_count"], 6)
        self.assertEqual(node["downstream_visible_call_count"], 0)
        self.assertEqual(node["internal_visible_call_count"], 0)
        self.assertEqual(node["visible_unit_count"], 7)
        self.assertTrue(node["has_omitted_calls"])
        self.assertEqual(node["omitted_region_count"], 1)
        boundary_fold = next(
            fold for fold in node["folds"]
            if fold["scope"] == "OUTER_CONTEXT"
        )
        self.assertEqual(boundary_fold["anchor_invocation_id"], 8)
        self.assertEqual(boundary_fold["represented_call_count"], 7)
        self.assertEqual(boundary_fold["leading_call_count"], 6)
        self.assertEqual(boundary_fold["trailing_call_count"], 1)
        puml = rendered[entry_id]["puml"].read_text(encoding="utf-8")
        self.assertEqual(puml.count(" level"), 6)
        leading = "... omit 6 calls ..."
        trailing = "... omit 1 calls ..."
        self.assertEqual(puml.count(leading), 1)
        self.assertEqual(puml.count(trailing), 1)
        self.assertLess(puml.index(leading), puml.index(" level6()"))
        self.assertGreater(puml.index(trailing), puml.rindex(": return"))
        self._assert_omission_regions_consistent(rendered)

    def test_near_siblings_are_selected_before_higher_level_siblings(self):
        graphs, _ = self._make_graphs(
            "near_context_first",
            tree(
                "p.RootTest", "fails",
                tree("p.Worker", "outerBefore"),
                tree(
                    "p.Controller", "controller",
                    tree("p.Worker", "innerBefore"),
                    tree("p.Service", "focus"),
                    tree("p.Worker", "innerAfter"),
                ),
                tree("p.Worker", "outerAfter"),
            ),
            max_upstream_calls=2,
            max_downstream_calls=1,
        )

        entry_id, rendered = self._render_all("near_context_first", graphs)

        self.assertEqual(set(rendered), {entry_id})
        self.assertEqual(
            rendered[entry_id]["node"]["method_signatures"],
            [
                "p.Controller.controller()",
                "p.Worker.innerBefore()",
                "p.Service.focus()",
                "p.Worker.innerAfter()",
            ],
        )
        self.assertNotIn(
            "p.Worker.outerBefore()",
            rendered[entry_id]["node"]["method_signatures"],
        )
        self.assertNotIn(
            "p.Worker.outerAfter()",
            rendered[entry_id]["node"]["method_signatures"],
        )
        self.assertEqual(
            rendered[entry_id]["node"]["upstream_visible_call_count"], 2
        )
        self.assertTrue(rendered[entry_id]["node"]["has_omitted_calls"])
        self.assertEqual(rendered[entry_id]["node"]["omitted_region_count"], 1)

    def test_bounded_context_marks_unselected_nearby_calls(self):
        graphs, _ = self._make_graphs(
            "bounded_near_context_first",
            tree(
                "p.RootTest", "fails",
                tree("p.Outer", "outerBefore"),
                tree(
                    "p.Controller", "controller",
                    tree("p.WorkerA", "stepA"),
                    tree("p.WorkerB", "stepB"),
                    tree("p.WorkerA", "stepA"),
                    tree("p.WorkerB", "stepB"),
                    tree("p.Service", "focus"),
                ),
            ),
            max_upstream_calls=1,
        )

        entry_id, rendered = self._render_all(
            "bounded_near_context_first", graphs
        )

        node = rendered[entry_id]["node"]
        self.assertEqual(node["upstream_visible_call_count"], 1)
        self.assertNotIn("p.Outer.outerBefore()", node["method_signatures"])
        self.assertNotIn("p.WorkerA.stepA()", node["method_signatures"])
        self.assertNotIn("p.WorkerB.stepB()", node["method_signatures"])
        self.assertTrue(node["has_omitted_calls"])
        self.assertEqual(node["omitted_region_count"], 2)
        puml = rendered[entry_id]["puml"].read_text(encoding="utf-8")
        self.assertEqual(puml.count("... omit "), 2)
        self._assert_omission_regions_consistent(rendered)

    def test_internal_calls_are_selected_in_breadth_first_order(self):
        graphs, _ = self._make_graphs(
            "internal_bfs",
            tree(
                "p.RootTest", "fails",
                tree(
                    "p.Service", "focus",
                    tree(
                        "p.First", "first",
                        tree("p.FirstLeaf", "firstLeaf"),
                    ),
                    tree(
                        "p.Second", "second",
                        tree("p.SecondLeaf", "secondLeaf"),
                    ),
                ),
            ),
            max_internal_calls=2,
        )

        entry_id, rendered = self._render_all("internal_bfs", graphs)

        self.assertEqual(set(rendered), {entry_id})
        self.assertEqual(
            rendered[entry_id]["node"]["method_signatures"],
            ["p.Service.focus()", "p.First.first()", "p.Second.second()"],
        )
        self.assertTrue(rendered[entry_id]["node"]["has_omitted_calls"])
        self.assertEqual(rendered[entry_id]["node"]["omitted_region_count"], 2)
        self.assertTrue(all(
            fold["scope"] == "CHILDREN"
            for fold in rendered[entry_id]["node"]["folds"]
        ))

    def test_complex_graph_respects_three_six_call_windows(self):
        downstream_specs = [
            ("p.DownA", "alphaOne"),
            ("p.DownB", "betaOne"),
            ("p.DownC", "gammaOne"),
            ("p.DownA", "alphaTwo"),
            ("p.DownB", "betaTwo"),
            ("p.DownD", "delta"),
            ("p.DownE", "epsilon"),
            ("p.DownF", "zeta"),
            ("p.DownG", "eta"),
            ("p.DownH", "theta"),
            ("p.DownI", "iota"),
            ("p.DownJ", "kappa"),
            ("p.DownK", "lambda"),
        ]
        current = tree(
            "p.FocusService",
            "focus",
            *(
                tree(
                    class_name,
                    method,
                    tree(f"p.Leaf{index:02d}", f"leaf{index:02d}"),
                )
                for index, (class_name, method) in enumerate(
                    downstream_specs, 1
                )
            ),
        )
        upstream_classes = ("p.LayerA", "p.LayerB", "p.LayerC")
        for level in range(1, 10):
            current = tree(
                upstream_classes[(level - 1) % len(upstream_classes)],
                f"stage{level:02d}",
                tree(f"p.Before{level:02d}", f"before{level:02d}"),
                current,
                tree(f"p.After{level:02d}", f"after{level:02d}"),
            )
        graphs, execution = self._make_graphs(
            "complex_split_6x4",
            tree("p.RootTest", "fails", current),
            max_upstream_calls=6,
            max_downstream_calls=6,
            max_internal_calls=6,
        )

        entry_id, rendered = self._render_all(
            "complex_split_6x4",
            graphs,
            focus_signature="p.FocusService.focus()",
        )

        self.assertGreaterEqual(execution["call_count"], 50)
        self.assertEqual(set(rendered), {entry_id})
        node = rendered[entry_id]["node"]
        self.assertEqual(node["links"], [])
        self.assertEqual(node["upstream_visible_call_count"], 6)
        self.assertEqual(node["downstream_visible_call_count"], 3)
        self.assertEqual(node["internal_visible_call_count"], 6)
        self.assertTrue(node["has_omitted_calls"])
        self.assertGreater(node["omitted_region_count"], 0)
        self.assertTrue(all(
            fold["kind"] == "OMITTED_CALLS" for fold in node["folds"]
        ))
        self.assertGreaterEqual(node["participant_count"], 7)
        puml = rendered[entry_id]["puml"].read_text(encoding="utf-8")
        self.assertNotIn("note right of ", puml)
        self.assertNotIn("TO ", puml)
        self.assertNotIn("FROM ", puml)
        self.assertNotIn("VIEW ", puml)
        self.assertRegex(puml, r"\.\.\. omit \d+ calls \.\.\.")
        self._assert_omission_regions_consistent(rendered)

    def test_repeated_sequence_uses_nearest_raw_calls(self):
        call_tree = tree(
            "p.RootTest", "fails",
            tree("p.WorkerA", "stepA"),
            tree("p.WorkerB", "stepB"),
            tree("p.WorkerA", "stepA"),
            tree("p.WorkerB", "stepB"),
            tree("p.Service", "focus"),
        )
        graphs, execution = self._make_graphs(
            "repeated_sequence",
            call_tree,
            max_upstream_calls=3,
        )

        entry_id, rendered = self._render_all("repeated_sequence", graphs)

        self.assertEqual(set(rendered), {entry_id})
        sibling = rendered[entry_id]["node"]
        self.assertEqual(sibling["upstream_visible_call_count"], 3)
        self.assertEqual(sibling["visible_unit_count"], 4)
        self.assertEqual(sibling["omitted_region_count"], 1)
        self._assert_omission_regions_consistent(rendered)

    def test_repeated_parent_subtrees_are_not_compressed(self):
        graphs, execution = self._make_graphs(
            "repeated_parent_omission",
            tree(
                "p.RootTest", "fails",
                tree(
                    "p.Service", "focus",
                    tree(
                        "p.Worker", "work",
                        tree("p.Leaf", "leaf"),
                    ),
                    tree(
                        "p.Worker", "work",
                        tree("p.Leaf", "leaf"),
                    ),
                ),
            ),
            max_internal_calls=1,
        )

        entry_id, rendered = self._render_all(
            "repeated_parent_omission", graphs
        )

        node = rendered[entry_id]["node"]
        self.assertTrue(node["has_omitted_calls"])
        self.assertEqual(node["omitted_region_count"], 2)
        self.assertEqual(len(node["folds"]), 2)
        puml = rendered[entry_id]["puml"].read_text(encoding="utf-8")
        self.assertNotIn("work() ×2", puml)
        self.assertEqual(puml.count("... omit "), 2)
        self._assert_omission_regions_consistent(rendered)

    def test_repeated_sequence_budget_selects_nearest_raw_call(self):
        execution = execution_from_tree(tree(
            "p.RootTest", "fails",
            tree("p.WorkerA", "stepA"),
            tree("p.WorkerB", "stepB"),
            tree("p.WorkerA", "stepA"),
            tree("p.WorkerB", "stepB"),
            tree("p.Service", "focus"),
        ))
        focus_id = invocation_id(execution, "focus")
        plan = plan_focus_viewport(
            diagram_id="D1",
            focus_invocation_id=focus_id,
            topology=topology(execution),
            max_upstream_calls=2,
            max_downstream_calls=2,
            max_internal_calls=8,
        )

        self.assertEqual(plan["links"], [])
        self.assertEqual(plan["upstream_visible_call_count"], 2)
        self.assertTrue(plan["has_omitted_calls"])
        self.assertEqual(plan["omitted_region_count"], 1)
        self.assertEqual(len(plan["folds"]), 1)
        self.assertEqual(plan["folds"][0]["scope"], "CHILDREN")

    def test_disconnected_trace_root_is_explicitly_omitted(self):
        execution = execution_from_tree(tree(
            "p.RootTest", "fails",
            tree("p.Service", "focus"),
        ))
        execution["invocations"].extend([
            {
                "invocation_id": 3,
                "parent_id": None,
                "class": "p.AsyncRoot",
                "method": "run",
                "descriptor": "()V",
                "thread_id": 2,
                "thread_name": "worker",
                "enter_seq": 5,
                "enter_ns": 500,
                "origin_test_line": 0,
                "exit_seq": 8,
                "exit_ns": 800,
                "exit_type": "RETURN",
                "duration_ns": 300,
                "exception_class": "",
                "message": "",
            },
            {
                "invocation_id": 4,
                "parent_id": 3,
                "class": "p.AsyncWorker",
                "method": "tick",
                "descriptor": "()V",
                "thread_id": 2,
                "thread_name": "worker",
                "enter_seq": 6,
                "enter_ns": 600,
                "origin_test_line": 0,
                "exit_seq": 7,
                "exit_ns": 700,
                "exit_type": "RETURN",
                "duration_ns": 100,
                "exception_class": "",
                "message": "",
            },
            {
                "invocation_id": 5,
                "parent_id": None,
                "class": "p.SecondAsyncRoot",
                "method": "run",
                "descriptor": "()V",
                "thread_id": 3,
                "thread_name": "worker-2",
                "enter_seq": 9,
                "enter_ns": 900,
                "origin_test_line": 0,
                "exit_seq": 12,
                "exit_ns": 1200,
                "exit_type": "RETURN",
                "duration_ns": 300,
                "exception_class": "",
                "message": "",
            },
            {
                "invocation_id": 6,
                "parent_id": 5,
                "class": "p.SecondAsyncWorker",
                "method": "tick",
                "descriptor": "()V",
                "thread_id": 3,
                "thread_name": "worker-2",
                "enter_seq": 10,
                "enter_ns": 1000,
                "origin_test_line": 0,
                "exit_seq": 11,
                "exit_ns": 1100,
                "exit_type": "RETURN",
                "duration_ns": 100,
                "exception_class": "",
                "message": "",
            },
        ])
        execution["calls"].extend([{
            "caller": "p.AsyncRoot.run",
            "callee": "p.AsyncWorker.tick",
            "caller_class": "p.AsyncRoot",
            "callee_class": "p.AsyncWorker",
            "caller_method": "run",
            "callee_method": "tick",
            "caller_descriptor": "()V",
            "callee_descriptor": "()V",
            "parent_invocation_id": 3,
            "invocation_id": 4,
            "parent_chain": [],
            "thread_id": 2,
            "enter_seq": 6,
            "exit_seq": 7,
            "exit_type": "RETURN",
            "origin_test_line": 0,
        }, {
            "caller": "p.SecondAsyncRoot.run",
            "callee": "p.SecondAsyncWorker.tick",
            "caller_class": "p.SecondAsyncRoot",
            "callee_class": "p.SecondAsyncWorker",
            "caller_method": "run",
            "callee_method": "tick",
            "caller_descriptor": "()V",
            "callee_descriptor": "()V",
            "parent_invocation_id": 5,
            "invocation_id": 6,
            "parent_chain": [],
            "thread_id": 3,
            "enter_seq": 10,
            "exit_seq": 11,
            "exit_type": "RETURN",
            "origin_test_line": 0,
        }])
        execution["original_call_count"] = 3
        execution["filtered_call_count"] = 3
        execution["call_count"] = 3
        plan = plan_focus_viewport(
            diagram_id="D1",
            focus_invocation_id=2,
            topology=topology(execution),
            max_upstream_calls=1,
            max_downstream_calls=1,
            max_internal_calls=1,
        )

        self.assertTrue(plan["has_omitted_calls"])
        self.assertEqual(plan["omitted_region_count"], 1)
        self.assertEqual(len(plan["folds"]), 1)
        async_fold = plan["folds"][0]
        self.assertEqual(async_fold["anchor_invocation_id"], 1)
        self.assertEqual(async_fold["scope"], "OUTER_CONTEXT")
        self.assertNotIn("p.AsyncRoot", plan["participant_classes"])
        self.assertNotIn("p.SecondAsyncRoot", plan["participant_classes"])


if __name__ == "__main__":
    unittest.main()
