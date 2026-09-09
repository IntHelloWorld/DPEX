import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mllmfl.infrastructure.checkouts import remove_checkout, temporary_checkout
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.process import CommandResult
from mllmfl.stages import collect


class CollectTests(unittest.TestCase):
    def run_collection(self, root, *, tests=None, failed=False, cached=False, corrupt=False):
        layout = RunLayout(Path(root))
        layout.ensure()
        workspace = layout.workspace_dir("Chart", "1")
        workspace.mkdir()
        (workspace / "source.java").write_text("source")
        jar = Path(root) / "agent.jar"
        jar.touch()
        config = {"capture_values": True}
        target = ("Chart", "1", "1", Path(root), "p.T::test", {"capture": config})
        recovery = [[target]] if cached else [[], [target]]
        with (
            patch.object(collect, "ensure_defects4j"),
            patch.object(collect, "checkout", return_value=CommandResult(0, "", "")) as checkout,
            patch.object(collect, "compile_project", return_value=CommandResult(0, "", "")),
            patch.object(collect, "trigger_tests", return_value=tests if tests is not None else ["p.T::test", "p.T::test"]),
            patch("mllmfl.stages.trace.load_trace_configuration", return_value=config),
            patch("mllmfl.stages.trace._suite_only_targets", side_effect=ValueError("bad JSON") if corrupt else recovery),
            patch("mllmfl.stages.trace.run", return_value=[{"status": "ERROR" if failed else "OK"}]) as trace,
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
            with patch("mllmfl.infrastructure.checkouts.checkout", side_effect=restore):
                with self.assertRaisesRegex(ValueError, "consumer"):
                    with temporary_checkout(layout, "Chart", "1") as workspace:
                        self.assertTrue(workspace.is_dir())
                        raise ValueError("consumer")
            self.assertFalse(workspace.exists())

    def test_single_execution_supplies_output_and_rejects_timeout(self):
        from mllmfl.stages import trace
        config = {
            "capture_values": True, "value_string_edge_chars": 10,
            "value_container_edge_items": 2, "value_nested_container_edge_items": 1,
            "value_max_depth": 2, "value_max_arguments": 8,
        }
        for code in (1, 124):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                output = Path(root) / "output"
                with (
                    patch.object(trace, "find_java_file", return_value=None),
                    patch.object(trace, "build_classpath", return_value="classes"),
                    patch.object(trace, "classpath_has_class", return_value=True),
                    patch.object(trace, "run_command", return_value=CommandResult(
                        code, "java.lang.AssertionError: current execution", "stderr"
                    )) as execute,
                    patch.object(trace, "validate_defect_context", side_effect=RuntimeError("captured context")) as validate,
                ):
                    with self.assertRaisesRegex(RuntimeError, "captured context" if code == 1 else "124"):
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
