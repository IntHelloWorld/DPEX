import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dpex.infrastructure.checkouts import remove_checkout, temporary_checkout
from dpex.infrastructure.layout import RunLayout
from dpex.infrastructure.process import CommandResult
from dpex.stages import collect


class CollectTests(unittest.TestCase):
    def run_collection(self, root, *, tests=None, failed=False, cached=False, corrupt=False):
        layout = RunLayout(Path(root))
        layout.ensure()
        workspace = layout.workspace_dir("Chart", "1")
        workspace.mkdir()
        (workspace / "source.java").write_text("source")
        jar = Path(root) / "agent.jar"
        jar.touch()
        capture = {
            "capture_values": True,
            "value_string_edge_chars": 10,
            "value_container_edge_items": 2,
            "value_nested_container_edge_items": 1,
            "value_max_depth": 2,
            "value_max_arguments": 8,
        }
        config = {**capture, "degradation_raw_size_bytes": 1024}
        target = (
            "Chart", "1", "1", Path(root), "p.T::test",
            {"trace_config": config},
        )
        recovery = [[target]] if cached else [[], [target]]
        with (
            patch.object(collect, "ensure_defects4j"),
            patch.object(collect, "checkout", return_value=CommandResult(0, "", "")) as checkout,
            patch.object(collect, "compile_project", return_value=CommandResult(0, "", "")),
            patch.object(collect, "trigger_tests", return_value=tests if tests is not None else ["p.T::test", "p.T::test"]),
            patch("dpex.stages.trace.load_trace_configuration", return_value=config),
            patch("dpex.stages.trace._suite_only_targets", side_effect=ValueError("bad JSON") if corrupt else recovery),
            patch("dpex.stages.trace.run", return_value=[{"status": "ERROR" if failed else "OK"}]) as trace,
        ):
            rows = collect.run(layout, ["Chart", "Chart"], {"1"}, None, None, 5,
                               agent_jar=jar, config_path=Path(root) / "config.json")
        return rows, workspace, checkout, trace

    def test_success_deduplicates_triggers_and_removes_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            rows, workspace, checkout, trace = self.run_collection(root)
            self.assertEqual(rows[0]["status"], "OK")
            self.assertEqual(rows[0]["trigger_count"], 1)
            self.assertFalse(workspace.exists())
            checkout.assert_called_once()
            trace.assert_called_once()

    def test_failed_trace_and_empty_export_retain_checkout(self):
        for kwargs in ({"failed": True}, {"tests": []}, {"corrupt": True}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as root:
                rows, workspace, _, _ = self.run_collection(root, **kwargs)
                self.assertEqual(rows[0]["status"], "ERROR")
                self.assertTrue(workspace.exists())

    def test_cached_suite_needs_no_checkout_or_execution(self):
        with tempfile.TemporaryDirectory() as root:
            rows, workspace, checkout, trace = self.run_collection(root, cached=True)
            self.assertEqual(rows[0]["status"], "SKIPPED")
            self.assertFalse(workspace.exists())
            checkout.assert_not_called()
            trace.assert_not_called()

    def test_cleanup_rejects_symlink_and_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            layout = RunLayout(Path(root))
            layout.ensure()
            target = Path(root) / "precious"
            target.mkdir()
            layout.workspace_dir("Chart", "1").symlink_to(target)
            with self.assertRaises(ValueError):
                remove_checkout(layout, "Chart", "1")
            with self.assertRaises(ValueError):
                remove_checkout(layout, "../Chart", "1")
            self.assertTrue(target.is_dir())

    def test_temporary_checkout_removed_on_consumer_failure(self):
        with tempfile.TemporaryDirectory() as root:
            layout = RunLayout(Path(root))
            def restore(project, bug, workspace, env, timeout):
                workspace.mkdir(parents=True)
                return CommandResult(0, "", "")
            with patch("dpex.infrastructure.checkouts.checkout", side_effect=restore):
                with self.assertRaisesRegex(ValueError, "consumer"):
                    with temporary_checkout(layout, "Chart", "1") as workspace:
                        self.assertTrue(workspace.is_dir())
                        raise ValueError("consumer")
            self.assertFalse(workspace.exists())

    def test_single_execution_supplies_output_and_rejects_timeout(self):
        from dpex.stages import trace
        config = {
            "capture_values": True, "value_string_edge_chars": 10,
            "value_container_edge_items": 2, "value_nested_container_edge_items": 1,
            "value_max_depth": 2, "value_max_arguments": 8,
            "retry_without_values_on_timeout": False,
        }
        for code in (1, 124):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                output = Path(root) / "output"
                with (
                    patch.object(trace, "find_java_file", return_value=None),
                    patch.object(trace, "build_classpath", return_value="classes"),
                    patch.object(trace, "classpath_has_class", return_value=True),
                    patch.object(trace, "run_command_with_zstd_fifo", return_value=CommandResult(
                        code, "java.lang.AssertionError: current execution", "stderr"
                    )) as execute,
                    patch.object(trace, "validate_defect_context", side_effect=RuntimeError("captured context")) as validate,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "captured context" if code == 1 else "timed out",
                    ):
                        trace.trace_trigger(Path(root), output, "p.T::test", "Chart",
                                            Path(root) / "agent.jar", {}, 5,
                                            capture_config=config)
                execute.assert_called_once()
                self.assertIn("fltrace.runner.SingleTestRunner", execute.call_args.args[0])
                if code == 1:
                    self.assertEqual(validate.call_args.args[0]["test_output"],
                                     "java.lang.AssertionError: current execution\nstderr")
                else:
                    validate.assert_not_called()

    def test_capture_timeout_retries_once_without_values_and_forces_degraded(self):
        from dpex.stages import trace
        config = {
            "capture_values": True, "value_string_edge_chars": 10,
            "value_container_edge_items": 2, "value_nested_container_edge_items": 1,
            "value_max_depth": 2, "value_max_arguments": 8,
            "degradation_raw_size_bytes": 1024,
            "retry_without_values_on_timeout": True,
        }
        calls = []
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "output"
            log_dir = Path(root) / "logs"
            output.mkdir()
            log_dir.mkdir()

            def capture_once(*args, **kwargs):
                calls.append((args[8]["capture_values"], kwargs.get("force_degraded", False)))
                if len(calls) == 1:
                    (output / "raw_events.jsonl.zst").write_bytes(b"first")
                    (log_dir / "trace.stderr.log").write_text("[TIMEOUT] 5s\n")
                    raise trace.TraceCaptureTimeout("timeout")
                return {"call_count": 7, "storage_mode": "degraded"}

            with patch.object(trace, "_trace_trigger_once", side_effect=capture_once):
                result = trace.trace_trigger(
                    Path(root), output, "p.T::test", "Chart",
                    Path(root) / "agent.jar", {}, 5, log_dir, config,
                )
            self.assertEqual(result["call_count"], 7)
            self.assertEqual(calls, [(True, False), (False, True)])
            self.assertTrue((
                output / "attempts/attempt_1_values_timeout/raw_events.jsonl.zst"
            ).is_file())
            provenance = trace.read_json(output / trace.TRACE_PROVENANCE_NAME)
            self.assertEqual(provenance["capture_attempt_count"], 2)
            self.assertEqual(provenance["fallback_reason"], "capture_timeout")
            self.assertFalse(provenance["effective_capture"]["capture_values"])

    def test_second_capture_timeout_is_not_retried(self):
        from dpex.stages import trace
        config = {
            "capture_values": True, "value_string_edge_chars": 10,
            "value_container_edge_items": 2, "value_nested_container_edge_items": 1,
            "value_max_depth": 2, "value_max_arguments": 8,
            "degradation_raw_size_bytes": 1024,
            "retry_without_values_on_timeout": True,
        }
        with tempfile.TemporaryDirectory() as root, patch.object(
            trace,
            "_trace_trigger_once",
            side_effect=trace.TraceCaptureTimeout("timeout"),
        ) as capture:
            with self.assertRaisesRegex(trace.TraceCaptureTimeout, "timeout"):
                trace.trace_trigger(
                    Path(root), Path(root) / "output", "p.T::test", "Chart",
                    Path(root) / "agent.jar", {}, 5,
                    Path(root) / "logs", config,
                )
            self.assertEqual(capture.call_count, 2)
            self.assertTrue((
                Path(root)
                / "output/attempts/attempt_2_no_values_timeout"
            ).is_dir())
