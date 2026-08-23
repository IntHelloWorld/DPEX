import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mllmfl.domain.schemas import validate_compressed_execution, validate_uml_index
from mllmfl.domain.interaction import IMAGE_ONLY_MODE
from mllmfl.domain.trace import build_trace, project_execution
from mllmfl.infrastructure.io import write_json
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.stages import uml
from mllmfl.stages.uml import (
    adaptive_graph_diagram_nodes,
    compress_execution,
    first_level_segments,
    make_puml,
    method_signatures,
    minimal_class_labels,
    readable_signature,
    recursive_compressed_diagram_nodes,
)
from tests.test_trace_domain import events


class UMLTests(unittest.TestCase):
    def test_uml_stage_rejects_invalid_sliced_trace_metadata(self):
        execution = project_execution(build_trace(events()), "p.Test", "testCase")
        execution["slice"] = {"schema": "invalid", "schema_version": 2}

        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory))
            layout.ensure()
            trigger_dir = layout.trigger_dir("P", "1", 1)
            trigger_dir.mkdir(parents=True)
            write_json(trigger_dir / "execution.json", execution)
            write_json(trigger_dir / "execution_sliced.json", execution)

            rows = uml.run(
                layout,
                ["P"],
                {"1"},
                None,
                "plantuml",
                None,
                30,
            )

            error = (
                layout.stage_log_dir("uml", "P", "1", "1") / "error.log"
            ).read_text(encoding="utf-8")

        self.assertEqual(rows, [{
            "project": "P", "bug": "1", "trigger": "1", "status": "ERROR",
            "segment_count": 0,
        }])
        self.assertIn("unsupported test slice schema", error)

    def test_adaptive_graph_renders_symmetric_sibling_views(self):
        nested = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.Parent", "method": "parent", "descriptor": "()V"},
        ]
        sequence = 4
        for invocation_id in range(3, 43):
            nested.extend([
                {"type": "ENTER", "seq": sequence,
                 "invocation_id": invocation_id, "parent_id": 2,
                 "class": "p.Child", "method": f"m{invocation_id}",
                 "descriptor": "()V"},
                {"type": "RETURN", "seq": sequence + 1,
                 "invocation_id": invocation_id},
            ])
            sequence += 2
        nested.extend([
            {"type": "RETURN", "seq": sequence, "invocation_id": 2},
            {"type": "RETURN", "seq": sequence + 1, "invocation_id": 1},
            {"type": "TEST_END", "seq": sequence + 2, "successful": True},
        ])
        execution = project_execution(build_trace(nested), "p.Test", "testCase")
        compressed = compress_execution(execution)
        test_invocation = next(
            item for item in execution["invocations"]
            if int(item["invocation_id"]) == 1
        )
        children = compressed["root_groups"][0]["calls"]
        focus = {
            "representative_invocation_id": 1,
            "call": None,
            "invocation": test_invocation,
            "repeat_count": 1,
            "represented_call_count": 41,
            "displayed_subtree_call_count": 42,
            "participant_classes": ["p.Child", "p.Parent", "p.Test"],
            "subtree_fingerprint": "test-root",
            "children": children,
        }

        def render_graph(paths, *_args, **_kwargs):
            successes = {}
            for puml_path in paths:
                png_path = puml_path.with_suffix(".png")
                png_path.write_bytes(b"png")
                successes[puml_path] = png_path
            return successes, {}

        with tempfile.TemporaryDirectory() as directory, patch(
            "mllmfl.infrastructure.plantuml.render_many", side_effect=render_graph
        ):
            diagram_dir = Path(directory) / "sequence_diagrams"
            diagram_dir.mkdir()
            nodes, entry_id, failures, method_catalog = adaptive_graph_diagram_nodes(
                execution, focus, "test_invocation", diagram_dir,
                "P", "1", "trigger", "plantuml", None, 30, 4096,
                24, 8, 100,
            )
            entry_puml = (diagram_dir / "D-001.puml").read_text(encoding="utf-8")
            first_peer_puml = (diagram_dir / "D-002.puml").read_text(encoding="utf-8")
            second_peer_puml = (diagram_dir / "D-003.puml").read_text(encoding="utf-8")

        self.assertEqual(entry_id, "D-001")
        self.assertEqual(failures, [])
        self.assertEqual(method_catalog, [])
        self.assertEqual(sum(node["represented_call_count"] for node in nodes), 41)
        self.assertEqual(nodes[1]["visible_unit_count"], 24)
        self.assertEqual(nodes[1]["folds"][0]["kind"], "SIBLING_BUNDLE")
        self.assertIn("M025-M041: 17 calls / VIEW D-003", first_peer_puml)
        self.assertIn("M002-M024: 23 calls / VIEW D-002", second_peer_puml)
        self.assertIn("TO D-002", entry_puml)
        self.assertIn("FROM D-001", first_peer_puml)
        self.assertNotIn("FROM D-002", second_peer_puml)
        first_parent_alias = first_peer_puml.split(
            'participant "Parent" as '
        )[1].splitlines()[0]
        second_parent_alias = second_peer_puml.split(
            'participant "Parent" as '
        )[1].splitlines()[0]
        self.assertIn(
            f"{first_parent_alias} -> {first_parent_alias}: SIBLING VIEWS\n"
            "note right #DCEFF8\n"
            "M025-M041: 17 calls / VIEW D-003\n"
            "end note",
            first_peer_puml,
        )
        self.assertIn(
            f"{second_parent_alias} -> {second_parent_alias}: SIBLING VIEWS\n"
            "note right #DCEFF8\n"
            "M002-M024: 23 calls / VIEW D-002\n"
            "end note",
            second_peer_puml,
        )
        self.assertNotIn("rnote right of", first_peer_puml)
        self.assertNotIn("rnote right of", second_peer_puml)
        self.assertIn("M001 parent()", first_peer_puml)
        self.assertIn("M001 parent()", second_peer_puml)
        self.assertIn('participant "Test"', first_peer_puml)
        self.assertIn('participant "Test"', second_peer_puml)
        self.assertNotIn(" context", entry_puml + first_peer_puml + second_peer_puml)
        self.assertEqual(nodes[1]["links"][1]["direction"], "PEER")
        self.assertEqual(nodes[2]["links"][0]["direction"], "PEER")
        index = {
            "schema": "execution-uml-graph", "schema_version": 1,
            "source_schema": "fullchain-execution",
            "strategy": "test-root-adaptive-graph",
            "entry_reason": "test_invocation", "slice_applied": True,
            "test": {"class": "p.Test", "method": "testCase"},
            "root_invocation_id": 1,
            "max_visible_units": 24, "max_participants_per_image": 8,
            "trace_call_count": 41, "layout_root_call_count": 0,
            "source_call_count": 41, "partitioned_call_count": 41,
            "excluded_call_count": 0, "entry_diagram_id": entry_id,
            "node_count": len(nodes), "diagram_count": len(nodes),
            "nodes": nodes,
        }
        self.assertIs(validate_uml_index(index), index)
        all_puml = entry_puml + first_peer_puml + second_peer_puml
        self.assertNotIn("GROUP", all_puml)
        self.assertNotIn("PAGE", all_puml)

    def test_keeps_unary_group_boundaries_visible_as_pages(self):
        nested = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.Outer", "method": "outer", "descriptor": "()V"},
            {"type": "ENTER", "seq": 4, "invocation_id": 3, "parent_id": 2,
             "class": "p.Middle", "method": "middle", "descriptor": "()V"},
            {"type": "ENTER", "seq": 5, "invocation_id": 4, "parent_id": 3,
             "class": "p.Leaf", "method": "leaf", "descriptor": "()V"},
            {"type": "RETURN", "seq": 6, "invocation_id": 4},
            {"type": "RETURN", "seq": 7, "invocation_id": 3},
            {"type": "RETURN", "seq": 8, "invocation_id": 2},
            {"type": "RETURN", "seq": 9, "invocation_id": 1},
            {"type": "TEST_END", "seq": 10, "successful": True},
        ]
        execution = project_execution(build_trace(nested), "p.Test", "testCase")
        compressed = compress_execution(execution)
        root = compressed["root_groups"][0]["calls"][0]

        def render_pages(paths, *_args, **_kwargs):
            successes = {}
            for puml_path in paths:
                png_path = puml_path.with_suffix(".png")
                png_path.write_bytes(b"png")
                successes[puml_path] = png_path
            return successes, {}

        with tempfile.TemporaryDirectory() as directory, patch(
            "mllmfl.infrastructure.plantuml.render_many", side_effect=render_pages
        ):
            artifact_dir = Path(directory)
            diagram_dir = artifact_dir / "sequence_diagrams"
            diagram_dir.mkdir()
            nodes, root_ids, failures = recursive_compressed_diagram_nodes(
                execution, compressed, [("L1-001-inv-2", root)], diagram_dir,
                "P", "1", "trigger", "plantuml", None, 30, 4096, 1, 8, 100,
            )
            by_id = {node["diagram_id"]: node for node in nodes}
            self.assertEqual(root_ids, ["L1-001-M001"])
            self.assertEqual(by_id["L1-001-M001"]["node_type"], "GROUP")
            self.assertIn("L1-001-M001.P001", by_id["L1-001-M001"]["children"])
            self.assertIn(
                "L1-001-M001.L2-M002", by_id["L1-001-M001"]["children"]
            )
            self.assertEqual(
                by_id["L1-001-M001.L2-M002"]["parent_id"], "L1-001-M001"
            )
            self.assertIn(
                "M001 outer()",
                (diagram_dir / "L1-001-M001.P001.puml").read_text(encoding="utf-8"),
            )
            parent_puml = (
                diagram_dir / "L1-001-M001.P001.puml"
            ).read_text(encoding="utf-8")
            child_puml = (
                diagram_dir / "L1-001-M001.L2-M002.P001.puml"
            ).read_text(encoding="utf-8")
            self.assertIn(
                "TO L1-001-M001.L2-M002.P001",
                parent_puml,
            )
            self.assertIn("rnote right of", parent_puml)
            self.assertIn("M002 middle()", parent_puml)
            self.assertIn(
                "FROM L1-001-M001.P001", child_puml
            )
            self.assertIn("rnote left of", child_puml)
            self.assertNotIn("group ref /", parent_puml + child_puml)
            self.assertNotIn('participant "ref', parent_puml + child_puml)
            self.assertEqual(by_id["L1-001-M001.P001"]["references"], [{
                "direction": "TO",
                "diagram_id": "L1-001-M001.L2-M002.P001",
                "invocation_id": 3,
                "message_id": "M002",
                "relation": "CHILD",
            }])
            self.assertEqual(failures, [])

    def test_pairs_top_level_sibling_pages_with_minimal_refs(self):
        sibling_events = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.First", "method": "first", "descriptor": "()V"},
            {"type": "RETURN", "seq": 4, "invocation_id": 2},
            {"type": "ENTER", "seq": 5, "invocation_id": 3, "parent_id": 1,
             "class": "p.Second", "method": "second", "descriptor": "()V"},
            {"type": "RETURN", "seq": 6, "invocation_id": 3},
            {"type": "RETURN", "seq": 7, "invocation_id": 1},
            {"type": "TEST_END", "seq": 8, "successful": True},
        ]
        execution = project_execution(build_trace(sibling_events), "p.Test", "testCase")
        compressed = compress_execution(execution)
        roots = compressed["root_groups"][0]["calls"]

        def render_pages(paths, *_args, **_kwargs):
            successes = {}
            for puml_path in paths:
                png_path = puml_path.with_suffix(".png")
                png_path.write_bytes(b"png")
                successes[puml_path] = png_path
            return successes, {}

        with tempfile.TemporaryDirectory() as directory, patch(
            "mllmfl.infrastructure.plantuml.render_many", side_effect=render_pages
        ):
            artifact_dir = Path(directory)
            diagram_dir = artifact_dir / "sequence_diagrams"
            diagram_dir.mkdir()
            nodes, root_ids, failures = recursive_compressed_diagram_nodes(
                execution, compressed,
                [("L1-001-inv-2", roots[0]), ("L1-002-inv-3", roots[1])],
                diagram_dir, "P", "1", "trigger", "plantuml", None,
                30, 4096, 24, 8, 100,
            )
            previous = (diagram_dir / "L1-001-M001.puml").read_text(encoding="utf-8")
            following = (diagram_dir / "L1-002-M002.puml").read_text(encoding="utf-8")
        by_id = {node["diagram_id"]: node for node in nodes}
        self.assertEqual(root_ids, ["L1-001-M001", "L1-002-M002"])
        self.assertIn("TO L1-002-M002", previous)
        self.assertIn("rnote right of", previous)
        self.assertIn("M002 second()", previous)
        self.assertIn("FROM L1-001-M001", following)
        test_alias = following.split('participant "Test" as ')[1].splitlines()[0]
        second_alias = following.split('participant "Second" as ')[1].splitlines()[0]
        self.assertIn(
            f"rnote left of {test_alias} #DCEFF8\n"
            "FROM L1-001-M001\n"
            "endrnote",
            following,
        )
        self.assertIn(f"[--> {test_alias}: return", following)
        self.assertIn(
            f"{test_alias} -> {second_alias}: M002 second()",
            following,
        )
        self.assertIn(f"{second_alias} --> {test_alias}: return", following)
        self.assertEqual(
            sum(line.startswith("activate ") for line in following.splitlines()),
            sum(line.startswith("deactivate ") for line in following.splitlines()),
        )
        self.assertNotIn("group ref /", previous + following)
        self.assertNotIn('participant "ref', previous + following)
        self.assertIn('participant "Test"', following)
        self.assertEqual(by_id["L1-001-M001"]["references"][0]["relation"],
                         "NEXT_SIBLING")
        self.assertEqual(by_id["L1-002-M002"]["references"][0]["direction"], "FROM")
        self.assertEqual(failures, [])

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

    def test_uses_diagram_id_title_and_shared_message_numbers(self):
        execution = project_execution(build_trace(events()), "p.Test", "testCase")
        numbers = {
            int(invocation["invocation_id"]): index
            for index, invocation in enumerate(execution["invocations"], 41)
        }
        puml = make_puml(
            execution, "P", "1", "1", title_suffix="L1-001-inv-2",
            message_numbers=numbers,
        )
        self.assertIn("title Diagram ID: L1-001-inv-2", puml)
        self.assertNotIn("title P bug 1 trigger 1", puml)
        self.assertIn("M041 testCase()", puml)
        self.assertIn("M042 run(int)", puml)

    def test_image_only_labels_calls_and_unique_methods_separately(self):
        execution = project_execution(build_trace(events()), "p.Test", "testCase")
        puml = make_puml(
            execution,
            message_prefix="C",
            method_ids={1: "M001", 2: "M002"},
        )
        self.assertIn("C001 M001 testCase()", puml)
        self.assertIn("C002 M002 run(int)", puml)
        self.assertNotIn("M001 testCase()", puml.replace("C001 M001 testCase()", ""))

    def test_boundary_context_draws_caller_and_return_destination(self):
        execution = project_execution(build_trace(events()), "p.Test", "testCase")
        root = dict(execution["invocations"][0])
        root["caller_class"] = "p.Runner"
        puml = make_puml(
            execution, include_test_boundary=False,
            boundary_invocations=[root],
        )
        self.assertIn(f"{puml.split('participant \"Runner\" as ')[1].splitlines()[0].split()[0]} ->", puml)
        self.assertIn(": return", puml)

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
        compressed = compress_execution(execution)
        self.assertEqual(compressed["source_call_count"], 4)
        self.assertEqual(compressed["represented_call_count"], 4)
        self.assertEqual(compressed["displayed_call_count"], 2)
        root = compressed["root_groups"][0]["calls"][0]
        self.assertEqual(root["repeat_count"], 2)
        self.assertEqual(root["represented_call_count"], 4)
        self.assertEqual(root["displayed_subtree_call_count"], 2)

    def test_losslessly_compresses_repeated_complete_subtree_sequence(self):
        repeated = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V"},
        ]
        sequence = 3
        for invocation_id, method in zip(range(2, 8), ("a", "b") * 3):
            repeated.extend([
                {"type": "ENTER", "seq": sequence,
                 "invocation_id": invocation_id, "parent_id": 1,
                 "class": "p.Service", "method": method, "descriptor": "()V"},
                {"type": "RETURN", "seq": sequence + 1,
                 "invocation_id": invocation_id},
            ])
            sequence += 2
        repeated.extend([
            {"type": "RETURN", "seq": sequence, "invocation_id": 1},
            {"type": "TEST_END", "seq": sequence + 1, "successful": True},
        ])
        execution = project_execution(build_trace(repeated), "p.Test", "testCase")
        compressed = compress_execution(execution)
        validate_compressed_execution(compressed)
        invalid = copy.deepcopy(compressed)
        invalid["root_groups"][0]["calls"][1]["repeat_sequence"]["position"] = 1
        with self.assertRaisesRegex(ValueError, "repeat sequence"):
            validate_compressed_execution(invalid)
        children = compressed["root_groups"][0]["calls"]

        self.assertEqual(compressed["schema_version"], 2)
        self.assertEqual(compressed["source_call_count"], 6)
        self.assertEqual(compressed["displayed_call_count"], 2)
        self.assertEqual(len(children), 2)
        self.assertEqual([item["repeat_count"] for item in children], [3, 3])
        self.assertEqual(
            [item["occurrence_invocation_ids"] for item in children],
            [[2, 4, 6], [3, 5, 7]],
        )
        self.assertEqual(
            [item["repeat_sequence"]["position"] for item in children],
            [1, 2],
        )

        test_invocation = next(
            item for item in execution["invocations"]
            if int(item["invocation_id"]) == 1
        )
        focus = {
            "representative_invocation_id": 1,
            "call": None,
            "invocation": test_invocation,
            "repeat_count": 1,
            "represented_call_count": 6,
            "displayed_subtree_call_count": 3,
            "participant_classes": ["p.Service", "p.Test"],
            "subtree_fingerprint": "test-root",
            "children": children,
        }

        def render_graph(paths, *_args, **_kwargs):
            successes = {}
            for puml_path in paths:
                png_path = puml_path.with_suffix(".png")
                png_path.write_bytes(b"png")
                successes[puml_path] = png_path
            return successes, {}

        with tempfile.TemporaryDirectory() as directory, patch(
            "mllmfl.infrastructure.plantuml.render_many", side_effect=render_graph
        ):
            diagram_dir = Path(directory) / "sequence_diagrams"
            diagram_dir.mkdir()
            nodes, _, failures, method_catalog = adaptive_graph_diagram_nodes(
                execution, focus, "test_invocation", diagram_dir,
                "P", "1", "trigger", "plantuml", None, 30, 4096,
                24, 8, 100, IMAGE_ONLY_MODE,
            )
            puml = (diagram_dir / "D-001.puml").read_text(encoding="utf-8")

        self.assertEqual(failures, [])
        self.assertEqual(
            [(item["method_id"], item["function"]) for item in method_catalog],
            [("M001", "p.Service.a"), ("M002", "p.Service.b")],
        )
        self.assertEqual(nodes[0]["method_ids"], ["M001", "M002"])
        self.assertIn("C001 M001 a()", puml)
        self.assertIn("C002 M002 b()", puml)
        self.assertEqual(sum(node["represented_call_count"] for node in nodes), 6)
        self.assertIn("loop repeated sequence ×3", puml)
        self.assertEqual(puml.count(" a()"), 1)
        self.assertEqual(puml.count(" b()"), 1)
        self.assertNotIn("a() ×3", puml)
        self.assertNotIn("b() ×3", puml)

    def test_does_not_compress_sequence_when_one_complete_subtree_differs(self):
        events = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.Service", "method": "a", "descriptor": "()V"},
            {"type": "RETURN", "seq": 4, "invocation_id": 2},
            {"type": "ENTER", "seq": 5, "invocation_id": 3, "parent_id": 1,
             "class": "p.Service", "method": "b", "descriptor": "()V"},
            {"type": "RETURN", "seq": 6, "invocation_id": 3},
            {"type": "ENTER", "seq": 7, "invocation_id": 4, "parent_id": 1,
             "class": "p.Service", "method": "a", "descriptor": "()V"},
            {"type": "RETURN", "seq": 8, "invocation_id": 4},
            {"type": "ENTER", "seq": 9, "invocation_id": 5, "parent_id": 1,
             "class": "p.Service", "method": "b", "descriptor": "()V"},
            {"type": "ENTER", "seq": 10, "invocation_id": 8, "parent_id": 5,
             "class": "p.Helper", "method": "different", "descriptor": "()V"},
            {"type": "RETURN", "seq": 11, "invocation_id": 8},
            {"type": "RETURN", "seq": 12, "invocation_id": 5},
            {"type": "ENTER", "seq": 13, "invocation_id": 6, "parent_id": 1,
             "class": "p.Service", "method": "a", "descriptor": "()V"},
            {"type": "RETURN", "seq": 14, "invocation_id": 6},
            {"type": "ENTER", "seq": 15, "invocation_id": 7, "parent_id": 1,
             "class": "p.Service", "method": "b", "descriptor": "()V"},
            {"type": "RETURN", "seq": 16, "invocation_id": 7},
            {"type": "RETURN", "seq": 17, "invocation_id": 1},
            {"type": "TEST_END", "seq": 18, "successful": True},
        ]
        execution = project_execution(build_trace(events), "p.Test", "testCase")
        compressed = compress_execution(execution)
        children = compressed["root_groups"][0]["calls"]

        self.assertEqual(len(children), 6)
        self.assertFalse(any(item.get("repeat_sequence") for item in children))

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

    def test_partitions_repeated_first_level_methods_by_runtime_invocation(self):
        repeated = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.Service", "method": "run", "descriptor": "()V",
             "origin_test_line": 10},
            {"type": "ENTER", "seq": 4, "invocation_id": 3, "parent_id": 2,
             "class": "p.Left", "method": "onlyLeft", "descriptor": "()V"},
            {"type": "RETURN", "seq": 5, "invocation_id": 3},
            {"type": "RETURN", "seq": 6, "invocation_id": 2},
            {"type": "ENTER", "seq": 7, "invocation_id": 4, "parent_id": 1,
             "class": "p.Service", "method": "run", "descriptor": "()V",
             "origin_test_line": 20},
            {"type": "ENTER", "seq": 8, "invocation_id": 5, "parent_id": 4,
             "class": "p.Right", "method": "onlyRight", "descriptor": "()V"},
            {"type": "RETURN", "seq": 9, "invocation_id": 5},
            {"type": "RETURN", "seq": 10, "invocation_id": 4},
            {"type": "THROW", "seq": 11, "invocation_id": 1,
             "exception_class": "java.lang.AssertionError"},
            {"type": "TEST_FAILURE", "seq": 12,
             "exception_class": "java.lang.AssertionError", "source_line": 21},
            {"type": "TEST_END", "seq": 13, "successful": False},
        ]
        execution = project_execution(build_trace(repeated), "p.Test", "testCase")
        root, segments, excluded = first_level_segments(execution)
        self.assertEqual(root["invocation_id"], 1)
        self.assertEqual(excluded, 0)
        self.assertEqual([item[0]["invocation_id"] for item in segments], [2, 4])
        self.assertEqual([len(item[1]["calls"]) for item in segments], [2, 2])
        left = make_puml(segments[0][1], include_test_boundary=False)
        right = make_puml(segments[1][1], include_test_boundary=False)
        self.assertIn("onlyLeft()", left)
        self.assertNotIn("onlyRight()", left)
        self.assertIn("onlyRight()", right)
        self.assertNotIn("onlyLeft()", right)
        self.assertNotIn("-->]", left)
        self.assertNotIn("throws", left)
        self.assertEqual(
            method_signatures(segments[0][1]),
            ["p.Service.run()", "p.Left.onlyLeft()"],
        )

    def test_rejects_missing_test_root_and_empty_first_level(self):
        execution = project_execution(build_trace(events()), "p.Other", "missing")
        with self.assertRaisesRegex(ValueError, "test root"):
            first_level_segments(execution)

    def test_missing_test_method_uses_complete_trace_boundaries(self):
        setup_failure = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "setUp", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.Service", "method": "prepare", "descriptor": "()V"},
            {"type": "THROW", "seq": 4, "invocation_id": 2,
             "exception_class": "java.lang.NullPointerException"},
            {"type": "THROW", "seq": 5, "invocation_id": 1,
             "exception_class": "java.lang.NullPointerException"},
            {"type": "TEST_FAILURE", "seq": 6,
             "exception_class": "java.lang.NullPointerException", "source_line": 0},
            {"type": "TEST_END", "seq": 7, "successful": False},
        ]
        execution = project_execution(build_trace(setup_failure), "p.Test", "testCase")
        with self.assertRaisesRegex(ValueError, "test root"):
            first_level_segments(execution)
        puml = make_puml(
            execution,
            include_test_boundary=False,
            include_all_root_boundaries=True,
        )
        self.assertIn("M001 setUp()", puml)
        self.assertIn("M002 prepare()", puml)
        self.assertIn("throws", puml)
