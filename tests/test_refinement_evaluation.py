import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mllmfl.domain.evaluation import evaluate_location_ranking, mean_metrics
from mllmfl.domain.schemas import validate_evaluation
from mllmfl.infrastructure.io import read_json, write_json
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.stages import evaluate


class RefinementEvaluationTests(unittest.TestCase):
    def test_location_metrics_distinguish_overloads(self):
        ranking = [
            {
                "function": "p.A.f",
                "source_file": "src/p/A.java",
                "start_line": 20,
                "end_line": 25,
            },
            {
                "function": "p.A.f",
                "source_file": "src/p/A.java",
                "start_line": 10,
                "end_line": 15,
            },
        ]
        metrics = evaluate_location_ranking(ranking, [ranking[1]])
        self.assertFalse(metrics["top_1"])
        self.assertTrue(metrics["top_3"])
        self.assertEqual(metrics["reciprocal_rank"], 0.5)

    def test_evaluate_consumes_only_refinement_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            layout = RunLayout(Path(temp))
            layout.ensure()
            bug_dir = layout.artifacts / "Chart" / "bug_1"
            bug_dir.mkdir(parents=True)
            write_json(bug_dir / "refinement.json", {
                "schema": "fault-localization-refinement",
                "schema_version": 5,
                "project": "Chart",
                "bug": "1",
                "status": "OK",
                "model": "test-model",
                "locator": {"name": "test-locator"},
                "top_k": 1,
                "input_fingerprint": "a" * 64,
                "configuration_fingerprint": "b" * 64,
                "suite_fingerprint": "c" * 64,
                "input_ranking": [{
                    "candidate_id": "L001",
                    "rank": 1,
                    "function": "p.A.f",
                    "signature": "p.A.f()",
                    "source_file": "src/p/A.java",
                    "start_line": 10,
                    "end_line": 15,
                    "reason": "input",
                }],
                "tests": [{"test_id": "T1", "test": "p.T::test"}],
                "test_count": 1,
                "ranking": [{
                    "rank": 1,
                    "function": "p.A.f",
                    "signature": "p.A.f()",
                    "source_file": "src/p/A.java",
                    "start_line": 10,
                    "end_line": 15,
                    "reason": "runtime evidence",
                    "candidate_id": "L001",
                    "original_rank": 1,
                }],
                "rejected_candidate_ids": [],
                "tool_rounds": 1,
                "diagram_view_count": 1,
                "terminal_command_count": 0,
                "final_retry_count": 0,
                "finalization_attempts": [{
                    "response_id": "response-1",
                    "max_tokens": 4096,
                    "finish_reason": "stop",
                    "content_empty": False,
                    "usage": {},
                }],
                "viewed_diagrams": ["T1-M1-C1-D1"],
                "inspected_candidate_ids": ["L001"],
                "candidate_runtime_method_ids": {"L001": "M1"},
                "inspected_invocation_ids": ["T1-C1"],
                "queried_methods": [{
                    "name": "f", "line": "src/p/A.java:10",
                }],
            })
            (bug_dir / "traces").mkdir()
            (bug_dir / "traces/intermediate.bin").write_bytes(b"trace")
            (bug_dir / "trace_suite.json").write_text("suite")
            workspace = layout.workspace_dir("Chart", "1")
            workspace.mkdir(parents=True)
            (workspace / "source.txt").write_text("source")
            truth = [{
                "function": "p.A.f",
                "source_file": "src/p/A.java",
                "start_line": 10,
                "end_line": 15,
            }]
            with patch.object(evaluate, "ground_truth_locations", return_value=truth):
                rows = evaluate.run(layout, ["Chart"], {"1"}, Path("/d4j"))
                self.assertTrue((bug_dir / "traces/intermediate.bin").is_file())
                self.assertTrue(workspace.is_dir())
                final_rows = evaluate.run(
                    layout, ["Chart"], {"1"}, Path("/d4j"), final_only=True
                )
            self.assertEqual(rows[0]["status"], "OK")
            self.assertEqual(final_rows[0]["status"], "OK")
            self.assertFalse((bug_dir / "traces").exists())
            self.assertFalse((bug_dir / "trace_suite.json").exists())
            self.assertFalse(workspace.exists())
            self.assertTrue((bug_dir / "refinement.json").is_file())
            report = validate_evaluation(
                read_json(layout.summaries / "evaluation.json")
            )
            self.assertEqual(report["evaluated_bug_count"], 1)
            self.assertEqual(mean_metrics([{"top_1": True, "top_3": True, "top_5": True, "reciprocal_rank": 1.0, "average_precision": 1.0}])["mrr"], 1.0)


if __name__ == "__main__":
    unittest.main()
