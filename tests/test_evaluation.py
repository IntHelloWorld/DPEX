import json
import tempfile
import unittest
from pathlib import Path

from mllmfl.domain.evaluation import evaluate_ranking, mean_metrics
from mllmfl.domain.schemas import validate_aggregate, validate_evaluation
from mllmfl.infrastructure.ground_truth import (
    ground_truth_methods,
    java_executables,
    parse_source_patch,
    reconstruct_fixed_source,
)
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.stages import evaluate


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
        self.assertEqual(methods, ["p.A.broken"])


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
