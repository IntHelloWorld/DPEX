import json
import tempfile
import unittest
from pathlib import Path

from mllmfl.domain.trace import build_trace, load_events, project_execution, validate_trace


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
        self.assertEqual(trace["schema_version"], 3)
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

    def test_removes_complete_class_initializer_subtree(self):
        value = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.StaticState", "method": "<clinit>", "descriptor": "()V"},
            {"type": "ENTER", "seq": 4, "invocation_id": 3, "parent_id": 2,
             "class": "p.Helper", "method": "load", "descriptor": "()V"},
            {"type": "RETURN", "seq": 5, "invocation_id": 3},
            {"type": "RETURN", "seq": 6, "invocation_id": 2},
            {"type": "RETURN", "seq": 7, "invocation_id": 1},
            {"type": "TEST_END", "seq": 8, "successful": True},
        ]
        trace = build_trace(value)
        self.assertEqual(trace["invocation_count"], 1)
        self.assertEqual(trace["call_count"], 0)
        self.assertNotIn("<clinit>", json.dumps(trace))
        self.assertNotIn("p.Helper.load", json.dumps(trace))


class ExecutionProjectionTests(unittest.TestCase):
    def test_preserves_every_filtered_call_and_thread(self):
        trace = build_trace(events())
        execution = project_execution(trace, "p.Test", "testCase")
        self.assertEqual(execution["schema"], "fullchain-execution")
        self.assertEqual(execution["original_call_count"], 1)
        self.assertEqual(execution["filtered_call_count"], 1)
        self.assertEqual(execution["call_count"], 1)
        self.assertEqual(execution["calls"][0]["thread_id"], 1)

    def test_does_not_compress_repeated_sibling_calls(self):
        repeated = events()
        repeated.insert(4, {
            "type": "ENTER", "seq": 5, "ts_ns": 31, "thread_id": 1,
            "thread_name": "main", "invocation_id": 3, "parent_id": 1,
            "class": "p.Service", "method": "run", "descriptor": "(I)V",
        })
        repeated.insert(5, {
            "type": "RETURN", "seq": 6, "ts_ns": 32, "thread_id": 1,
            "invocation_id": 3, "duration_ns": 1,
        })
        repeated[6]["seq"] = 7
        repeated[7]["seq"] = 8
        execution = project_execution(build_trace(repeated), "p.Test", "testCase")
        self.assertEqual(execution["call_count"], 2)
        self.assertEqual([call["count"] for call in execution["calls"]], [1, 1])
        self.assertEqual(
            [call["invocation_ids"] for call in execution["calls"]], [[2], [3]]
        )
