import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from mllmfl.cli import build_parser
from mllmfl.domain.schemas import validate_candidates, validate_localization
from mllmfl.infrastructure.java_source import extract_methods
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.infrastructure.process import CommandResult, run_command
from mllmfl.stages import collect
from mllmfl.stages.aggregate import aggregate_rankings
from mllmfl.stages.localize import gate_ranking, parse_model_response
from mllmfl.stages.summarize import candidate_functions


class JavaSourceTests(unittest.TestCase):
    def test_extracts_method_and_ignores_braces_in_strings(self):
        text = 'package p; class A { public int run(int x) { String s = "}"; return x; } }'
        methods = extract_methods(text, "p.A", "run")
        self.assertEqual(len(methods), 1)
        self.assertIn("return x", methods[0]["code"])


class LocalizationTests(unittest.TestCase):
    def test_parses_fenced_json_and_rejects_invalid_response(self):
        self.assertEqual(parse_model_response('```json\n{"ranked": []}\n```'), {"ranked": []})
        self.assertIsNone(parse_model_response("not json"))

    def test_gates_and_deduplicates_candidates(self):
        ranking, dropped = gate_ranking([
            {"function": "A.run"}, {"function": "p.A.run"}, {"function": "not.allowed"},
        ], ["p.A.run"], 5)
        self.assertEqual([item.function for item in ranking], ["p.A.run"])
        self.assertEqual(dropped, ["not.allowed"])

    def test_schema_validation_rejects_duplicate_and_non_contiguous_values(self):
        with self.assertRaisesRegex(ValueError, "duplicate candidate"):
            validate_candidates({"schema": "fault-candidates", "schema_version": 1, "candidates": [
                {"function": "p.A.m"}, {"function": "p.A.m"},
            ]})
        with self.assertRaisesRegex(ValueError, "non-contiguous"):
            validate_localization({"schema": "fault-localization", "schema_version": 1,
                                   "ranking": [{"function": "p.A.m", "rank": 2}]})

    def test_schema_validation_normalizes_invalid_rank_type(self):
        with self.assertRaisesRegex(ValueError, "invalid ranking rank"):
            validate_localization({
                "schema": "fault-localization",
                "schema_version": 1,
                "ranking": [{"function": "p.A.m", "rank": {"invalid": True}}],
            })


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
