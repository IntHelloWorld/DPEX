import unittest

from mllmfl.cli import build_parser


class CliTests(unittest.TestCase):
    def test_only_refinement_pipeline_stages_are_registered(self) -> None:
        parser = build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if action.dest == "stage"
        )
        self.assertEqual(
            set(subparsers.choices),
            {"collect", "trace", "refine", "evaluate"},
        )

    def test_trace_value_capture_defaults_on_and_can_be_disabled(self) -> None:
        parser = build_parser()
        default_args = parser.parse_args(["trace"])
        disabled_args = parser.parse_args(["trace", "--no-capture-values"])
        self.assertTrue(default_args.capture_values)
        self.assertFalse(disabled_args.capture_values)


if __name__ == "__main__":
    unittest.main()
