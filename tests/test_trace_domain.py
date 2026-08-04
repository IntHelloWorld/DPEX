import json
import tempfile
import unittest
from pathlib import Path

from mllmfl.domain.trace import build_trace, load_events, project_fault_window, validate_trace


def events(exit_type="RETURN"):
    result = [
        {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
        {"type": "ENTER", "seq": 2, "ts_ns": 10, "thread_id": 1, "thread_name": "main",
         "invocation_id": 1, "parent_id": 0, "class": "p.Test", "method": "testCase", "descriptor": "()V"},
        {"type": "ENTER", "seq": 3, "ts_ns": 20, "thread_id": 1, "thread_name": "main",
         "invocation_id": 2, "parent_id": 1, "class": "p.Service", "method": "run", "descriptor": "(I)V"},
        {"type": exit_type, "seq": 4, "ts_ns": 30, "thread_id": 1,
         "invocation_id": 2, "duration_ns": 10},
        {"type": "RETURN", "seq": 5, "ts_ns": 40, "thread_id": 1,
         "invocation_id": 1, "duration_ns": 30},
        {"type": "TEST_END", "seq": 6, "successful": exit_type == "RETURN"},
    ]
    if exit_type == "THROW":
        result[3].update({"exception_class": "p.Failure", "message": "bad"})
        result.insert(-1, {"type": "TEST_FAILURE", "seq": 5,
                           "exception_class": "p.Failure", "message": "bad"})
        result[-1]["seq"] = 7
    return result


class EventParsingTests(unittest.TestCase):
    def test_builds_full_trace_with_parent_chain_and_exit(self):
        trace = build_trace(events("THROW"))
        self.assertEqual(trace["schema_version"], 2)
        self.assertEqual(trace["calls"][0]["parent_chain"], [1])
        self.assertEqual(trace["calls"][0]["exit_type"], "THROW")
        validate_trace(trace)

    def test_rejects_duplicate_invocation(self):
        value = events()
        value.insert(2, dict(value[1]))
        with self.assertRaisesRegex(ValueError, "duplicate invocation"):
            build_trace(value)

    def test_rejects_exit_without_enter_and_unclosed_invocation(self):
        with self.assertRaisesRegex(ValueError, "requires at least one ENTER"):
            build_trace([{"type": "RETURN", "invocation_id": 7}])
        with self.assertRaisesRegex(ValueError, "exit without ENTER"):
            build_trace([
                {"type": "ENTER", "invocation_id": 1, "parent_id": 0},
                {"type": "RETURN", "invocation_id": 7},
            ])
        with self.assertRaisesRegex(ValueError, "unclosed invocations"):
            build_trace(events()[:-3])

    def test_load_events_rejects_corrupt_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text('{"type":"ENTER"}\nnot-json\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 2"):
                load_events(path)


class WindowTests(unittest.TestCase):
    def test_prefers_failure_stack_and_preserves_thread(self):
        trace = build_trace(events())
        window = project_fault_window(trace, "p.Test", "testCase", " at p.Service.run(X.java:1)")
        self.assertEqual(window["focus"]["mode"], "failure_stack")
        self.assertEqual(window["calls"][0]["thread_id"], 1)

    def test_test_then_tail_fallback_and_size_limit(self):
        trace = build_trace(events())
        by_test = project_fault_window(trace, "p.Test", "testCase")
        self.assertEqual(by_test["focus"]["mode"], "test_method")
        by_tail = project_fault_window(trace, "missing.Test", "none", max_calls=1)
        self.assertEqual(by_tail["focus"]["mode"], "tail")
        self.assertLessEqual(by_tail["call_count"], 1)
