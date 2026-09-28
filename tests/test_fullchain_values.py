import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from dpex.domain.trace import (
    build_trace,
    load_events,
    project_execution,
    validate_trace,
)
from dpex.infrastructure.plantuml import DEFAULT_PLANTUML_JAR, ensure_rendered
from dpex.infrastructure.sequence_diagram import make_puml


ROOT = Path(__file__).resolve().parents[1]
AGENT_JAR = ROOT / "lib" / "fullchain-tracer.jar"
FIXTURES = ROOT / "tests" / "fixtures" / "fullchain_values"


@unittest.skipUnless(shutil.which("java") and shutil.which("javac"), "Java is required")
class FullchainValueCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.classes = Path(cls.temp.name) / "classes"
        cls.classes.mkdir()
        subprocess.run(
            [
                "javac", "-source", "8", "-target", "8", "-cp", str(AGENT_JAR),
                "-d", str(cls.classes),
                str(FIXTURES / "ValueWorkload.java"),
                str(FIXTURES / "ValueDriver.java"),
                str(FIXTURES / "StackOverflowWorkload.java"),
                str(FIXTURES / "StackOverflowDriver.java"),
                str(FIXTURES / "JUnit3SelectionTest.java"),
                str(FIXTURES / "ConstructorFailureWorkload.java"),
                str(FIXTURES / "ConstructorFailureDriver.java"),
                str(FIXTURES / "TraceGapDriver.java"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_workload(self, capture_values: bool):
        raw = Path(self.temp.name) / (
            "captured.jsonl" if capture_values else "uncaptured.jsonl"
        )
        raw.unlink(missing_ok=True)
        config = {
            "capture_values": capture_values,
            "value_string_edge_chars": 10,
            "value_container_edge_items": 2,
            "value_nested_container_edge_items": 1,
            "value_max_depth": 2,
            "value_max_arguments": 8,
        }
        command = [
            "java",
            f"-Dfltrace.raw.file={raw}",
            "-Dfltrace.test.class=valuefixture.ValueWorkload",
            "-Dfltrace.test.method=scenario",
            f"-Dfltrace.capture.values={str(capture_values).lower()}",
            "-Dfltrace.value.string.edge.chars=10",
            "-Dfltrace.value.container.edge.items=2",
            "-Dfltrace.value.nested.container.edge.items=1",
            "-Dfltrace.value.max.depth=2",
            "-Dfltrace.value.max.arguments=8",
            f"-javaagent:{AGENT_JAR}=class:valuefixture.ValueWorkload",
            "-cp", f"{self.classes}:{AGENT_JAR}",
            "valuefixture.ValueDriver",
        ]
        started = time.perf_counter()
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        elapsed = time.perf_counter() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT=", result.stdout)
        outcome = next(
            line for line in result.stdout.splitlines() if line.startswith("RESULT=")
        )
        events = load_events(raw)
        trace = build_trace(events)
        validate_trace(trace)
        return events, trace, elapsed, raw.stat().st_size, config, outcome

    def run_default_limit_workload(self):
        raw = Path(self.temp.name) / "default-limits.jsonl"
        raw.unlink(missing_ok=True)
        command = [
            "java",
            f"-Dfltrace.raw.file={raw}",
            "-Dfltrace.test.class=valuefixture.ValueWorkload",
            "-Dfltrace.test.method=scenario",
            f"-javaagent:{AGENT_JAR}=class:valuefixture.ValueWorkload",
            "-cp", f"{self.classes}:{AGENT_JAR}",
            "valuefixture.ValueDriver",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = build_trace(load_events(raw))
        validate_trace(trace)
        return trace

    def test_safe_value_protocol_and_statuses(self):
        events, trace, _, _, config, _ = self.run_workload(True)
        start = next(event for event in events if event["type"] == "TEST_START")
        self.assertEqual(start["agent_protocol_version"], 5)
        self.assertEqual(start["value_capture"], config)
        self.assertEqual(trace["schema_version"], 6)
        enters = {
            event["method"]: event
            for event in events if event["type"] == "ENTER"
        }
        exits = {
            event["invocation_id"]: event
            for event in events if event["type"] in {"RETURN", "THROW"}
        }
        scalar = enters["scalar"]["arguments"]
        self.assertEqual(scalar["count"], 6)
        self.assertEqual(scalar["items"][0]["text"], "3")
        self.assertIn("雪", scalar["items"][1]["text"])
        self.assertEqual(scalar["items"][4]["kind"], "null")
        self.assertEqual(scalar["items"][5]["kind"], "enum")
        self.assertTrue(enters["array"]["arguments"]["items"][0]["truncated"])
        self.assertIn("<cycle>", enters["containers"]["arguments"]["items"][0]["text"])
        self.assertEqual(enters["business"]["arguments"]["items"][0]["kind"], "object")
        self.assertEqual(
            enters["business"]["arguments"]["items"][0]["text"],
            "<valuefixture.ValueWorkload$Evil>",
        )
        self.assertEqual(
            enters["captureFailure"]["arguments"]["items"][0]["kind"], "error"
        )
        self.assertEqual(
            enters["mutate"]["arguments"]["items"][0]["text"], '["before-entry"]'
        )
        self.assertEqual(enters["many"]["arguments"]["omitted_count"], 1)
        constructor = next(
            event for event in events
            if event.get("type") == "ENTER" and event.get("method") == "<init>"
        )
        self.assertEqual(exits[constructor["invocation_id"]]["return_value"]["kind"], "void")
        noop = enters["noop"]
        self.assertEqual(exits[noop["invocation_id"]]["return_value"]["kind"], "void")
        boxed = enters["boxedVoid"]
        self.assertEqual(boxed["descriptor"], "()Ljava/lang/Void;")
        self.assertEqual(exits[boxed["invocation_id"]]["return_value"], {
            "declared_type": "java.lang.Void", "runtime_type": "",
            "kind": "null", "text": "null", "truncated": False,
        })
        explode = enters["explode"]
        self.assertEqual(exits[explode["invocation_id"]]["type"], "THROW")
        self.assertEqual(
            exits[explode["invocation_id"]]["message"], "expected\u0004control"
        )
        self.assertNotIn("return_value", exits[explode["invocation_id"]])
        scenario = enters["scenario"]
        self.assertEqual(exits[scenario["invocation_id"]]["return_value"]["kind"], "number")

    def test_capture_preserves_topology_and_records_bounded_values(self):
        _, captured, captured_seconds, captured_size, _, captured_outcome = (
            self.run_workload(True)
        )
        _, plain, plain_seconds, plain_size, _, plain_outcome = self.run_workload(False)
        self.assertEqual(captured_outcome, plain_outcome)
        topology = lambda trace: [
            (
                item["class"], item["method"], item["descriptor"], item["parent_id"],
                item["exit_type"],
            )
            for item in trace["invocations"]
        ]
        self.assertEqual(topology(captured), topology(plain))
        repeat_arguments = [
            item["arguments"]["items"][0]["text"]
            for item in captured["invocations"] if item["method"] == "repeat"
        ]
        self.assertEqual(repeat_arguments, ["1", "1", "2"])
        self.assertGreater(captured_size, plain_size)
        self.assertGreater(captured_seconds, 0)
        self.assertGreater(plain_seconds, 0)

    def test_stack_overflow_keeps_jsonl_and_parent_links_complete(self):
        raw = Path(self.temp.name) / "stack-overflow.jsonl"
        raw.unlink(missing_ok=True)
        command = [
            "java",
            "-Xss256k",
            f"-Dfltrace.raw.file={raw}",
            "-Dfltrace.test.class=overflowfixture.StackOverflowWorkload",
            "-Dfltrace.test.method=recurse",
            f"-javaagent:{AGENT_JAR}=class:overflowfixture.StackOverflowWorkload",
            "-cp", f"{self.classes}:{AGENT_JAR}",
            "overflowfixture.StackOverflowDriver",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STACK_OVERFLOW_RECORDED", result.stdout)
        self.assertNotIn("STACK_OVERFLOW_TOP=fltrace.TraceRuntime", result.stdout)
        self.assertNotIn("fltrace-shutdown", result.stderr)
        self.assertNotIn("NoSuchMethodError", result.stderr)
        self.assertTrue(raw.read_bytes().endswith(b"\n"))

        events = load_events(raw)
        enter_ids = {
            event["invocation_id"]
            for event in events if event["type"] == "ENTER"
        }
        self.assertTrue(enter_ids)
        self.assertTrue(all(
            not event.get("parent_id") or event["parent_id"] in enter_ids
            for event in events if event["type"] == "ENTER"
        ))
        self.assertTrue(all(
            event["invocation_id"] in enter_ids
            for event in events if event["type"] in {"RETURN", "THROW"}
        ))
        trace = build_trace(events)
        validate_trace(trace)

    def test_single_test_runner_selects_one_junit3_method(self):
        raw = Path(self.temp.name) / "junit3-selection.jsonl"
        raw.unlink(missing_ok=True)
        command = [
            "java",
            f"-Dfltrace.raw.file={raw}",
            "-Dfltrace.test.class=valuefixture.JUnit3SelectionTest",
            "-Dfltrace.test.method=testSelected",
            f"-javaagent:{AGENT_JAR}=class:valuefixture.JUnit3SelectionTest",
            "-cp", f"{self.classes}:{AGENT_JAR}",
            "fltrace.runner.SingleTestRunner",
            "valuefixture.JUnit3SelectionTest",
            "testSelected",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Run count: 1", result.stdout)
        self.assertIn("Failure count: 1", result.stdout)
        self.assertIn("selected failure", result.stdout)
        self.assertNotIn("unselected test ran", result.stdout)

    def test_delegating_constructor_failure_has_no_unclosed_outer_enter(self):
        raw = Path(self.temp.name) / "constructor-failure.jsonl"
        raw.unlink(missing_ok=True)
        command = [
            "java",
            f"-Dfltrace.raw.file={raw}",
            "-Dfltrace.test.class=valuefixture.ConstructorFailureWorkload",
            "-Dfltrace.test.method=scenario",
            f"-javaagent:{AGENT_JAR}=class:valuefixture.ConstructorFailureWorkload",
            "-cp", f"{self.classes}:{AGENT_JAR}",
            "valuefixture.ConstructorFailureDriver",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CONSTRUCTOR_FAILURE_RECORDED", result.stdout)

        events = load_events(raw)
        constructors = [
            event for event in events
            if event.get("type") == "ENTER" and event.get("method") == "<init>"
        ]
        self.assertEqual(len(constructors), 1)
        self.assertEqual(constructors[0]["descriptor"], "(Z)V")
        trace = build_trace(events)
        validate_trace(trace)

    def test_missing_exit_callbacks_are_closed_as_explicit_trace_gaps(self):
        raw = Path(self.temp.name) / "trace-gaps.jsonl"
        raw.unlink(missing_ok=True)
        command = [
            "java",
            f"-Dfltrace.raw.file={raw}",
            "-Dfltrace.test.class=valuefixture.TraceGapDriver",
            "-Dfltrace.test.method=scenario",
            "-cp", f"{self.classes}:{AGENT_JAR}",
            "valuefixture.TraceGapDriver",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TRACE_GAPS_RECORDED", result.stdout)

        events = load_events(raw)
        trace = build_trace(events)
        validate_trace(trace)
        by_method = {
            item["method"]: item for item in trace["invocations"]
        }
        self.assertEqual(by_method["outer"]["exit_type"], "RETURN")
        for method in ("inner", "leftOpen", "workerLeftOpen"):
            self.assertEqual(by_method[method]["exit_type"], "THROW")
            self.assertEqual(
                by_method[method]["exception_class"], "fltrace.TraceGap"
            )

    def test_default_limits_render_long_values_to_png(self):
        trace = self.run_default_limit_workload()
        self.assertEqual(trace["test_start"]["value_capture"], {
            "capture_values": True,
            "value_string_edge_chars": 10,
            "value_container_edge_items": 2,
            "value_nested_container_edge_items": 1,
            "value_max_depth": 2,
            "value_max_arguments": 8,
        })
        invocations = {
            item["method"]: item for item in trace["invocations"]
        }
        bounded = invocations["boundedValues"]["arguments"]

        self.assertEqual(
            bounded["items"][0]["text"],
            '"xxxxxxxxxx…xxxxxxxxxx"',
        )
        self.assertTrue(bounded["items"][0]["truncated"])
        self.assertEqual(
            bounded["items"][1]["text"],
            "[0, 1, …, 8, 9]",
        )
        self.assertEqual(
            bounded["items"][2]["text"],
            "[0, 1, …, 8, 9]",
        )
        self.assertEqual(
            bounded["items"][3]["text"],
            "{0=10, 1=11, …, 8=18, 9=19}",
        )
        self.assertEqual(
            bounded["items"][4]["text"],
            "[[0, …, 4], …, [10, …, 14]]",
        )
        self.assertTrue(all(item["truncated"] for item in bounded["items"]))

        budget = invocations["argumentBudget"]["arguments"]
        self.assertEqual(budget["count"], 6)
        self.assertEqual(len(budget["items"]), 6)
        self.assertEqual(budget["omitted_count"], 0)
        self.assertTrue(budget["truncated"])
        self.assertTrue(all(
            item["text"] == '"xxxxxxxxxx…xxxxxxxxxx"'
            for item in budget["items"]
        ))
        many = invocations["many"]["arguments"]
        self.assertEqual(many["count"], 9)
        self.assertEqual(len(many["items"]), 8)
        self.assertEqual(many["omitted_count"], 1)

        execution = project_execution(
            trace, "valuefixture.ValueWorkload", "scenario"
        )
        puml = make_puml(
            execution,
            title="Default value limits",
            boundary_invocations=[],
            graph_folds=[],
            invocation_labels={
                int(item["invocation_id"]): f"T1-C{item['invocation_id']}"
                for item in execution["invocations"]
            },
            highlighted_invocation_ids=[],
        )
        self.assertIn('"xxxxxxxxxx…xxxxxxxxxx"', puml)
        self.assertIn("[0, 1, …, 8, 9]", puml)
        self.assertIn(
            "{0=10, 1=11, …, 8=18, 9=19}",
            puml,
        )
        self.assertIn("[[0, …, 4], …, [10, …, 14]]", puml)
        self.assertNotIn("… (+2 omitted)", puml)
        self.assertEqual(puml.count('"xxxxxxxxxx…xxxxxxxxxx"'), 7)
        self.assertIn("… (+1 omitted)", puml)
        self.assertIn("\\n", puml)

        with tempfile.TemporaryDirectory() as directory:
            puml_path = Path(directory) / "default-value-limits.puml"
            png_path = puml_path.with_suffix(".png")
            puml_path.write_text(puml, encoding="utf-8")
            ensure_rendered(
                puml_path,
                png_path,
                jar=DEFAULT_PLANTUML_JAR,
                timeout=30,
                limit_size=32768,
            )
            self.assertEqual(png_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

if __name__ == "__main__":
    unittest.main()
