import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dpex.domain.schemas import validate_localization_result
from dpex.infrastructure.failure_cache import (
    failure_cache_dir,
    load_failure_cache,
    validate_failure_cache,
)
from dpex.infrastructure.layout import RunLayout
from dpex.stages.localize import _localize_bug
from dpex.stages.refine.context import (
    build_localization_prompt,
    build_localization_system_prompt,
)
from dpex.stages.refine.shell import source_inspection_workspace


class FailureCacheTests(unittest.TestCase):
    def test_validated_cache_keeps_one_hashed_file_per_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = failure_cache_dir(root, "Chart", "1")
            cache.mkdir(parents=True)
            content = "Failing test: p.T::fails\n\nError stack:\nboom\n"
            report = cache / "T1.txt"
            report.write_text(content, encoding="utf-8")
            import hashlib
            manifest = {
                "schema": "failing-test-evidence-cache",
                "schema_version": 1,
                "project": "Chart",
                "bug": "1",
                "source_fingerprint": "a" * 64,
                "test_count": 1,
                "tests": [{
                    "test_id": "T1", "test": "p.T::fails", "file": "T1.txt",
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                }],
            }
            (cache / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(load_failure_cache(root, "Chart", "1"), manifest)
            report.write_text(content + "changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                validate_failure_cache(manifest, cache, "Chart", "1")

    def test_inspection_workspace_exposes_only_java_and_named_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "checkout"
            (workspace / "src").mkdir(parents=True)
            (workspace / "src/A.java").write_text("class A {}", encoding="utf-8")
            (workspace / "pom.xml").write_text("secret metadata", encoding="utf-8")
            report = root / "T1.txt"
            report.write_text("failure", encoding="utf-8")
            with source_inspection_workspace(
                workspace, {"failing-tests/T1.txt": report}
            ) as exposed:
                self.assertTrue((exposed / "src/A.java").is_file())
                self.assertTrue((exposed / "failing-tests/T1.txt").is_file())
                self.assertFalse((exposed / "pom.xml").exists())


class StandaloneLocalizationTests(unittest.TestCase):
    def test_dynamic_prompt_requires_runtime_verification_without_upstream_framing(self) -> None:
        prompt = build_localization_system_prompt(5, "dynamic-text")
        self.assertIn(
            "You are expected to use the available dynamic-evidence tools",
            prompt,
        )
        self.assertIn(
            "verify and strengthen your diagnosis", " ".join(prompt.split())
        )
        self.assertNotIn("before finalizing", prompt)
        self.assertNotIn("upstream", prompt.lower())
        self.assertNotIn(
            "upstream", build_localization_system_prompt(5, "bash-only").lower()
        )

    def test_prompt_lists_paths_without_inlining_failures(self) -> None:
        prompt = build_localization_prompt("Chart", "1", [{
            "test_id": "T1", "test": "p.T::fails",
            "failure_file": "failing-tests/T1.txt",
        }])
        self.assertIn("p.T::fails | failing-tests/T1.txt", prompt)
        self.assertNotIn("Error stack:", prompt)
        self.assertNotIn("upstream", prompt.lower())

    @patch("dpex.stages.localize.run_agent")
    @patch("dpex.stages.localize.build_failure_cache")
    def test_localize_has_no_locator_input_and_supports_bash_only(
        self, build_cache, run_agent,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory) / "run")
            layout.ensure()
            workspace = layout.workspace_dir("P", "1")
            workspace.mkdir(parents=True)
            trace_dir = layout.artifacts / "P/bug_1"
            trace_dir.mkdir(parents=True)
            (trace_dir / "trace_suite.json").write_text("{}", encoding="utf-8")
            cache_root = Path(directory) / "cache"
            report_dir = failure_cache_dir(cache_root, "P", "1")
            report_dir.mkdir(parents=True)
            (report_dir / "T1.txt").write_text("failure", encoding="utf-8")
            build_cache.return_value = {
                "source_fingerprint": "a" * 64,
                "cache_hit": False,
                "tests": [{
                    "test_id": "T1", "test": "p.T::fails",
                    "file": "T1.txt", "sha256": "b" * 64,
                }],
            }
            run_agent.return_value = {
                "model": "test-model",
                "ranking": [{
                    "function": "p.A.run", "signature": "p.A.run()",
                    "source_file": "src/A.java", "start_line": 1, "end_line": 3,
                    "reason": "source evidence", "input_candidate_id": None,
                }],
                "tool_rounds": 1, "terminal_command_count": 1,
                "diagram_view_count": 0, "viewed_diagrams": [],
                "inspected_invocation_ids": [], "queried_methods": [],
                "request_count": 2, "usage": {},
                "finalization_attempt_count": 1,
                "final_length_retry_count": 0, "final_finish_reason": "stop",
            }
            result = _localize_bug(
                layout, layout, cache_root, "P", "1",
                {"dpex": {"agent_variant": "bash-only", "top_k": 1}},
                30, None, False, True, 6, 6, 10,
            )
            self.assertEqual(result["status"], "OK")
            args, kwargs = run_agent.call_args
            self.assertEqual(args[0]["dpex"]["agent_variant"], "bash-only")
            self.assertEqual(args[2], [])
            self.assertTrue(kwargs["standalone"])
            output = json.loads((layout.artifacts / "P/bug_1/localization.json").read_text())
            self.assertIs(validate_localization_result(output), output)
            self.assertNotIn("locator", output)
            self.assertEqual(output["tests"][0]["failure_file"], "failing-tests/T1.txt")

    @patch("dpex.stages.localize.MethodExecutionGraphs")
    @patch("dpex.stages.localize.run_agent")
    @patch("dpex.stages.localize.build_failure_cache")
    def test_default_standalone_mode_is_dynamic_text(
        self, build_cache, run_agent, graph_class,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory) / "run")
            layout.ensure()
            workspace = layout.workspace_dir("P", "1")
            workspace.mkdir(parents=True)
            trace_dir = layout.artifacts / "P/bug_1"
            trace_dir.mkdir(parents=True)
            (trace_dir / "trace_suite.json").write_text("{}", encoding="utf-8")
            cache_root = Path(directory) / "cache"
            reports = failure_cache_dir(cache_root, "P", "1")
            reports.mkdir(parents=True)
            (reports / "T1.txt").write_text("failure", encoding="utf-8")
            build_cache.return_value = {
                "source_fingerprint": "a" * 64, "cache_hit": True,
                "tests": [{
                    "test_id": "T1", "test": "p.T::fails", "file": "T1.txt",
                    "sha256": "b" * 64,
                }],
            }
            run_agent.return_value = {
                "model": "test-model",
                "ranking": [{
                    "function": "p.A.run", "signature": "p.A.run()",
                    "source_file": "src/A.java", "start_line": 1, "end_line": 3,
                    "reason": "dynamic evidence", "input_candidate_id": None,
                }],
                "tool_rounds": 2, "terminal_command_count": 1,
                "diagram_view_count": 0, "viewed_diagrams": [],
                "inspected_invocation_ids": ["T1-C1"],
                "queried_methods": [{"name": "run", "line": "src/A.java:1"}],
                "request_count": 3, "usage": {},
                "finalization_attempt_count": 1,
                "final_length_retry_count": 0, "final_finish_reason": "stop",
            }
            result = _localize_bug(
                layout, layout, cache_root, "P", "1", {"dpex": {"top_k": 1}},
                30, None, False, True, 6, 6, 10,
            )
            self.assertEqual(result["status"], "OK")
            self.assertIs(run_agent.call_args.args[4], graph_class.return_value)
            output = json.loads((trace_dir / "localization.json").read_text())
            self.assertEqual(output["agent_variant"], "dynamic-text")
            self.assertEqual(output["prompt_version"], "localization-method-lines-dynamic-v1")


if __name__ == "__main__":
    unittest.main()
