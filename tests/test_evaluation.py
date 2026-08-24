import json
import tempfile
import unittest
from pathlib import Path

from mllmfl.domain.evaluation import (
    evaluate_location_ranking,
    evaluate_ranking,
    mean_metrics,
)
from mllmfl.domain.models import Ranking
from mllmfl.domain.schemas import validate_aggregate, validate_evaluation
from mllmfl.infrastructure.ground_truth import (
    ground_truth_locations,
    ground_truth_methods,
    java_executables,
    parse_source_patch,
    reconstruct_fixed_source,
)
from mllmfl.infrastructure.method_location import resolve_method_location
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.stages import evaluate
from mllmfl.stages.localize import attach_source_locations


PATCH = """diff --git a/src/p/A.java b/src/p/A.java
--- a/src/p/A.java
+++ b/src/p/A.java
@@ -3,5 +3,5 @@ public class A {
     void helper() {}
     void broken() {
-        if (value == null) return;
+        if (value != null) return;
     }
 }
"""

SOURCE = """package p;
public class A {
    void helper() {}
    void broken() {
        if (value != null) return;
    }
}
"""

OVERLOAD_PATCH = """diff --git a/src/p/A.java b/src/p/A.java
--- a/src/p/A.java
+++ b/src/p/A.java
@@ -4,3 +4,3 @@ class A {
     void run(String value) {
-        sink(value);
+        sink(value.trim());
     }
"""

OVERLOAD_SOURCE = """package p;
class A {
    void run(int value) {}
    void run(String value) {
        sink(value.trim());
    }
}
"""


class GroundTruthTests(unittest.TestCase):
    def test_parses_buggy_side_changed_lines(self):
        files = parse_source_patch(PATCH)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].path, "src/p/A.java")
        self.assertEqual(files[0].fixed_changed_lines, frozenset({5}))
        self.assertEqual(files[0].buggy_changed_lines, frozenset({5}))
        fixed = reconstruct_fixed_source(SOURCE, files[0].hunks)
        self.assertIn("value == null", fixed)
        self.assertNotIn("value != null", fixed)

    def test_extracts_nested_methods_and_constructors(self):
        methods = java_executables(
            "package p; class A { A() {} class B { void run() {} } }"
        )
        self.assertEqual(
            [item.function for item in methods],
            ["p.A.<init>", "p.A$B.run"],
        )

    def test_maps_defects4j_patch_to_buggy_method(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch = root / "d4j/framework/projects/P/patches/1.src.patch"
            patch.parent.mkdir(parents=True)
            patch.write_text(PATCH, encoding="utf-8")
            source = root / "workspace/src/p/A.java"
            source.parent.mkdir(parents=True)
            source.write_text(SOURCE, encoding="utf-8")
            methods = ground_truth_methods(root / "d4j", root / "workspace", "P", "1")
            locations = ground_truth_locations(
                root / "d4j", root / "workspace", "P", "1"
            )
        self.assertEqual(methods, ["p.A.broken"])
        self.assertEqual(locations, [{
            "function": "p.A.broken", "source_file": "src/p/A.java",
            "start_line": 4, "end_line": 6,
        }])

    def test_resolves_overloaded_runtime_method_to_exact_buggy_range(self):
        source_text = """package p;
class A {
    void run(int value) {}
    void run(String value) {}
}
"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "src/p/A.java"
            source.parent.mkdir(parents=True)
            source.write_text(source_text, encoding="utf-8")
            integer = resolve_method_location(
                workspace, "p.A.run", descriptor="(I)V"
            )
            string = resolve_method_location(
                workspace, "p.A.run", descriptor="(Ljava/lang/String;)V"
            )
            ranking, dropped = attach_source_locations([
                Ranking(
                    "p.A.run", "p.A.run(String)", 1,
                    method_id="M002", descriptor="(Ljava/lang/String;)V",
                )
            ], workspace)
        self.assertEqual((integer.start_line, integer.end_line), (3, 3))
        self.assertEqual((string.start_line, string.end_line), (4, 4))
        self.assertEqual(dropped, [])
        self.assertEqual(
            (ranking[0].source_file, ranking[0].start_line, ranking[0].end_line),
            ("src/p/A.java", 4, 4),
        )
        unresolved, unresolved_ids = attach_source_locations([
            Ranking(
                "p.Missing.run", "p.Missing.run()", 1,
                method_id="M999", descriptor="()V",
            )
        ], Path("/definitely/missing/workspace"))
        self.assertEqual(unresolved, [])
        self.assertEqual(unresolved_ids, ["M999"])

    def test_ground_truth_patch_selects_one_overloaded_source_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch = root / "d4j/framework/projects/P/patches/1.src.patch"
            patch.parent.mkdir(parents=True)
            patch.write_text(OVERLOAD_PATCH, encoding="utf-8")
            source = root / "workspace/src/p/A.java"
            source.parent.mkdir(parents=True)
            source.write_text(OVERLOAD_SOURCE, encoding="utf-8")
            locations = ground_truth_locations(
                root / "d4j", root / "workspace", "P", "1"
            )
        self.assertEqual(locations, [{
            "function": "p.A.run", "source_file": "src/p/A.java",
            "start_line": 4, "end_line": 6,
        }])

    def test_descriptor_keeps_qualified_types_for_same_simple_name(self):
        source_text = """package p;
class A {
    void run(p1.Tick value) {}
    void run(p2.Tick value) {}
}
"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "src/p/A.java"
            source.parent.mkdir(parents=True)
            source.write_text(source_text, encoding="utf-8")
            first = resolve_method_location(
                workspace, "p.A.run", descriptor="(Lp1/Tick;)V"
            )
            second = resolve_method_location(
                workspace, "p.A.run", descriptor="(Lp2/Tick;)V"
            )
        self.assertEqual(first.start_line, 3)
        self.assertEqual(second.start_line, 4)


class MetricTests(unittest.TestCase):
    def test_computes_top_rr_and_average_precision_for_multiple_faults(self):
        metrics = evaluate_ranking(["p.A", "p.B", "p.C"], {"p.A", "p.C"})
        self.assertEqual(metrics["relevant_ranks"], [1, 3])
        self.assertTrue(metrics["top_1"])
        self.assertEqual(metrics["reciprocal_rank"], 1.0)
        self.assertEqual(metrics["average_precision"], 0.833333)

    def test_unreturned_ground_truth_methods_contribute_zero_to_ap(self):
        metrics = evaluate_ranking(["p.B", "p.C"], {"p.A", "p.C"})
        self.assertFalse(metrics["top_1"])
        self.assertTrue(metrics["top_3"])
        self.assertEqual(metrics["reciprocal_rank"], 0.5)
        self.assertEqual(metrics["average_precision"], 0.25)

    def test_empty_collection_has_zero_macro_metrics(self):
        self.assertEqual(mean_metrics([]), {
            "top_1": 0.0, "top_3": 0.0, "top_5": 0.0, "mrr": 0.0, "map": 0.0,
        })

    def test_source_ranges_distinguish_overloads_with_same_function(self):
        ranking = [
            {"function": "p.A.run", "source_file": "src/p/A.java",
             "start_line": 3, "end_line": 3},
            {"function": "p.A.run", "source_file": "src/p/A.java",
             "start_line": 4, "end_line": 4},
        ]
        metrics = evaluate_location_ranking(ranking, [ranking[1]])
        self.assertEqual(metrics["relevant_ranks"], [2])
        self.assertEqual(metrics["reciprocal_rank"], 0.5)
        self.assertEqual(metrics["average_precision"], 0.5)


class EvaluateStageTests(unittest.TestCase):
    def test_evaluates_aggregate_and_writes_versioned_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = RunLayout(root / "run")
            layout.ensure()
            source = layout.workspace_dir("P", "1") / "src/p/A.java"
            source.parent.mkdir(parents=True)
            source.write_text(SOURCE, encoding="utf-8")
            patch = root / "d4j/framework/projects/P/patches/1.src.patch"
            patch.parent.mkdir(parents=True)
            patch.write_text(PATCH, encoding="utf-8")
            summary = layout.summaries / "P/bug_1.json"
            summary.parent.mkdir(parents=True)
            summary.write_text(json.dumps({
                "schema": "fault-localization-aggregate",
                "schema_version": 1,
                "project": "P",
                "bug": "1",
                "top_k": 5,
                "valid_trigger_count": 1,
                "status_counts": {"OK": 1},
                "ranking": [
                    {"function": "p.Other.run", "rank": 1},
                    {"function": "p.A.broken", "rank": 2},
                ],
            }), encoding="utf-8")

            rows = evaluate.run(layout, ["P"], {"1"}, root / "d4j")
            output = json.loads((layout.summaries / "evaluation.json").read_text())

        self.assertEqual(rows[0]["status"], "OK")
        self.assertEqual(output["evaluated_bug_count"], 1)
        self.assertEqual(output["metrics"], {
            "top_1": 0.0, "top_3": 1.0, "top_5": 1.0, "mrr": 0.5, "map": 0.5,
        })
        self.assertEqual(output["bugs"][0]["ground_truth"], ["p.A.broken"])

    def test_skips_aggregate_without_valid_agent_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory))
            layout.ensure()
            summary = layout.summaries / "P/bug_1.json"
            summary.parent.mkdir(parents=True)
            summary.write_text(json.dumps({
                "schema": "fault-localization-aggregate",
                "schema_version": 1,
                "project": "P",
                "bug": "1",
                "top_k": 5,
                "valid_trigger_count": 0,
                "status_counts": {"DRY_RUN": 1},
                "ranking": [],
            }), encoding="utf-8")
            rows = evaluate.run(layout, ["P"], {"1"}, Path(directory) / "d4j")
        self.assertEqual(rows[0]["status"], "NO_VALID_RESULT")

    def test_evaluates_v2_aggregate_by_buggy_source_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = RunLayout(root / "run")
            layout.ensure()
            source = layout.workspace_dir("P", "1") / "src/p/A.java"
            source.parent.mkdir(parents=True)
            source.write_text(SOURCE, encoding="utf-8")
            patch = root / "d4j/framework/projects/P/patches/1.src.patch"
            patch.parent.mkdir(parents=True)
            patch.write_text(PATCH, encoding="utf-8")
            summary = layout.summaries / "P/bug_1.json"
            summary.parent.mkdir(parents=True)
            summary.write_text(json.dumps({
                "schema": "fault-localization-aggregate",
                "schema_version": 2,
                "project": "P", "bug": "1", "top_k": 5,
                "valid_trigger_count": 1, "status_counts": {"OK": 1},
                "ranking": [{
                    "function": "p.A.broken", "signature": "p.A.broken()",
                    "source_file": "src/p/A.java", "start_line": 4,
                    "end_line": 6, "rank": 1,
                }],
            }), encoding="utf-8")

            rows = evaluate.run(layout, ["P"], {"1"}, root / "d4j")
            output = json.loads((layout.summaries / "evaluation.json").read_text())

        self.assertEqual(rows[0]["status"], "OK")
        self.assertEqual(output["schema_version"], 2)
        self.assertEqual(output["bugs"][0]["identity_mode"], "source_range")
        self.assertEqual(output["bugs"][0]["relevant_ranks"], [1])
        self.assertEqual(output["metrics"]["top_1"], 1.0)

    def test_evaluates_bug_level_v5_without_aggregate_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = RunLayout(root / "run")
            layout.ensure()
            source = layout.workspace_dir("P", "1") / "src/p/A.java"
            source.parent.mkdir(parents=True)
            source.write_text(SOURCE, encoding="utf-8")
            patch = root / "d4j/framework/projects/P/patches/1.src.patch"
            patch.parent.mkdir(parents=True)
            patch.write_text(PATCH, encoding="utf-8")
            result = layout.artifacts / "P/bug_1/localization.json"
            result.parent.mkdir(parents=True)
            result.write_text(json.dumps({
                "schema": "fault-localization", "schema_version": 5,
                "project": "P", "bug": "1", "status": "OK", "model": "vision",
                "top_k": 5, "test_count": 1,
                "tests": [{
                    "test_id": "T001", "test": "p.Test::fails",
                    "entry_diagram_id": "T001-D001",
                }],
                "viewed_test_ids": ["T001"], "candidate_count": 1,
                "diagram_count": 1, "tool_rounds": 1, "diagram_view_count": 1,
                "viewed_diagrams": ["T001-D001"],
                "returned_method_ids": ["M001"],
                "dropped_invalid_method_ids": [],
                "dropped_unresolved_source_methods": [],
                "ranking": [{
                    "function": "p.A.broken", "signature": "p.A.broken()",
                    "source_file": "src/p/A.java", "start_line": 4,
                    "end_line": 6, "rank": 1,
                }],
            }), encoding="utf-8")

            rows = evaluate.run(layout, ["P"], {"1"}, root / "d4j")
            output = json.loads((layout.summaries / "evaluation.json").read_text())

        self.assertEqual(rows[0]["status"], "OK")
        self.assertEqual(output["bugs"][0]["identity_mode"], "source_range")
        self.assertEqual(output["metrics"]["top_1"], 1.0)

    def test_evaluation_schema_rejects_inconsistent_counts(self):
        value = {
            "schema": "fault-localization-evaluation",
            "schema_version": 1,
            "ground_truth_source": "Defects4J",
            "evaluated_bug_count": 1,
            "skipped_bug_count": 0,
            "metrics": {
                "top_1": 0.0, "top_3": 0.0, "top_5": 0.0, "mrr": 0.0, "map": 0.0,
            },
            "bugs": [],
        }
        with self.assertRaisesRegex(ValueError, "inconsistent evaluation bug counts"):
            validate_evaluation(value)

    def test_aggregate_schema_rejects_result_count_without_ranking(self):
        value = {
            "schema": "fault-localization-aggregate",
            "schema_version": 1,
            "project": "P",
            "bug": "1",
            "top_k": 5,
            "valid_trigger_count": 1,
            "status_counts": {"OK": 1},
            "ranking": [],
        }
        with self.assertRaisesRegex(ValueError, "inconsistent aggregate"):
            validate_aggregate(value)


if __name__ == "__main__":
    unittest.main()
