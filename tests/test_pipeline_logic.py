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
from mllmfl.domain.schemas import (
    validate_candidates,
    validate_defect_context,
    validate_localization,
    validate_uml_index,
)
from mllmfl.infrastructure.java_source import extract_methods
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.process import CommandResult, run_command
from mllmfl.stages import collect
from mllmfl.stages.aggregate import aggregate_rankings
from mllmfl.stages.localize import (
    SYSTEM_PROMPT,
    _post_response,
    build_prompt,
    defect_output_context,
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
    def test_initial_prompt_omits_candidates_project_and_bug(self):
        prompt = build_prompt(
            "p.Test::testCase", "1 | run();", "p.Error: bad\n\tat p.Test.testCase(Test.java:1)",
            "", {"segments": []}, 1,
        )
        self.assertNotIn("[Candidate Functions]", prompt)
        self.assertNotIn("[Project]", prompt)
        self.assertNotIn("[Bug]", prompt)
        self.assertNotIn("candidate", SYSTEM_PROMPT.lower())
        self.assertIn("[Sliced Failing-Test Code With Original Line Numbers]\n1 | run();", prompt)
        self.assertIn("[Error Stack]\np.Error: bad", prompt)
        self.assertIn("[Test Output]\n(empty)", prompt)

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

    def test_parses_fenced_json_and_rejects_invalid_response(self):
        self.assertEqual(parse_model_response('```json\n{"ranked": []}\n```'), {"ranked": []})
        self.assertIsNone(parse_model_response("not json"))

    def test_model_ranking_payload_requires_exact_top_k_schema(self):
        value = {"ranked": [
            {"signature": "p.A.run(int)", "reason": "evidence"},
        ]}
        self.assertEqual(validate_model_ranking_payload(value, 1), value["ranked"])
        with self.assertRaisesRegex(ValueError, "exactly 2"):
            validate_model_ranking_payload(value, 2)
        value["ranked"][0]["function"] = "p.A.run"
        with self.assertRaisesRegex(ValueError, "invalid model ranking entry"):
            validate_model_ranking_payload(value, 1)

    def test_gates_and_deduplicates_candidates(self):
        ranking, dropped = gate_ranking([
            {"signature": "p.A.run(int)"}, {"signature": "p.A.run(int)"},
            {"signature": "p.A.run(Integer)"},
        ], ["p.A.run"], ["p.A.run(int)"], 5)
        self.assertEqual([item.function for item in ranking], ["p.A.run"])
        self.assertEqual([item.signature for item in ranking], ["p.A.run(int)"])
        self.assertEqual(dropped, ["p.A.run(Integer)"])

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
            ({"content": None, "tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": "view_sequence_diagram",
                    "arguments": '{"diagram_id":"L1-001-inv-2"}',
                },
            }]}, "vision", "resp-1"),
            ({"content": '{"ranked":[{"function":"p.Service.run"}]}'}, "vision", "resp-2"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "segment.png"
            image.write_bytes(b"png")
            conversation = root / "conversation.jsonl"
            index = {"segments": [{
                "diagram_id": "L1-001-inv-2", "function": "p.Service.run",
                "invocation_id": 2, "image": "segment.png",
                "method_signatures": ["p.Service.run()", "p.Helper.work(int)"],
            }]}
            raw, model, viewed, rounds, views = run_agent(
                {}, "prompt", index, root, 30, conversation
            )
            conversation_text = conversation.read_text()
        self.assertIn("p.Service.run", raw)
        self.assertEqual(model, "vision")
        self.assertEqual(viewed, ["L1-001-inv-2"])
        self.assertEqual((rounds, views), (1, 1))
        second_input = post.call_args_list[1].args[1]
        self.assertEqual(second_input[-1]["role"], "user")
        self.assertEqual(second_input[-1]["content"][1]["type"], "input_image")
        tool_output = second_input[-2]
        self.assertIn("p.Helper.work(int)", second_input[-1]["content"][0]["text"])
        self.assertEqual(tool_output["type"], "function_call_output")
        self.assertEqual(post.call_args_list[1].args[3], "resp-1")
        records = [json.loads(line) for line in conversation_text.splitlines()]
        self.assertEqual(
            [record["role"] for record in records],
            ["system", "user", "assistant", "tool", "user", "assistant"],
        )
        self.assertEqual(records[4]["content"][1], {
            "type": "image_ref", "diagram_id": "L1-001-inv-2",
        })
        self.assertNotIn("data:image", conversation_text)

    @patch("mllmfl.stages.localize._post_response")
    def test_agent_returns_recoverable_unknown_diagram_error(self, post):
        post.side_effect = [
            ({"tool_calls": [{"id": "bad", "function": {
                "name": "view_sequence_diagram",
                "arguments": '{"diagram_id":"missing"}',
            }}]}, "vision", "resp-1"),
            ({"content": '{"ranked":[]}'}, "vision", "resp-2"),
        ]
        raw, _, viewed, rounds, views = run_agent(
            {}, "prompt", {"segments": []}, Path("."), 30
        )
        self.assertEqual(raw, '{"ranked":[]}')
        self.assertEqual((viewed, rounds, views), ([], 1, 0))
        second_input = post.call_args_list[1].args[1]
        self.assertIn("unknown diagram_id", second_input[-1]["output"])

    @patch("mllmfl.stages.localize._post_response")
    def test_agent_loads_only_one_diagram_per_tool_round(self, post):
        post.side_effect = [
            ({"tool_calls": [
                {"id": "one", "function": {"name": "view_sequence_diagram",
                 "arguments": '{"diagram_id":"L1-001"}'}},
                {"id": "two", "function": {"name": "view_sequence_diagram",
                 "arguments": '{"diagram_id":"L1-002"}'}},
            ]}, "vision", "resp-1"),
            ({"content": '{"ranked":[]}'}, "vision", "resp-2"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one.png", "two.png"):
                (root / name).write_bytes(b"png")
            index = {"segments": [
                {"diagram_id": "L1-001", "function": "p.A.one",
                 "invocation_id": 2, "image": "one.png",
                 "method_signatures": ["p.A.one()"]},
                {"diagram_id": "L1-002", "function": "p.B.two",
                 "invocation_id": 3, "image": "two.png",
                 "method_signatures": ["p.B.two()"]},
            ]}
            _, _, viewed, rounds, views = run_agent({}, "prompt", index, root, 30)
        self.assertEqual(viewed, ["L1-001"])
        self.assertEqual((rounds, views), (1, 1))
        image_message = post.call_args_list[1].args[1][-1]
        self.assertEqual(
            [part["type"] for part in image_message["content"]],
            ["input_text", "input_image"],
        )
        rejected_tool = post.call_args_list[1].args[1][-2]
        self.assertIn("only one diagram", rejected_tool["output"])

    @patch("mllmfl.stages.localize.time.sleep")
    @patch("mllmfl.stages.localize.requests.post")
    @patch.dict("os.environ", {"TEST_API_KEY": "secret"}, clear=False)
    def test_response_request_retries_http_failure(self, post, sleep):
        failed = unittest.mock.Mock(status_code=500, text="temporary")
        succeeded = unittest.mock.Mock(status_code=200)
        succeeded.json.return_value = {
            "id": "resp-1",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "done"},
            ]}],
        }
        post.side_effect = [failed, succeeded]
        message, model, response_id = _post_response({"mllm": {
            "api_key_env": "TEST_API_KEY", "vision_model": "vision",
            "retry": {"max_retries": 2, "backoff_s": 0},
        }}, [], 30)
        self.assertEqual(message["content"], "done")
        self.assertEqual(model, "vision")
        self.assertEqual(response_id, "resp-1")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()
        payload = post.call_args.kwargs["json"]
        self.assertIs(payload["parallel_tool_calls"], False)
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["reasoning"], {"effort": "medium"})
        self.assertTrue(post.call_args.kwargs["json"]["tools"][0]["name"])
        self.assertTrue(post.call_args.args[0].endswith("/responses"))

    @patch.dict("os.environ", {"TEST_API_KEY": "secret"}, clear=False)
    def test_response_request_rejects_invalid_reasoning_effort(self):
        base = {"api_key_env": "TEST_API_KEY", "vision_model": "vision"}
        with self.assertRaisesRegex(ValueError, "reasoning_effort must be"):
            _post_response({"mllm": {**base, "reasoning_effort": "maximum"}}, [], 30)

    @patch("mllmfl.stages.localize.requests.post")
    @patch.dict("os.environ", {"TEST_API_KEY": "secret"}, clear=False)
    def test_response_request_only_sets_reasoning_control(self, post):
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = {
            "id": "resp-2",
            "output": [{"type": "function_call", "call_id": "call-1",
                        "name": "view_sequence_diagram", "arguments": "{}"}],
        }
        post.return_value = response
        message, _, _ = _post_response({"mllm": {
            "api_key_env": "TEST_API_KEY", "vision_model": "vision",
            "reasoning_effort": "medium",
        }}, [{"role": "user", "content": [{"type": "input_image",
              "image_url": "data:image/png;base64,eA=="}]}], 30, "resp-1")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["reasoning"], {"effort": "medium"})
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["previous_response_id"], "resp-1")
        self.assertEqual(message["tool_calls"][0]["id"], "call-1")


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


class AggregateTests(unittest.TestCase):
    def test_ties_have_deterministic_function_order(self):
        results = [
            {"trigger": "1", "ranking": [{"rank": 1, "function": "p.B.m"}]},
            {"trigger": "2", "ranking": [{"rank": 1, "function": "p.A.m"}]},
        ]
        ranking = aggregate_rankings(results, 5)
        self.assertEqual([item["function"] for item in ranking], ["p.A.m", "p.B.m"])

    def test_rejects_non_positive_top_k(self):
        with self.assertRaisesRegex(ValueError, "top_k must be positive"):
            aggregate_rankings([], 0)


class CollectTests(unittest.TestCase):
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
    def test_method_summary_generation_is_disabled(self):
        candidate = candidate_without_summary("p.Service.run")
        self.assertEqual(candidate.function, "p.Service.run")
        self.assertEqual(candidate.summary, "")
        self.assertEqual(candidate.status, "SUMMARY_DISABLED")
        self.assertEqual(candidate.source_file, "")

    def test_rejects_non_positive_candidate_cap_and_top_k(self):
        parser = build_parser()
        invalid_commands = [
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
