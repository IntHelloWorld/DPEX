import unittest

from mllmfl.domain.diagram_graph import plan_diagram_graph


def call_item(invocation_id, children=None, caller="p.Root", callee="p.Child"):
    nested = list(children or [])
    participants = {caller, callee}
    for child in nested:
        participants.update(child["participant_classes"])
    return {
        "representative_invocation_id": invocation_id,
        "invocation": {
            "id": invocation_id,
            "class": callee,
            "signature": f"{callee}.m{invocation_id}()",
            "enter_seq": invocation_id * 2,
            "exit_seq": invocation_id * 2 + 1,
        },
        "call": {
            "caller_class": caller,
            "callee_class": callee,
        },
        "children": nested,
        "represented_call_count": (
            1 + sum(child["represented_call_count"] for child in nested)
        ),
        "participant_classes": sorted(participants),
    }


def root_item(children):
    nested = list(children)
    participants = {"p.Root"}
    for child in nested:
        participants.update(child["participant_classes"])
    return {
        "representative_invocation_id": 0,
        "invocation": {
            "id": 0,
            "class": "p.Root",
            "signature": "execution root",
            "enter_seq": 0,
            "exit_seq": 100000,
        },
        "call": None,
        "children": nested,
        "represented_call_count": sum(
            child["represented_call_count"] for child in nested
        ),
        "participant_classes": sorted(participants),
    }


class DiagramGraphPlannerTests(unittest.TestCase):
    def test_packs_tail_of_forty_direct_children_into_one_bundle(self):
        focus = root_item([call_item(index) for index in range(1, 41)])

        graph = plan_diagram_graph(focus, 24, 8, "synthetic_execution_root")
        entry = graph["nodes"][0]

        self.assertEqual(entry["visible_unit_count"], 24)
        self.assertEqual(len(entry["visible_items"]), 23)
        self.assertEqual(len(entry["folds"]), 1)
        self.assertEqual(entry["folds"][0]["kind"], "SIBLING_BUNDLE")
        self.assertEqual(len(entry["folds"][0]["invocation_ids"]), 17)
        self.assertEqual(len(graph["nodes"]), 2)
        following = graph["nodes"][1]
        self.assertEqual(entry["sibling_group"]["peer_diagram_ids"], ["D-002"])
        self.assertEqual(following["sibling_group"]["peer_diagram_ids"], ["D-001"])
        self.assertEqual(entry["links"][0]["direction"], "PEER")
        self.assertEqual(entry["links"][0]["relation"], "SIBLING_PEER")
        self.assertEqual(following["folds"][0]["position"], "PREFIX")
        self.assertEqual(
            following["folds"][0]["peer_ranges"][0]["diagram_id"],
            "D-001",
        )

    def test_three_sibling_views_are_fully_connected(self):
        focus = root_item([call_item(index) for index in range(1, 61)])

        graph = plan_diagram_graph(focus, 24, 8, "synthetic_execution_root")
        members = graph["nodes"][:3]

        self.assertEqual([node["diagram_id"] for node in members], [
            "D-001", "D-002", "D-003",
        ])
        for node in members:
            expected_peers = {
                value["diagram_id"] for value in members
                if value["diagram_id"] != node["diagram_id"]
            }
            self.assertEqual(
                set(node["sibling_group"]["peer_diagram_ids"]), expected_peers
            )
            self.assertEqual(
                {
                    link["diagram_id"] for link in node["links"]
                    if link["relation"] == "SIBLING_PEER"
                },
                expected_peers,
            )
        self.assertEqual(
            [fold["position"] for fold in members[0]["folds"]], ["SUFFIX"]
        )
        self.assertEqual(
            [fold["position"] for fold in members[1]["folds"]],
            ["PREFIX", "SUFFIX"],
        )
        self.assertEqual(
            [fold["position"] for fold in members[2]["folds"]], ["PREFIX"]
        )
        self.assertEqual(
            [
                value["diagram_id"]
                for value in members[2]["folds"][0]["peer_ranges"]
            ],
            ["D-001", "D-002"],
        )

    def test_does_not_split_repeat_sequence_across_sibling_views(self):
        children = [call_item(index) for index in range(1, 23)]
        first = call_item(23)
        second = call_item(24)
        for position, item in enumerate((first, second), 1):
            item["repeat_count"] = 3
            item["repeat_sequence"] = {
                "sequence_id": "RS-23",
                "pattern_length": 2,
                "position": position,
                "repeat_count": 3,
            }
        children.extend((first, second, call_item(25)))

        graph = plan_diagram_graph(
            root_item(children), 24, 8, "synthetic_execution_root"
        )
        owners = {
            item["representative_invocation_id"]: node["diagram_id"]
            for node in graph["nodes"]
            for item in node["visible_items"]
        }

        self.assertEqual(owners[23], owners[24])

    def test_folds_later_child_internals_without_bundling_nested_calls(self):
        children = []
        for index in range(1, 11):
            descendants = [
                call_item(
                    100 + index * 10 + offset,
                    caller="p.Child",
                    callee="p.Helper",
                )
                for offset in range(3)
            ]
            children.append(call_item(index, descendants))

        graph = plan_diagram_graph(
            root_item(children), 24, 8, "synthetic_execution_root"
        )
        entry = graph["nodes"][0]

        self.assertEqual(len(entry["visible_roots"]), 10)
        self.assertLessEqual(entry["visible_unit_count"], 24)
        self.assertTrue(entry["folds"])
        self.assertEqual(
            {fold["kind"] for fold in entry["folds"]}, {"CALL_INTERNAL"}
        )

    def test_participant_budget_uses_bundle_without_hidden_lifelines(self):
        children = [
            call_item(index, callee=f"p.C{index}")
            for index in range(1, 11)
        ]

        graph = plan_diagram_graph(
            root_item(children), 24, 8, "synthetic_execution_root"
        )

        self.assertTrue(any(node["folds"] for node in graph["nodes"]))
        for node in graph["nodes"]:
            self.assertLessEqual(node["visible_unit_count"], 24)
            self.assertLessEqual(len(node["participant_classes"]), 8)
            visible_classes = {"p.Root"}
            for item in node["visible_items"]:
                visible_classes.update(
                    value
                    for value in (
                        item["call"].get("caller_class"),
                        item["call"].get("callee_class"),
                    )
                    if value
                )
            self.assertEqual(set(node["participant_classes"]), visible_classes)

    def test_every_invocation_is_visible_in_exactly_one_graph_node(self):
        nested = [call_item(index) for index in range(2, 8)]
        focus = root_item([call_item(1, nested)] + [
            call_item(index) for index in range(8, 35)
        ])

        graph = plan_diagram_graph(focus, 12, 4, "synthetic_execution_root")
        visible_ids = [
            item["representative_invocation_id"]
            for node in graph["nodes"]
            for item in node["visible_items"]
        ]

        self.assertEqual(len(visible_ids), len(set(visible_ids)))
        self.assertEqual(set(visible_ids), set(range(1, 35)))
        node_ids = {node["diagram_id"] for node in graph["nodes"]}
        for node in graph["nodes"]:
            for link in node["links"]:
                self.assertIn(link["diagram_id"], node_ids)

    def test_represented_call_counts_partition_repeated_subtrees(self):
        child = call_item(2, caller="p.Child", callee="p.Helper")
        child["repeat_count"] = 2
        parent = call_item(1, [child])
        parent["repeat_count"] = 3

        graph = plan_diagram_graph(
            root_item([parent]), 24, 8, "synthetic_execution_root"
        )

        self.assertEqual(
            sum(node["represented_call_count"] for node in graph["nodes"]),
            9,
        )


if __name__ == "__main__":
    unittest.main()
