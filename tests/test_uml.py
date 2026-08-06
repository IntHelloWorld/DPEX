import unittest

from mllmfl.domain.trace import build_trace, project_execution
from mllmfl.stages.uml import make_puml, minimal_class_labels, readable_signature
from tests.test_trace_domain import events


class UMLTests(unittest.TestCase):
    def test_shortest_unique_class_labels(self):
        labels = minimal_class_labels(["a.left.Node", "b.right.Node", "b.Service"])
        self.assertEqual(labels["a.left.Node"], "left.Node")
        self.assertEqual(labels["b.right.Node"], "right.Node")
        self.assertEqual(labels["b.Service"], "Service")

    def test_descriptor_is_readable_and_activation_balanced(self):
        self.assertEqual(readable_signature("run", "(ILjava/lang/String;[I)V"), "run(int, String, int[])")
        execution = project_execution(build_trace(events()), "p.Test", "testCase")
        puml = make_puml(execution, "P", "1", "1")
        lines = puml.splitlines()
        self.assertEqual(sum(line.startswith("activate ") for line in lines),
                         sum(line.startswith("deactivate ") for line in lines))
        self.assertIn("M001 testCase()", puml)
        self.assertIn("M002 run(int)", puml)
        self.assertNotIn("[inv:", puml)
        self.assertIn("[-> p_", puml)
        self.assertIn("-->]: return", puml)

    def test_throw_and_repeat_marker(self):
        execution = project_execution(build_trace(events("THROW")), "p.Test", "testCase")
        execution["calls"][0]["count"] = 3
        puml = make_puml(execution)
        self.assertIn("throws", puml)
        self.assertIn("×3", puml)

    def test_nested_call_returns_in_stack_order(self):
        nested = events()
        nested.insert(3, {
            "type": "ENTER", "seq": 4, "ts_ns": 25, "thread_id": 1, "thread_name": "main",
            "invocation_id": 3, "parent_id": 2, "class": "p.Helper", "method": "work", "descriptor": "()V",
        })
        nested[4].update({"seq": 5, "invocation_id": 3})
        nested.insert(5, {"type": "RETURN", "seq": 6, "ts_ns": 35, "thread_id": 1,
                          "invocation_id": 2, "duration_ns": 15})
        nested[6]["seq"] = 7
        nested[7]["seq"] = 8
        puml = make_puml(project_execution(build_trace(nested), "p.Test", "testCase"))
        self.assertLess(puml.index("work()"), puml.index(": return", puml.index("work()")))
        self.assertLess(puml.index(": return", puml.index("work()")), puml.rindex(": return"))

    def test_merges_adjacent_identical_subtrees_at_the_highest_level(self):
        repeated = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.Service", "method": "outer", "descriptor": "()V"},
            {"type": "ENTER", "seq": 4, "invocation_id": 3, "parent_id": 2,
             "class": "p.Helper", "method": "inner", "descriptor": "()V"},
            {"type": "RETURN", "seq": 5, "invocation_id": 3},
            {"type": "RETURN", "seq": 6, "invocation_id": 2},
            {"type": "ENTER", "seq": 7, "invocation_id": 4, "parent_id": 1,
             "class": "p.Service", "method": "outer", "descriptor": "()V"},
            {"type": "ENTER", "seq": 8, "invocation_id": 5, "parent_id": 4,
             "class": "p.Helper", "method": "inner", "descriptor": "()V"},
            {"type": "RETURN", "seq": 9, "invocation_id": 5},
            {"type": "RETURN", "seq": 10, "invocation_id": 4},
            {"type": "RETURN", "seq": 11, "invocation_id": 1},
            {"type": "TEST_END", "seq": 12, "successful": True},
        ]
        execution = project_execution(build_trace(repeated), "p.Test", "testCase")
        puml = make_puml(execution)
        self.assertEqual(puml.count("outer()"), 1)
        self.assertEqual(puml.count("inner()"), 1)
        self.assertIn("outer() ×2", puml)
        self.assertNotIn("inner() ×2", puml)

    def test_does_not_merge_identical_subtrees_across_an_intervening_call(self):
        non_adjacent = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.Service", "method": "repeat", "descriptor": "()V"},
            {"type": "RETURN", "seq": 4, "invocation_id": 2},
            {"type": "ENTER", "seq": 5, "invocation_id": 3, "parent_id": 1,
             "class": "p.Service", "method": "separator", "descriptor": "()V"},
            {"type": "RETURN", "seq": 6, "invocation_id": 3},
            {"type": "ENTER", "seq": 7, "invocation_id": 4, "parent_id": 1,
             "class": "p.Service", "method": "repeat", "descriptor": "()V"},
            {"type": "RETURN", "seq": 8, "invocation_id": 4},
            {"type": "RETURN", "seq": 9, "invocation_id": 1},
            {"type": "TEST_END", "seq": 10, "successful": True},
        ]
        execution = project_execution(build_trace(non_adjacent), "p.Test", "testCase")
        puml = make_puml(execution)
        self.assertEqual(puml.count("repeat()"), 2)
        self.assertNotIn("repeat() ×2", puml)
