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
            {"collect", "trace", "refine", "evaluate", "cleanup"},
        )

    def test_collect_and_trace_share_the_merged_parser(self) -> None:
        parser = build_parser()
        for stage in ("collect", "trace"):
            args = parser.parse_args([stage, "--config", "trace.json"])
            self.assertEqual(args.config, "trace.json")
            self.assertNotIn("trigger", vars(args))

    def test_trace_value_capture_configuration_is_json_only(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["trace", "--config", "trace.json"])
        self.assertEqual(args.config, "trace.json")
        self.assertNotIn("capture_values", vars(args))
        self.assertNotIn("value_max_arguments_chars", vars(args))

    def test_debug_and_cleanup_flags_default_to_safe_retention(self) -> None:
        parser = build_parser()
        self.assertFalse(parser.parse_args([
            "trace", "--config", "trace.json",
        ]).retain_debug_artifacts)
        self.assertFalse(parser.parse_args(["refine", "--locator-results", "x", "--config", "y"]).retain_debug_artifacts)
        self.assertFalse(parser.parse_args(["evaluate"]).final_only)
        self.assertFalse(parser.parse_args(["cleanup"]).apply)


if __name__ == "__main__":
    unittest.main()
