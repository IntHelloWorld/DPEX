import unittest

from mllmfl.domain.trace import build_trace, project_fault_window
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
        window = project_fault_window(build_trace(events()), "p.Test", "testCase")
        puml = make_puml(window, "P", "1", "1")
        lines = puml.splitlines()
        self.assertEqual(sum(line.startswith("activate ") for line in lines),
                         sum(line.startswith("deactivate ") for line in lines))
        self.assertIn("M001 testCase()", puml)
        self.assertIn("M002 run(int)", puml)
        self.assertNotIn("[inv:", puml)
        self.assertIn("[-> p_", puml)
        self.assertIn("-->]: return", puml)

    def test_throw_and_repeat_marker(self):
        window = project_fault_window(build_trace(events("THROW")), "p.Test", "testCase")
        window["calls"][0]["count"] = 3
        puml = make_puml(window)
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
        puml = make_puml(project_fault_window(build_trace(nested), "p.Test", "testCase"))
        self.assertLess(puml.index("work()"), puml.index(": return", puml.index("work()")))
        self.assertLess(puml.index(": return", puml.index("work()")), puml.rindex(": return"))
