import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from mllmfl.cli import build_parser
from mllmfl.domain.failure import extract_error_stack
from mllmfl.domain.interaction import (
    IMAGE_ONLY_MODE,
    TEXT_INDEX_MODE,
    localization_interaction_mode,
)
from mllmfl.domain.schemas import (
    validate_candidates,
    validate_defect_context,
    validate_localization,
    validate_uml_index,
)
from mllmfl.infrastructure.java_source import extract_methods
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.process import CommandResult, run_command
from mllmfl.stages import aggregate, collect
from mllmfl.stages.aggregate import aggregate_rankings
from mllmfl.stages.localize import (
    IMAGE_ONLY_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    _post_response,
    build_prompt,
    defect_output_context,
    gate_method_id_ranking,
    gate_ranking,
    parse_model_response,
    run_agent,
    test_code_context,
    validate_model_ranking_payload,
)
from mllmfl.stages.summarize import candidate_functions, candidate_without_summary


class JavaSourceTests(unittest.TestCase):
    def test_extracts_method_and_ignores_braces_in_strings(self):
        text = 'package p; class A { public int run(int x) { String s = "}"; return x; } }'
        methods = extract_methods(text, "p.A", "run")
        self.assertEqual(len(methods), 1)
        self.assertIn("return x", methods[0]["code"])


class LocalizationTests(unittest.TestCase):
    @staticmethod
    def graph_node(
        diagram_id, image="segment.png", signatures=None, links=None,
        entry_signature="p.Service.run()", origin_test_line=42,
        visible_calls=2, visible_units=2, participants=2,
    ):
        return {
            "diagram_id": diagram_id,
            "focus_invocation_id": 1,
            "entry_signature": entry_signature,
            "origin_test_line": origin_test_line,
            "represented_call_count": visible_calls,
            "visible_call_count": visible_calls,
            "visible_unit_count": visible_units,
            "participant_count": participants,
            "method_signatures": signatures or [entry_signature],
            "folds": [],
            "links": links or [],
            "puml": "segment.puml",
            "image": image,
        }

    @staticmethod
    def agent_response(message, response_id, usage=None):
        output = []
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str):
            output.append({
                "type": "reasoning",
                "id": f"reasoning-{response_id}",
                "summary": [{"type": "summary_text", "text": reasoning}],
                "encrypted_content": f"encrypted-{response_id}",
            })
        content = message.get("content")
        if isinstance(content, str):
            output.append({
                "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": content}],
            })
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            output.append({
                "type": "function_call", "status": "completed",
                "call_id": tool_call.get("id"),
                "name": function.get("name"),
                "arguments": function.get("arguments"),
            })
        return message, "vision", response_id, output, usage or {}

    def test_system_prompt_is_structured_and_defines_agent_contract(self):
        self.assertIn("\n", SYSTEM_PROMPT)
        self.assertIn("software defect-localization agent", SYSTEM_PROMPT)
        self.assertIn("## Localization Approach", SYSTEM_PROMPT)
        self.assertIn("reconstruct how the defect is triggered", SYSTEM_PROMPT)
        self.assertIn("until you have enough evidence", SYSTEM_PROMPT)
        self.assertIn("## Diagram Guide", SYSTEM_PROMPT)
        self.assertIn("Solid arrows are method calls", SYSTEM_PROMPT)
        self.assertIn("TO/FROM D-xxx", SYSTEM_PROMPT)
        for prompt in (SYSTEM_PROMPT, IMAGE_ONLY_SYSTEM_PROMPT):
            self.assertIn("Distinguish the caller", prompt)
            self.assertIn("callee whose implementation", prompt)
            self.assertIn("trigger path", prompt)
            self.assertIn("Rank a caller only", prompt)
            self.assertIn("## Tool-Use Preamble", prompt)
            self.assertIn("at most once in each assistant response", prompt)
            self.assertIn('"evidence":', prompt)
            self.assertIn('"next_action":', prompt)
            self.assertIn("request the others in later turns", prompt)
            self.assertNotIn("Observation:", prompt)
            self.assertNotIn("Hypothesis:", prompt)
        self.assertIn("## Output Contract", SYSTEM_PROMPT)
        self.assertNotIn("GROUP", SYSTEM_PROMPT)
        self.assertNotIn("PAGE", SYSTEM_PROMPT)
        self.assertIn("view_sequence_diagram(diagram_id)", SYSTEM_PROMPT)
        self.assertIn('{"ranked":[{"signature":"...","reason":"..."}]}', SYSTEM_PROMPT)
        self.assertIn("\n", IMAGE_ONLY_SYSTEM_PROMPT)
        self.assertIn("## Localization Approach", IMAGE_ONLY_SYSTEM_PROMPT)
        self.assertIn("## Diagram Guide", IMAGE_ONLY_SYSTEM_PROMPT)
        self.assertIn("## Output Contract", IMAGE_ONLY_SYSTEM_PROMPT)
        self.assertIn("Cxxx", IMAGE_ONLY_SYSTEM_PROMPT)
        self.assertIn("Mxxx", IMAGE_ONLY_SYSTEM_PROMPT)
        self.assertIn("runtime call-occurrence ID", IMAGE_ONLY_SYSTEM_PROMPT)
        self.assertIn('"method_id":"M001"', IMAGE_ONLY_SYSTEM_PROMPT)
        self.assertIn('"method_signature":"add(TickUnit)"', IMAGE_ONLY_SYSTEM_PROMPT)

    def test_initial_prompt_omits_candidates_project_and_bug(self):
        prompt = build_prompt(
            "p.Test::testCase", "1 | run();", "p.Error: bad\n\tat p.Test.testCase(Test.java:1)",
            "", {
                "entry_diagram_id": "D-001",
                "nodes": [self.graph_node("D-001")],
            }, 1,
        )
        self.assertNotIn("[Candidate Functions]", prompt)
        self.assertNotIn("[Project]", prompt)
        self.assertNotIn("[Bug]", prompt)
        self.assertNotIn("candidate", SYSTEM_PROMPT.lower())
        self.assertIn("[Sliced Failing-Test Code With Original Line Numbers]\n1 | run();", prompt)
        self.assertIn("[Error Stack]\np.Error: bad", prompt)
        self.assertIn("[Test Output]\n(empty)", prompt)

    def test_initial_prompt_identifies_preloaded_sequence_subgraph(self):
        prompt = build_prompt(
            "p.Test::testCase", "1 | run();", "p.Error: bad", "", {
                "entry_diagram_id": "D-001",
                "nodes": [self.graph_node(
                    "D-001", entry_signature="p.Service.run(int)",
                    visible_calls=5, visible_units=6, participants=3,
                )],
            }, 1,
        )
        self.assertIn("[Initial Sequence Subgraph]", prompt)
        self.assertIn("ID: `D-001`", prompt)
        self.assertIn("Entry: `p.Service.run(int)`", prompt)
        self.assertIn("Visible Units: 6", prompt)
        self.assertNotIn("PAGE", prompt)

    def test_initial_prompt_labels_complete_trace_diagram(self):
        prompt = build_prompt(
            "p.Test::testCase", "1 | run();", "p.Error: bad", "",
            {
                "strategy": "synthetic-root-adaptive-graph",
                "entry_diagram_id": "D-001",
                "nodes": [self.graph_node(
                    "D-001", entry_signature="execution root",
                    origin_test_line=0, visible_calls=7, visible_units=7,
                )],
            },
            1,
        )
        self.assertIn("[Failing-Test Code With Original Line Numbers]", prompt)
        self.assertIn("[Initial Sequence Subgraph]", prompt)

    def test_image_only_prompt_has_no_text_index_or_method_signature(self):
        prompt = build_prompt(
            "p.Test::testCase", "1 | run();", "p.Error: bad", "", {
                "entry_diagram_id": "D-001",
                "nodes": [self.graph_node(
                    "D-001", entry_signature="p.Service.run(int)",
                    signatures=["p.Service.run(int)"],
                )],
            }, 1, IMAGE_ONLY_MODE,
        )
        self.assertNotIn("p.Service.run", prompt)
        self.assertNotIn("ID: `D-001`", prompt)
        self.assertNotIn("Visible Units", prompt)
        self.assertNotIn("[Initial Sequence Subgraph]", prompt)
        self.assertNotIn("initial subgraph image is supplied", prompt.lower())
        self.assertIn("[Task Parameters]\nMaximum ranked methods: 1", prompt)
        self.assertNotIn('"ranked"', prompt)
        self.assertNotIn('"method_id"', prompt)

    def test_defect_context_schema_requires_stack_but_allows_empty_output(self):
        value = {
            "schema": "defect-context", "schema_version": 1,
            "test": "p.Test::testCase", "error_stack": "p.Error\n\tat p.Test.testCase",
            "test_output": "",
        }
        self.assertIs(validate_defect_context(value), value)
        value["error_stack"] = ""
        with self.assertRaisesRegex(ValueError, "error stack is unavailable"):
            validate_defect_context(value)

    def test_parses_only_strict_json_objects(self):
        self.assertEqual(parse_model_response('{"ranked": []}'), {"ranked": []})
        self.assertIsNone(parse_model_response('```json\n{"ranked": []}\n```'))
        self.assertIsNone(parse_model_response('result: {"ranked": []}'))
        self.assertIsNone(parse_model_response("not json"))

    def test_model_ranking_payload_accepts_up_to_top_k_entries(self):
        value = {"ranked": [
            {"signature": "p.A.run(int)", "reason": "evidence"},
        ]}
        self.assertEqual(validate_model_ranking_payload(value, 1), value["ranked"])
        self.assertEqual(validate_model_ranking_payload(value, 2), value["ranked"])
        with self.assertRaisesRegex(ValueError, "between 1 and 2"):
            validate_model_ranking_payload({"ranked": []}, 2)
        with self.assertRaisesRegex(ValueError, "between 1 and 1"):
            validate_model_ranking_payload({"ranked": value["ranked"] * 2}, 1)
        value["ranked"][0]["function"] = "p.A.run"
        with self.assertRaisesRegex(ValueError, "invalid model ranking entry"):
            validate_model_ranking_payload(value, 1)

        image_value = {"ranked": [
            {
                "method_id": "M001", "method_signature": "run(int)",
                "reason": "evidence",
            },
        ]}
        self.assertEqual(
            validate_model_ranking_payload(image_value, 1, IMAGE_ONLY_MODE),
            image_value["ranked"],
        )
        image_value["ranked"][0]["method_id"] = "C001"
        with self.assertRaisesRegex(ValueError, "invalid model ranking method_id"):
            validate_model_ranking_payload(image_value, 1, IMAGE_ONLY_MODE)

    def test_gates_and_deduplicates_candidates(self):
        ranking, dropped = gate_ranking([
            {"signature": "p.A.run(int)"}, {"signature": "p.A.run(int)"},
            {"signature": "p.A.run(Integer)"},
        ], ["p.A.run"], ["p.A.run(int)"], 5)
        self.assertEqual([item.function for item in ranking], ["p.A.run"])
        self.assertEqual([item.signature for item in ranking], ["p.A.run(int)"])
        self.assertEqual(dropped, ["p.A.run(Integer)"])

    def test_gates_image_methods_in_requested_match_priority(self):
        catalog = [
            {"method_id": "M001", "function": "p.A.run",
             "signature": "p.A.run(int)", "descriptor": "(I)V"},
            {"method_id": "M002", "function": "p.B.work",
             "signature": "p.B.work()", "descriptor": "()V"},
            {"method_id": "M003", "function": "p.C.add",
             "signature": "p.C.add(TickUnit)", "descriptor": "(Lx/TickUnit;)V"},
        ]
        ranking, dropped = gate_method_id_ranking([
            {
                "method_id": "M001", "method_signature": "run(int)",
                "reason": "both match",
            },
            {
                "method_id": "M002", "method_signature": "add(TickUnit)",
                "reason": "ID takes precedence over a conflicting signature",
            },
            {
                "method_id": "M999", "method_signature": "add(TickUnit)",
                "reason": "fall back to signature",
            },
            {
                "method_id": "M998", "method_signature": "missing()",
                "reason": "no match",
            },
        ], ["p.A.run", "p.B.work", "p.C.add"], catalog,
            ["M001", "M002", "M003"], 5)
        self.assertEqual(
            [item.function for item in ranking],
            ["p.A.run", "p.B.work", "p.C.add"],
        )
        self.assertEqual(dropped, ["M998"])

    def test_signature_only_fallback_rejects_ambiguous_or_unviewed_methods(self):
        catalog = [
            {"method_id": "M001", "function": "p.A.add",
             "signature": "p.A.add(TickUnit)", "descriptor": "(Lx/TickUnit;)V"},
            {"method_id": "M002", "function": "p.B.add",
             "signature": "p.B.add(TickUnit)", "descriptor": "(Lx/TickUnit;)V"},
            {"method_id": "M003", "function": "p.C.work",
             "signature": "p.C.work()", "descriptor": "()V"},
        ]
        ranking, dropped = gate_method_id_ranking([
            {"method_id": "M999", "method_signature": "add(TickUnit)"},
            {"method_id": "M998", "method_signature": "work()"},
        ], ["p.A.add", "p.B.add", "p.C.work"], catalog, ["M001", "M002"], 5)
        self.assertEqual(ranking, [])
        self.assertEqual(dropped, ["M999", "M998"])

    def test_schema_validation_rejects_duplicate_and_non_contiguous_values(self):
        with self.assertRaisesRegex(ValueError, "duplicate candidate"):
            validate_candidates({"schema": "fault-candidates", "schema_version": 1, "candidates": [
                {"function": "p.A.m"}, {"function": "p.A.m"},
            ]})
        with self.assertRaisesRegex(ValueError, "non-contiguous"):
            validate_localization({"schema": "fault-localization", "schema_version": 1,
                                   "ranking": [{"function": "p.A.m", "rank": 2}]})

    def test_disabled_summary_contract_rejects_populated_summary(self):
        with self.assertRaisesRegex(ValueError, "summary is not disabled"):
            validate_candidates({
                "schema": "fault-candidates", "schema_version": 1,
                "summary_generation": "disabled",
                "candidates": [{
                    "function": "p.A.m", "summary": "unexpected", "status": "OK",
                }],
            })

    def test_schema_validation_normalizes_invalid_rank_type(self):
        with self.assertRaisesRegex(ValueError, "invalid ranking rank"):
            validate_localization({
                "schema": "fault-localization",
                "schema_version": 1,
                "ranking": [{"function": "p.A.m", "rank": {"invalid": True}}],
            })

    def test_localization_v2_validates_diagram_audit_fields(self):
        value = {
            "schema": "fault-localization", "schema_version": 2,
            "diagram_count": 2, "tool_rounds": 1, "diagram_view_count": 2,
            "viewed_diagrams": ["L1-001"], "ranking": [],
        }
        self.assertIs(validate_localization(value), value)
        value["viewed_diagrams"] = ["L1-001", "L1-001"]
        with self.assertRaisesRegex(ValueError, "duplicate viewed"):
            validate_localization(value)

    def test_localization_v3_requires_matching_full_signature(self):
        value = {
            "schema": "fault-localization", "schema_version": 3,
            "diagram_count": 1, "tool_rounds": 1, "diagram_view_count": 1,
            "viewed_diagrams": ["L1-001"],
            "ranking": [{
                "function": "p.A.run", "signature": "p.A.run(int)", "rank": 1,
            }],
        }
        self.assertIs(validate_localization(value), value)
        value["ranking"][0]["signature"] = "p.A.other(int)"
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_localization(value)

    def test_localization_v3_validates_image_only_method_id_audit(self):
        value = {
            "schema": "fault-localization", "schema_version": 3,
            "interaction_mode": IMAGE_ONLY_MODE,
            "diagram_count": 1, "tool_rounds": 0, "diagram_view_count": 1,
            "viewed_diagrams": ["D-001"],
            "returned_method_ids": ["M001"],
            "dropped_invalid_method_ids": [],
            "ranking": [{
                "function": "p.A.run", "signature": "p.A.run(int)", "rank": 1,
            }],
        }
        self.assertIs(validate_localization(value), value)
        value["returned_method_ids"] = ["C001"]
        with self.assertRaisesRegex(ValueError, "method ID audit"):
            validate_localization(value)

    def test_localization_v4_requires_unique_buggy_source_ranges(self):
        value = {
            "schema": "fault-localization", "schema_version": 4,
            "interaction_mode": IMAGE_ONLY_MODE,
            "diagram_count": 1, "tool_rounds": 0, "diagram_view_count": 1,
            "viewed_diagrams": ["D-001"],
            "returned_method_ids": ["M001", "M002"],
            "dropped_invalid_method_ids": [],
            "dropped_unresolved_source_methods": [],
            "ranking": [
                {"function": "p.A.run", "signature": "p.A.run(int)", "rank": 1,
                 "source_file": "src/p/A.java", "start_line": 3, "end_line": 3},
                {"function": "p.A.run", "signature": "p.A.run(String)", "rank": 2,
                 "source_file": "src/p/A.java", "start_line": 4, "end_line": 4},
            ],
        }
        self.assertIs(validate_localization(value), value)
        value["ranking"][1]["start_line"] = 3
        value["ranking"][1]["end_line"] = 3
        with self.assertRaisesRegex(ValueError, "duplicate ranking source location"):
            validate_localization(value)

    def test_test_code_requires_and_uses_slice(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory))
            trigger = layout.trigger_dir("P", "1", 1)
            trigger.mkdir(parents=True)
            workspace_source = layout.workspace_dir("P", "1") / "src" / "p" / "Test.java"
            workspace_source.parent.mkdir(parents=True)
            workspace_source.write_text(
                "package p; class Test { void testCase() { int ignored = 0; run(); } }",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FileNotFoundError, "test slice not found"):
                test_code_context(layout, "P", "1", trigger, "p.Test::testCase")
            metadata = {
                "schema": "test-boundary-slice", "schema_version": 2,
                "applied": True, "selected_statements": [{
                    "kind": "statement", "start_line": 42, "end_line": 42,
                    "definitions": [], "references": [], "code": "run();",
                }],
            }
            (trigger / "test_slice.json").write_text(
                json.dumps(metadata), encoding="utf-8"
            )
            sliced = test_code_context(layout, "P", "1", trigger, "p.Test::testCase")
            self.assertEqual(sliced, "   42 | run();")
            self.assertNotIn("ignored", sliced)

    def test_test_code_falls_back_to_full_method_when_test_was_not_entered(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory))
            trigger = layout.trigger_dir("P", "1", 1)
            trigger.mkdir(parents=True)
            workspace_source = layout.workspace_dir("P", "1") / "src" / "p" / "Test.java"
            workspace_source.parent.mkdir(parents=True)
            workspace_source.write_text(
                "package p;\nclass Test {\n"
                "  void testCase() {\n    run();\n  }\n}\n",
                encoding="utf-8",
            )
            metadata = {
                "schema": "test-boundary-slice", "schema_version": 2,
                "applied": False, "selected_statements": [],
            }
            (trigger / "test_slice.json").write_text(
                json.dumps(metadata), encoding="utf-8"
            )

            code = test_code_context(layout, "P", "1", trigger, "p.Test::testCase")

            self.assertIn("    3 | void testCase() {", code)
            self.assertIn("    4 |     run();", code)

    def test_defect_context_requires_stack_and_allows_empty_test_output(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory))
            trigger = layout.trigger_dir("P", "1", 1)
            trigger.mkdir(parents=True)
            trace_log = layout.stage_log_dir("trace", "P", "1", 1) / "trace.stdout.log"
            trace_log.parent.mkdir(parents=True)
            trace_log.write_text(
                "test failed\np.Error: bad\n\tat p.Test.testCase(Test.java:42)\n"
                "Run count: 1\nFailure count: 1\n",
                encoding="utf-8",
            )
            stack, output = defect_output_context(
                layout, "P", "1", "1", trigger, "p.Test::testCase"
            )
            self.assertEqual(stack, "p.Error: bad\n\tat p.Test.testCase(Test.java:42)")
            self.assertEqual(output, "")

    def test_extract_error_stack_rejects_output_without_frames(self):
        self.assertEqual(extract_error_stack("Run count: 1\nFailure count: 0\n"), "")

    @patch("mllmfl.stages.localize._post_response")
    def test_agent_views_selected_diagram_then_returns_final_ranking(self, post):
        post.side_effect = [
            self.agent_response({"content": (
                "The current path suggests setup-state inconsistency, so I will inspect "
                "the linked setup subgraph next."
            ), "tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": "view_sequence_diagram",
                    "arguments": '{"diagram_id":"D-002"}',
                },
            }]}, "resp-1"),
            self.agent_response(
                {"content": (
                    '{"ranked":[{"signature":"p.Service.run()",'
                    '"reason":"evidence"}]}'
                )},
                "resp-2",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entry.png").write_bytes(b"png")
            (root / "segment.png").write_bytes(b"png")
            conversation = root / "conversation.jsonl"
            index = {
                "entry_diagram_id": "D-001",
                "nodes": [
                    self.graph_node("D-001", image="entry.png", links=[{
                        "direction": "TO", "diagram_id": "D-002",
                        "relation": "EXPAND_CALL", "invocation_ids": [2],
                    }]),
                    self.graph_node(
                        "D-002", signatures=["p.Service.run()", "p.Helper.work(int)"],
                        links=[{
                            "direction": "FROM", "diagram_id": "D-001",
                            "relation": "EXPAND_CALL", "invocation_ids": [2],
                        }],
                    ),
                ],
            }
            raw, model, viewed, rounds, views = run_agent(
                {}, "prompt", index, root, 30, conversation
            )
            conversation_text = conversation.read_text()
        self.assertIn("p.Service.run", raw)
        self.assertEqual(model, "vision")
        self.assertEqual(viewed, ["D-001", "D-002"])
        self.assertEqual((rounds, views), (1, 2))
        initial_content = post.call_args_list[0].args[1][0]["content"]
        self.assertEqual(
            [part["type"] for part in initial_content],
            ["text", "text", "image_url"],
        )
        second_input = post.call_args_list[1].args[1]
        self.assertEqual(second_input[-1]["role"], "user")
        self.assertEqual(second_input[-1]["content"][1]["type"], "image_url")
        tool_output = second_input[-2]
        self.assertIn("p.Helper.work(int)", second_input[-1]["content"][0]["text"])
        self.assertIn("Visible Units: 2 | Participants: 2", second_input[-1]["content"][0]["text"])
        self.assertEqual(tool_output["role"], "tool")
        self.assertEqual(json.loads(tool_output["content"])["origin_test_line"], 42)
        self.assertEqual(json.loads(tool_output["content"])["visible_calls"], 2)
        self.assertEqual(
            [item.get("type") or item.get("role") for item in second_input],
            ["user", "message", "function_call", "tool", "user"],
        )
        records = [json.loads(line) for line in conversation_text.splitlines()]
        self.assertEqual(
            [record.get("type") or record.get("role") for record in records],
            [
                "system", "user", "assistant", "tool", "user", "assistant",
            ],
        )
        self.assertIn("setup-state inconsistency", records[2]["content"])
        self.assertEqual(records[4]["content"][1], {
            "type": "image_ref", "diagram_id": "D-002",
        })
        self.assertEqual(post.call_count, 2)
        self.assertIsNone(
            post.call_args_list[0].kwargs["previous_response_id"]
        )
        self.assertIsNone(post.call_args_list[1].kwargs["previous_response_id"])
        self.assertNotIn("json_output", post.call_args_list[1].kwargs)
        self.assertNotIn("enable_tools", post.call_args_list[1].kwargs)
        self.assertNotIn("data:image", conversation_text)

    @patch("mllmfl.stages.localize._post_response")
    def test_agent_replays_complete_local_history_every_turn(self, post):
        post.side_effect = [
            self.agent_response({"content": None, "reasoning_content": "reasoning-1",
                                 "tool_calls": [{
                "id": "call-1", "function": {
                    "name": "view_sequence_diagram",
                    "arguments": '{"diagram_id":"D-002"}',
                },
            }]}, "resp-1", {
                "input_tokens_details": {"cached_tokens": 0},
            }),
            self.agent_response({"content": None, "reasoning_content": "reasoning-2",
                                 "tool_calls": [{
                "id": "call-2", "function": {
                    "name": "view_sequence_diagram",
                    "arguments": '{"diagram_id":"D-003"}',
                },
            }]}, "resp-2", {
                "input_tokens_details": {"cached_tokens": 1024},
            }),
            self.agent_response({"content": (
                '{"ranked":[{"method_id":"M001",'
                '"method_signature":"run()","reason":"evidence"}]}'
            ),
                                 "reasoning_content": "reasoning-3"}, "resp-3", {
                "input_tokens_details": {"cached_tokens": 2048},
            }),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entry.png").write_bytes(b"entry")
            (root / "segment.png").write_bytes(b"segment")
            index = {
                "entry_diagram_id": "D-001",
                "nodes": [
                    self.graph_node("D-001", image="entry.png", links=[{
                        "direction": "TO", "diagram_id": "D-002",
                        "relation": "EXPAND_CALL", "invocation_ids": [2],
                    }]),
                    self.graph_node("D-002", image="segment.png", links=[{
                        "direction": "TO", "diagram_id": "D-003",
                        "relation": "EXPAND_CALL", "invocation_ids": [3],
                    }]),
                    self.graph_node("D-003", image="entry.png"),
                ],
            }

            conversation = root / "conversation.jsonl"
            raw, _, viewed, rounds, views = run_agent(
                {"mllm": {"interaction_mode": IMAGE_ONLY_MODE}},
                "prompt", index, root, 30, conversation,
            )
            conversation_text = conversation.read_text()
            usage_records = [
                json.loads(line)
                for line in (root / "response_usage.jsonl").read_text().splitlines()
            ]

        self.assertIn('"method_id":"M001"', raw)
        self.assertEqual(
            (viewed, rounds, views), (["D-001", "D-002", "D-003"], 2, 3)
        )
        replay = post.call_args_list[1].args[1]
        self.assertEqual(
            [item.get("type") or item.get("role") for item in replay],
            ["user", "reasoning", "function_call", "tool", "user"],
        )
        self.assertEqual(
            [part["type"] for part in replay[-1]["content"]], ["image_url"]
        )
        repeated_replay = post.call_args_list[2].args[1]
        self.assertEqual(
            [item.get("type") or item.get("role") for item in repeated_replay],
            [
                "user", "reasoning", "function_call", "tool", "user",
                "reasoning", "function_call", "tool", "user",
            ],
        )
        self.assertIsNone(
            post.call_args_list[0].kwargs["previous_response_id"]
        )
        self.assertEqual(
            [call.kwargs["previous_response_id"] for call in post.call_args_list[1:]],
            [None, None],
        )
        self.assertEqual(
            [
                item["usage"]["input_tokens_details"]["cached_tokens"]
                for item in usage_records
            ],
            [0, 1024, 2048],
        )
        self.assertIn('"reasoning_content": "reasoning-1"', conversation_text)
        self.assertIn('"type": "image_ref"', conversation_text)
        self.assertNotIn("data:image", conversation_text)

    @patch("mllmfl.stages.localize._post_response")
    def test_image_only_agent_sends_images_without_text_metadata(self, post):
        post.side_effect = [
            self.agent_response({"content": None, "tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": "view_sequence_diagram",
                    "arguments": '{"diagram_id":"D-002"}',
                },
            }]}, "resp-1"),
            self.agent_response({"content": (
                '{"ranked":[{"method_id":"M002",'
                '"method_signature":"work(int)","reason":"e"}]}'
            )}, "resp-2"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entry.png").write_bytes(b"png")
            (root / "segment.png").write_bytes(b"png")
            index = {
                "entry_diagram_id": "D-001",
                "nodes": [
                    self.graph_node("D-001", image="entry.png", links=[{
                        "direction": "TO", "diagram_id": "D-002",
                        "relation": "EXPAND_CALL", "invocation_ids": [2],
                    }]),
                    self.graph_node("D-002", signatures=["p.Helper.work(int)"],
                                    entry_signature="p.Helper.work(int)", links=[{
                        "direction": "FROM", "diagram_id": "D-001",
                        "relation": "EXPAND_CALL", "invocation_ids": [2],
                    }]),
                ],
            }
            raw, _, viewed, _, _ = run_agent(
                {"mllm": {"interaction_mode": IMAGE_ONLY_MODE}},
                "prompt", index, root, 30,
            )
        self.assertIn("M002", raw)
        self.assertEqual(viewed, ["D-001", "D-002"])
        initial_content = post.call_args_list[0].args[1][0]["content"]
        self.assertEqual(
            [part["type"] for part in initial_content],
            ["text", "image_url"],
        )
        second_input = post.call_args_list[1].args[1]
        self.assertEqual(json.loads(second_input[-2]["content"]), {
            "ok": True, "diagram_id": "D-002",
        })
        self.assertEqual(
            [part["type"] for part in second_input[-1]["content"]],
            ["image_url"],
        )

    @patch("mllmfl.stages.localize._post_response")
    def test_agent_retries_invalid_final_json_with_stateless_history(self, post):
        post.side_effect = [
            self.agent_response({"content": "analysis complete"}, "resp-1"),
            self.agent_response({"content": (
                '{"ranked":[{"signature":"p.Service.run()",'
                '"reason":"row ("S1")"}]}'
            )}, "resp-2"),
            self.agent_response({"content": (
                '{"ranked":[{"signature":"p.Service.run()",'
                '"reason":"row S1"}]}'
            )}, "resp-3"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entry.png").write_bytes(b"png")
            conversation = root / "conversation.jsonl"
            raw, _, viewed, rounds, views = run_agent(
                {}, "prompt", {
                    "entry_diagram_id": "D-001",
                    "nodes": [self.graph_node("D-001", image="entry.png")],
                }, root, 30, conversation,
            )
            records = [
                json.loads(line) for line in conversation.read_text().splitlines()
            ]

        self.assertIn('"reason":"row S1"', raw)
        self.assertEqual((viewed, rounds, views), (["D-001"], 0, 1))
        self.assertEqual(
            [record["role"] for record in records],
            [
                "system", "user", "assistant", "user", "assistant", "user",
                "assistant",
            ],
        )
        self.assertIn("response is not valid JSON", records[3]["content"])
        self.assertIn("response is not valid JSON", records[5]["content"])
        self.assertIn("correctly JSON-escaped", records[5]["content"])
        retry_input = post.call_args_list[2].args[1]
        self.assertEqual(
            [item["role"] for item in retry_input],
            ["user", "assistant", "user", "assistant", "user"],
        )
        self.assertIsNone(post.call_args_list[2].kwargs["previous_response_id"])
        self.assertTrue(all(
            "json_output" not in call.kwargs and "enable_tools" not in call.kwargs
            for call in post.call_args_list
        ))

    @patch("mllmfl.stages.localize._post_response")
    def test_agent_bounds_invalid_final_json_retries(self, post):
        post.side_effect = [
            self.agent_response({"content": "analysis complete"}, "resp-1"),
            self.agent_response({"content": "not json one"}, "resp-2"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entry.png").write_bytes(b"png")
            raw, _, _, _, _ = run_agent(
                {"mllm": {"invalid_final_json_retries": 1}},
                "prompt", {
                    "entry_diagram_id": "D-001",
                    "nodes": [self.graph_node("D-001", image="entry.png")],
                }, root, 30,
            )
        self.assertEqual(raw, "not json one")
        self.assertEqual(post.call_count, 2)

    @patch("mllmfl.stages.localize._post_response")
    def test_agent_retries_final_json_that_violates_effective_top_k(self, post):
        two_entries = (
            '{"ranked":['
            '{"signature":"p.A.one()","reason":"one"},'
            '{"signature":"p.B.two()","reason":"two"}'
            ']}'
        )
        post.side_effect = [
            self.agent_response({"content": "analysis complete"}, "resp-1"),
            self.agent_response({"content": two_entries}, "resp-2"),
            self.agent_response({"content": (
                '{"ranked":[{"signature":"p.A.one()",'
                '"reason":"one"}]}'
            )}, "resp-3"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entry.png").write_bytes(b"png")
            raw, _, _, _, _ = run_agent(
                {"mllm": {"top_k": 5}},
                "prompt", {
                    "entry_diagram_id": "D-001",
                    "nodes": [self.graph_node("D-001", image="entry.png")],
                }, root, 30, top_k=1,
            )
        self.assertIn('"signature":"p.A.one()"', raw)
        retry_input = post.call_args_list[2].args[1]
        self.assertIn("between 1 and 1", retry_input[-1]["content"])

    def test_agent_rejects_invalid_final_json_retry_limit(self):
        graph = {
            "entry_diagram_id": "D-001",
            "nodes": [self.graph_node("D-001", image="entry.png")],
        }
        with self.assertRaisesRegex(
            ValueError, "invalid_final_json_retries must be non-negative"
        ):
            run_agent(
                {"mllm": {"invalid_final_json_retries": -1}},
                "prompt", graph, Path("."), 30,
            )

    @patch("mllmfl.stages.localize._post_response")
    def test_agent_returns_recoverable_unknown_diagram_error(self, post):
        post.side_effect = [
            self.agent_response({"tool_calls": [{"id": "bad", "function": {
                "name": "view_sequence_diagram",
                "arguments": '{"diagram_id":"missing"}',
            }}]}, "resp-1"),
            self.agent_response({"content": (
                '{"ranked":[{"signature":"p.Service.run()",'
                '"reason":"evidence"}]}'
            )}, "resp-2"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entry.png").write_bytes(b"png")
            raw, _, viewed, rounds, views = run_agent(
                {}, "prompt", {
                    "entry_diagram_id": "D-001",
                    "nodes": [self.graph_node("D-001", image="entry.png")],
                }, root, 30,
            )
        self.assertIn('"signature":"p.Service.run()"', raw)
        self.assertEqual((viewed, rounds, views), (["D-001"], 1, 1))
        second_input = post.call_args_list[1].args[1]
        self.assertIn("unknown diagram_id", second_input[-1]["content"])

    @patch("mllmfl.stages.localize._post_response")
    def test_agent_keeps_only_first_tool_call_without_error(self, post):
        post.side_effect = [
            self.agent_response({"tool_calls": [
                {"id": "one", "function": {"name": "view_sequence_diagram",
                 "arguments": '{"diagram_id":"L1-001"}'}},
                {"id": "two", "function": {"name": "view_sequence_diagram",
                 "arguments": '{"diagram_id":"L1-002"}'}},
                {"id": "three", "function": {"name": "view_sequence_diagram",
                 "arguments": '{"diagram_id":"L1-003"}'}},
                {"id": "four", "function": {"name": "view_sequence_diagram",
                 "arguments": '{"diagram_id":"L1-004"}'}},
            ]}, "resp-1"),
            self.agent_response({"content": (
                '{"ranked":[{"signature":"p.A.one()",'
                '"reason":"evidence"}]}'
            )}, "resp-2"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("entry.png", "one.png", "two.png", "three.png", "four.png"):
                (root / name).write_bytes(b"png")
            links = [
                {"direction": "TO", "diagram_id": f"L1-00{index}",
                 "relation": "EXPAND_CALL", "invocation_ids": [index + 1]}
                for index in range(1, 5)
            ]
            index = {
                "entry_diagram_id": "D-001",
                "nodes": [
                    self.graph_node(
                        "D-001", image="entry.png", links=links
                    ),
                    *[
                        self.graph_node(
                            f"L1-00{index}",
                            image=f"{name}.png",
                            entry_signature=f"p.A.{name}()",
                            signatures=[f"p.A.{name}()"],
                        )
                        for index, name in enumerate(
                            ("one", "two", "three", "four"), 1
                        )
                    ],
                ],
            }
            conversation = root / "conversation.jsonl"
            _, _, viewed, rounds, views = run_agent(
                {}, "prompt", index, root, 30, conversation
            )
            records = [
                json.loads(line) for line in conversation.read_text().splitlines()
            ]
        self.assertEqual(viewed, ["D-001", "L1-001"])
        self.assertEqual((rounds, views), (1, 2))
        image_message = post.call_args_list[1].args[1][-1]
        self.assertEqual(
            [part["type"] for part in image_message["content"]],
            ["text", "image_url"],
        )
        tool_results = [
            item for item in post.call_args_list[1].args[1]
            if item.get("role") == "tool"
        ]
        self.assertEqual(len(tool_results), 1)
        self.assertTrue(json.loads(tool_results[0]["content"])["ok"])
        self.assertIsNone(post.call_args_list[1].kwargs["previous_response_id"])
        saved_assistant = next(record for record in records if record.get("tool_calls"))
        self.assertEqual(
            [call["id"] for call in saved_assistant["tool_calls"]],
            ["one"],
        )

    @patch("mllmfl.stages.localize.time.sleep")
    @patch("mllmfl.stages.localize.requests.post")
    @patch.dict("os.environ", {"TEST_API_KEY": "secret"}, clear=False)
    def test_responses_request_retries_http_failure(self, post, sleep):
        failed = unittest.mock.Mock(status_code=500, text="temporary")
        succeeded = unittest.mock.Mock(status_code=200)
        succeeded.json.return_value = {
            "id": "resp-1",
            "model": "vision",
            "status": "completed",
            "output": [{
                "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "done"}],
            }],
        }
        post.side_effect = [failed, succeeded]
        message, model, response_id, response_output, usage = _post_response({"mllm": {
            "api_key_env": "TEST_API_KEY", "vision_model": "vision",
            "retry": {"max_retries": 2, "backoff_s": 0},
        }}, [], 30)
        self.assertEqual(message["content"], "done")
        self.assertEqual(model, "vision")
        self.assertEqual(response_id, "resp-1")
        self.assertEqual([item["type"] for item in response_output], ["message"])
        self.assertEqual(usage, {})
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()
        payload = post.call_args.kwargs["json"]
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertNotIn("response_format", payload)
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["reasoning"], {"effort": "medium"})
        self.assertFalse(payload["store"])
        self.assertEqual(payload["include"], ["reasoning.encrypted_content"])
        self.assertIsInstance(payload["instructions"], str)
        self.assertTrue(payload["tools"][0]["name"])
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertTrue(post.call_args.args[0].endswith("/responses"))

    @patch.dict("os.environ", {"TEST_API_KEY": "secret"}, clear=False)
    def test_response_request_rejects_invalid_reasoning_effort(self):
        base = {"api_key_env": "TEST_API_KEY", "vision_model": "vision"}
        with self.assertRaisesRegex(ValueError, "reasoning_effort must be"):
            _post_response({"mllm": {**base, "reasoning_effort": "maximum"}}, [], 30)

    @patch("mllmfl.stages.localize.requests.post")
    @patch.dict("os.environ", {"TEST_API_KEY": "secret"}, clear=False)
    def test_responses_normalizes_reasoning_tools_and_image_input(self, post):
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = {
            "id": "resp-2",
            "status": "completed",
            "output": [
                {"type": "reasoning", "id": "reason-1", "summary": [{
                    "type": "summary_text",
                    "text": "Inspect the linked setup path.",
                }]},
                {"type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": (
                     "The current evidence points to setup, so I will inspect its linked "
                     "subgraph next."
                 )}]},
                {"type": "function_call", "id": "fc-1", "call_id": "call-1",
                 "name": "view_sequence_diagram", "arguments": "{}"},
            ],
        }
        post.return_value = response
        message, _, _, response_output, usage = _post_response({"mllm": {
            "api_key_env": "TEST_API_KEY", "vision_model": "vision",
            "reasoning_effort": "medium",
        }}, [{"role": "user", "content": [{"type": "image_url",
              "image_url": {"url": "data:image/png;base64,eA=="}}]}], 30)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["reasoning"], {"effort": "medium"})
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["input"][0]["content"][0], {
            "type": "input_image", "image_url": "data:image/png;base64,eA==",
        })
        self.assertEqual(
            [item["type"] for item in response_output],
            ["reasoning", "message", "function_call"],
        )
        self.assertEqual(usage, {})
        self.assertEqual(message["tool_calls"][0]["id"], "call-1")
        self.assertIn("current evidence points to setup", message["content"])

    @patch("mllmfl.stages.localize.requests.post")
    @patch.dict("os.environ", {"OPENAI_API_KEY": "secret"}, clear=False)
    def test_responses_uses_gpt_5_5_stateless_defaults(self, post):
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = {
            "id": "response-1",
            "status": "completed",
            "output": [{
                "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "done"}],
            }],
        }
        post.return_value = response

        _, model, _, _, _ = _post_response(
            {}, [{"role": "user", "content": "next"}], 30,
            previous_response_id="response-0",
        )

        payload = post.call_args.kwargs["json"]
        self.assertEqual(model, "gpt-5.5")
        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertEqual(payload["reasoning"], {"effort": "medium"})
        self.assertNotIn("previous_response_id", payload)
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["include"], ["reasoning.encrypted_content"])
        self.assertEqual(
            post.call_args.args[0],
            "https://api.openai.com/v1/responses",
        )


class UMLIndexTests(unittest.TestCase):
    def _index(self):
        return {
            "schema": "execution-uml-index", "schema_version": 2,
            "source_schema": "fullchain-execution",
            "strategy": "test-root-direct-invocation-subtrees",
            "slice_applied": True,
            "test": {"class": "p.Test", "method": "testCase"},
            "root_invocation_id": 1, "source_call_count": 1,
            "partitioned_call_count": 1, "excluded_call_count": 0,
            "segment_count": 1, "segments": [{
                "diagram_id": "L1-001-inv-2", "ordinal": 1, "invocation_id": 2,
                "function": "p.Service.run", "descriptor": "()V", "signature": "run()",
                "method_signatures": ["p.Service.run()"],
                "origin_test_line": 4, "enter_seq": 3, "exit_seq": 4,
                "exit_type": "RETURN",
                "call_count": 1, "displayed_call_count": 1,
                "puml": "sequence_diagrams/one.puml",
                "image": "sequence_diagrams/one.png",
            }],
        }

    def _graph_index(self):
        return {
            "schema": "execution-uml-graph", "schema_version": 1,
            "source_schema": "fullchain-execution",
            "strategy": "test-root-adaptive-graph",
            "entry_reason": "test_invocation",
            "slice_applied": True,
            "test": {"class": "p.Test", "method": "testCase"},
            "root_invocation_id": 1,
            "max_visible_units": 24,
            "max_participants_per_image": 8,
            "trace_call_count": 1,
            "layout_root_call_count": 0,
            "source_call_count": 1,
            "partitioned_call_count": 1,
            "excluded_call_count": 0,
            "entry_diagram_id": "D-001",
            "node_count": 1,
            "diagram_count": 1,
            "nodes": [{
                "diagram_id": "D-001",
                "focus_invocation_id": 1,
                "entry_signature": "p.Test.testCase()",
                "origin_test_line": 4,
                "represented_call_count": 1,
                "visible_call_count": 1,
                "visible_unit_count": 1,
                "participant_count": 2,
                "method_signatures": ["p.Test.testCase()", "p.Service.run()"],
                "folds": [],
                "links": [],
                "puml": "sequence_diagrams/D-001.puml",
                "image": "sequence_diagrams/D-001.png",
            }],
        }

    def test_validates_uniform_graph_and_rejects_legacy_page_fields(self):
        value = self._graph_index()
        self.assertIs(validate_uml_index(value), value)
        value["nodes"][0]["node_type"] = "PAGE"
        with self.assertRaisesRegex(ValueError, "legacy GROUP/PAGE"):
            validate_uml_index(value)

    def test_graph_requires_exact_represented_call_partition(self):
        value = self._graph_index()
        value["nodes"][0]["represented_call_count"] = 0
        with self.assertRaisesRegex(ValueError, "partition represented calls"):
            validate_uml_index(value)

    def test_validates_image_only_graph_method_catalog_and_call_ids(self):
        value = self._graph_index()
        value.update({
            "schema_version": 2,
            "interaction_mode": IMAGE_ONLY_MODE,
            "method_catalog": [{
                "method_id": "M001", "function": "p.Service.run",
                "signature": "p.Service.run()", "descriptor": "()V",
            }],
        })
        value["nodes"][0]["method_ids"] = ["M001"]
        self.assertIs(validate_uml_index(value), value)
        value["nodes"][0]["method_ids"] = ["M999"]
        with self.assertRaisesRegex(ValueError, "invalid UML graph method IDs"):
            validate_uml_index(value)

    def test_validates_files_and_rejects_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment_dir = root / "sequence_diagrams"
            segment_dir.mkdir()
            (segment_dir / "one.puml").write_text("@startuml\n@enduml\n", encoding="utf-8")
            (segment_dir / "one.png").write_bytes(b"png")
            value = self._index()
            self.assertIs(validate_uml_index(value, root), value)
            value["segments"][0]["image"] = "../outside.png"
            with self.assertRaisesRegex(ValueError, "unsafe"):
                validate_uml_index(value, root)

    def test_rejects_duplicate_out_of_order_and_inconsistent_counts(self):
        value = self._index()
        duplicate = dict(value["segments"][0])
        duplicate.update({"ordinal": 2, "invocation_id": 3, "enter_seq": 2})
        value["segments"].append(duplicate)
        value["segment_count"] = 2
        value["source_call_count"] = 2
        value["partitioned_call_count"] = 2
        with self.assertRaisesRegex(ValueError, "duplicate UML segment diagram_id"):
            validate_uml_index(value)
        value["segments"][1]["diagram_id"] = "L1-002-inv-3"
        with self.assertRaisesRegex(ValueError, "out-of-order"):
            validate_uml_index(value)
        value["segments"][1]["enter_seq"] = 4
        value["partitioned_call_count"] = 1
        with self.assertRaisesRegex(ValueError, "inconsistent UML index call counts"):
            validate_uml_index(value)

    def test_validates_complete_trace_single_diagram_strategy(self):
        value = self._index()
        value.update({
            "strategy": "complete-trace-single-diagram",
            "slice_applied": False,
            "root_invocation_id": 0,
        })
        self.assertIs(validate_uml_index(value), value)
        value["slice_applied"] = True
        with self.assertRaisesRegex(ValueError, "complete-trace"):
            validate_uml_index(value)

    def test_validates_paired_recursive_page_references(self):
        common = {
            "node_type": "PAGE", "parent_id": None, "children": [],
            "function": "p.Service.run", "signature": "run()",
            "entry_signature": "p.Service.run()", "repeat_count": 1,
            "origin_test_line": 4, "exit_type": "RETURN",
            "call_count": 1, "displayed_call_count": 1,
            "participant_count": 2, "method_signatures": ["p.Service.run()"],
        }
        value = {
            "schema": "execution-uml-index", "schema_version": 4,
            "source_schema": "fullchain-execution",
            "strategy": "test-root-recursive-invocation-pages",
            "slice_applied": True,
            "test": {"class": "p.Test", "method": "testCase"},
            "root_invocation_id": 1,
            "max_calls_per_image": 24, "max_participants_per_image": 8,
            "source_call_count": 2, "partitioned_call_count": 2,
            "excluded_call_count": 0, "node_count": 2,
            "page_count": 2, "group_count": 0,
            "root_ids": ["L1-001-M001", "L1-002-M002"],
            "nodes": [
                {
                    **common, "diagram_id": "L1-001-M001", "invocation_id": 2,
                    "enter_seq": 3, "exit_seq": 4,
                    "puml": "sequence_diagrams/one.puml",
                    "image": "sequence_diagrams/one.png",
                    "references": [{
                        "direction": "TO", "diagram_id": "L1-002-M002",
                        "invocation_id": 3, "message_id": "M002",
                        "relation": "NEXT_SIBLING",
                    }],
                },
                {
                    **common, "diagram_id": "L1-002-M002", "invocation_id": 3,
                    "enter_seq": 5, "exit_seq": 6,
                    "puml": "sequence_diagrams/two.puml",
                    "image": "sequence_diagrams/two.png",
                    "references": [{
                        "direction": "FROM", "diagram_id": "L1-001-M001",
                        "invocation_id": 3, "message_id": "M002",
                        "relation": "NEXT_SIBLING",
                    }],
                },
            ],
        }
        self.assertIs(validate_uml_index(value), value)
        value["nodes"][1]["references"] = []
        with self.assertRaisesRegex(ValueError, "unpaired"):
            validate_uml_index(value)


class AggregateTests(unittest.TestCase):
    def test_frequency_only_ranking_preserves_natural_order_for_ties(self):
        results = [
            {"trigger": "1", "ranking": [
                {"rank": 1, "function": "A"},
                {"rank": 2, "function": "B"},
                {"rank": 3, "function": "C"},
            ]},
            {"trigger": "2", "ranking": [
                {"rank": 1, "function": "D"},
                {"rank": 2, "function": "B"},
                {"rank": 3, "function": "E"},
            ]},
        ]
        ranking = aggregate_rankings(results, 5)
        self.assertEqual([item["function"] for item in ranking], ["B", "A", "C", "D", "E"])
        self.assertEqual(ranking[0]["trigger_support"], 2)
        self.assertNotIn("average_rank", ranking[0])
        self.assertNotIn("best_rank", ranking[0])
        self.assertNotIn("reciprocal_rank_sum", ranking[0])

    def test_rejects_non_positive_top_k(self):
        with self.assertRaisesRegex(ValueError, "top_k must be positive"):
            aggregate_rankings([], 0)

    def test_source_range_aggregation_keeps_overloads_separate(self):
        results = [{"ranking": [
            {"function": "p.A.run", "signature": "p.A.run(int)", "rank": 1,
             "source_file": "src/p/A.java", "start_line": 3, "end_line": 3},
            {"function": "p.A.run", "signature": "p.A.run(String)", "rank": 2,
             "source_file": "src/p/A.java", "start_line": 4, "end_line": 4},
        ]}]
        ranking = aggregate_rankings(results, 5, use_source_ranges=True)
        self.assertEqual(len(ranking), 2)
        self.assertEqual([item["start_line"] for item in ranking], [3, 4])
        self.assertEqual([item["rank"] for item in ranking], [1, 2])

    def test_aggregate_stage_writes_v2_for_localization_v4(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory))
            trigger = layout.trigger_dir("P", "1", 1)
            trigger.mkdir(parents=True)
            (trigger / "localization.json").write_text(json.dumps({
                "schema": "fault-localization", "schema_version": 4,
                "project": "P", "bug": "1", "trigger": "1",
                "status": "OK", "model": "test", "interaction_mode": "image_only",
                "candidate_count": 1, "diagram_count": 1, "tool_rounds": 0,
                "diagram_view_count": 1, "viewed_diagrams": ["D-001"],
                "returned_method_ids": ["M001", "M002"],
                "dropped_invalid_method_ids": [], "dropped_invalid_signatures": [],
                "dropped_unresolved_source_methods": [],
                "ranking": [
                    {"function": "p.A.run", "signature": "p.A.run(int)", "rank": 1,
                     "reason": "one", "source_file": "src/p/A.java",
                     "start_line": 3, "end_line": 3},
                    {"function": "p.A.run", "signature": "p.A.run(String)", "rank": 2,
                     "reason": "two", "source_file": "src/p/A.java",
                     "start_line": 4, "end_line": 4},
                ],
            }), encoding="utf-8")
            aggregate.run(layout, ["P"], {"1"}, 5)
            output = json.loads(
                (layout.summaries / "P/bug_1.json").read_text()
            )
        self.assertEqual(output["schema_version"], 2)
        self.assertEqual(len(output["ranking"]), 2)


class CollectTests(unittest.TestCase):
    @patch("mllmfl.stages.collect.random.sample", return_value=[3, 0, 2])
    def test_randomly_limits_failing_tests_and_preserves_export_order(self, sample_mock):
        tests = ["p.T::one", "p.T::two", "p.T::three", "p.T::four"]
        selected = collect.select_failing_tests(tests, 3)
        self.assertEqual(selected, ["p.T::one", "p.T::three", "p.T::four"])
        sample_mock.assert_called_once_with(range(4), 3)

    def test_failing_test_limit_keeps_short_input_and_rejects_non_positive_limit(self):
        tests = ["p.T::one", "p.T::two"]
        self.assertEqual(collect.select_failing_tests(tests, 3), tests)
        with self.assertRaisesRegex(ValueError, "maximum failing tests must be positive"):
            collect.select_failing_tests(tests, 0)

    def test_trigger_preparation_removes_unselected_and_changed_cached_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory))
            first = layout.trigger_dir("P", "1", 1)
            stale = layout.trigger_dir("P", "1", 4)
            first.mkdir(parents=True)
            stale.mkdir(parents=True)
            (first / "trigger_test.txt").write_text("p.T::old\n", encoding="utf-8")
            (first / "execution.json").write_text("{}", encoding="utf-8")
            (stale / "localization.json").write_text("{}", encoding="utf-8")

            collect._prepare_trigger_directories(layout, "P", "1", ["p.T::new"])

            self.assertFalse(first.exists())
            self.assertFalse(stale.exists())

    @patch("mllmfl.stages.collect.compile_project")
    @patch("mllmfl.stages.collect.checkout")
    @patch("mllmfl.stages.collect.ensure_defects4j")
    @patch("mllmfl.stages.collect.defects4j_environment", return_value={})
    def test_checkout_failure_skips_compile(
        self,
        _environment,
        _ensure,
        checkout_mock,
        compile_mock,
    ):
        checkout_mock.return_value = CommandResult(1, "", "checkout failed")
        with tempfile.TemporaryDirectory() as directory:
            rows = collect.run(
                RunLayout(Path(directory)),
                ["Lang"],
                {"1"},
                None,
                None,
                30,
            )

        compile_mock.assert_not_called()
        self.assertEqual(rows[0]["status"], "SETUP_FAILED")


class CliValidationTests(unittest.TestCase):
    def test_interaction_mode_defaults_and_rejects_unknown_values(self):
        self.assertEqual(localization_interaction_mode({}), TEXT_INDEX_MODE)
        self.assertEqual(
            localization_interaction_mode({
                "mllm": {"interaction_mode": IMAGE_ONLY_MODE},
            }),
            IMAGE_ONLY_MODE,
        )
        with self.assertRaisesRegex(ValueError, "interaction_mode"):
            localization_interaction_mode({
                "mllm": {"interaction_mode": "unsupported"},
            })

    def test_collect_defaults_to_three_failing_tests(self):
        args = build_parser().parse_args(["collect"])
        self.assertEqual(args.max_failing_tests, 3)

    def test_method_summary_generation_is_disabled(self):
        candidate = candidate_without_summary("p.Service.run")
        self.assertEqual(candidate.function, "p.Service.run")
        self.assertEqual(candidate.summary, "")
        self.assertEqual(candidate.status, "SUMMARY_DISABLED")
        self.assertEqual(candidate.source_file, "")

    def test_rejects_non_positive_candidate_cap_and_top_k(self):
        parser = build_parser()
        invalid_commands = [
            ["collect", "--max-failing-tests", "0"],
            ["summarize", "--candidate-cap", "0"],
            ["localize", "--config", "config.json", "--top-k", "-1"],
            ["aggregate", "--top-k", "0"],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parser.parse_args(command)

    def test_candidate_selection_rejects_non_positive_cap(self):
        with self.assertRaisesRegex(ValueError, "candidate cap must be positive"):
            candidate_functions({}, 0)


class ProcessTests(unittest.TestCase):
    def test_timeout_has_stable_return_code(self):
        result = run_command([sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.01)
        self.assertEqual(result.returncode, 124)
