import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dpex.domain.evaluation import evaluate_location_ranking, mean_metrics
from dpex.domain.schemas import validate_evaluation
from dpex.infrastructure.io import read_json, write_json
from dpex.infrastructure.layout import RunLayout
from dpex.stages import evaluate


class RefinementEvaluationTests(unittest.TestCase):
    def test_evaluation_discovers_standalone_localization(self):
        with tempfile.TemporaryDirectory() as temp:
            layout = RunLayout(Path(temp))
            layout.ensure()
            bug_dir = layout.artifacts / "Chart/bug_1"
            bug_dir.mkdir(parents=True)
            (bug_dir / "localization.json").write_text("{}", encoding="utf-8")
            targets = evaluate._targets(layout, ["Chart"], None)
            self.assertEqual(targets, [
                ("Chart", "1", bug_dir / "localization.json")
            ])

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
            layout = RunLayout(Path(temp) / "run-one")
            layout.ensure()
            cache_root = Path(temp) / "shared-cache"
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
            d4j_home = Path(temp) / "defects4j"
            patch_path = (
                d4j_home
                / "framework/projects/Chart/patches/1.src.patch"
            )
            patch_path.parent.mkdir(parents=True)
            patch_path.write_text("synthetic patch input")
            truth = [{
                "function": "p.A.f",
                "source_file": "src/p/A.java",
                "start_line": 10,
                "end_line": 15,
            }]
            with patch.object(
                evaluate, "ground_truth_locations", return_value=truth
            ) as ground_truth:
                rows = evaluate.run(
                    layout, ["Chart"], {"1"}, d4j_home, cache_root=cache_root
                )
                self.assertTrue((bug_dir / "traces/intermediate.bin").is_file())
                self.assertTrue(workspace.is_dir())
                cache_path = (
                    cache_root
                    / "artifacts/Chart/bug_1/evaluation_ground_truth.json"
                )
                self.assertTrue(cache_path.is_file())
                second_layout = RunLayout(Path(temp) / "run-two")
                second_layout.ensure()
                second_bug_dir = second_layout.artifacts / "Chart" / "bug_1"
                second_bug_dir.mkdir(parents=True)
                write_json(
                    second_bug_dir / "refinement.json",
                    read_json(bug_dir / "refinement.json"),
                )
                with patch.object(
                    evaluate,
                    "temporary_checkout",
                    side_effect=AssertionError("cache hit must avoid checkout"),
                ):
                    shared_rows = evaluate.run(
                        second_layout,
                        ["Chart"],
                        {"1"},
                        d4j_home,
                        cache_root=cache_root,
                    )
                    final_rows = evaluate.run(
                        layout,
                        ["Chart"],
                        {"1"},
                        d4j_home,
                        final_only=True,
                        cache_root=cache_root,
                    )
            ground_truth.assert_called_once()
            self.assertEqual(rows[0]["status"], "OK")
            self.assertEqual(rows[0]["ground_truth_cache_hit"], 0)
            self.assertEqual(shared_rows[0]["status"], "OK")
            self.assertEqual(shared_rows[0]["ground_truth_cache_hit"], 1)
            self.assertEqual(final_rows[0]["status"], "OK")
            self.assertEqual(final_rows[0]["ground_truth_cache_hit"], 1)
            self.assertFalse((bug_dir / "traces").exists())
            self.assertFalse((bug_dir / "trace_suite.json").exists())
            self.assertFalse(workspace.exists())
            self.assertTrue((bug_dir / "refinement.json").is_file())
            self.assertTrue(cache_path.is_file())
            self.assertFalse((bug_dir / "evaluation_ground_truth.json").exists())
            report = validate_evaluation(
                read_json(layout.summaries / "evaluation.json")
            )
            self.assertEqual(report["evaluated_bug_count"], 1)
            self.assertEqual(mean_metrics([{"top_1": True, "top_3": True, "top_5": True, "reciprocal_rank": 1.0, "average_precision": 1.0}])["mrr"], 1.0)

    def test_corrupt_or_stale_ground_truth_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "evaluation_ground_truth.json"
            cache_path.write_text("not JSON")
            self.assertIsNone(
                evaluate._cached_ground_truth(cache_path, "Chart", "1", "a" * 64)
            )
            write_json(cache_path, {
                "schema": "evaluation-ground-truth-cache",
                "schema_version": 1,
                "project": "Chart",
                "bug": "1",
                "source_fingerprint": "b" * 64,
                "ground_truth_locations": [{
                    "function": "p.A.f",
                    "source_file": "src/p/A.java",
                    "start_line": 10,
                    "end_line": 15,
                }],
            })
            self.assertIsNone(
                evaluate._cached_ground_truth(cache_path, "Chart", "1", "a" * 64)
            )


if __name__ == "__main__":
    unittest.main()
