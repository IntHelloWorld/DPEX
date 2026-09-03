import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from mllmfl.domain.trace import build_trace, load_events, validate_trace


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
            "value_max_chars": 80,
            "value_max_items": 8,
            "value_max_depth": 2,
            "value_max_arguments_chars": 240,
        }
        command = [
            "java",
            f"-Dfltrace.raw.file={raw}",
            "-Dfltrace.test.class=valuefixture.ValueWorkload",
            "-Dfltrace.test.method=scenario",
            f"-Dfltrace.capture.values={str(capture_values).lower()}",
            "-Dfltrace.value.max.chars=80",
            "-Dfltrace.value.max.items=8",
            "-Dfltrace.value.max.depth=2",
            "-Dfltrace.value.max.arguments.chars=240",
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

    def test_safe_value_protocol_and_statuses(self):
        events, trace, _, _, config, _ = self.run_workload(True)
        start = next(event for event in events if event["type"] == "TEST_START")
        self.assertEqual(start["agent_protocol_version"], 4)
        self.assertEqual(start["value_capture"], config)
        self.assertEqual(trace["schema_version"], 4)
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

if __name__ == "__main__":
    unittest.main()
