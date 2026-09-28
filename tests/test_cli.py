import unittest

from dpex.cli import build_parser


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
            {"collect", "trace", "localize", "refine", "evaluate", "cleanup"},
        )

    def test_collect_and_trace_share_the_merged_parser(self) -> None:
        parser = build_parser()
        for stage in ("collect", "trace"):
            args = parser.parse_args([stage, "--config", "trace.json"])
            self.assertEqual(args.config, "trace.json")
            self.assertEqual(args.timeout, 600)
            self.assertNotIn("trigger", vars(args))
        self.assertEqual(parser.parse_args([
            "refine", "--locator-results", "x", "--config", "y",
        ]).timeout, 1200)
        localize = parser.parse_args(["localize", "--config", "y"])
        self.assertEqual(localize.timeout, 1200)
        self.assertIsNone(localize.failure_cache_root)

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
        self.assertIsNone(
            parser.parse_args(["evaluate"]).evaluation_cache_root
        )
        self.assertFalse(parser.parse_args(["cleanup"]).apply)

    def test_refine_viewport_flags_are_optional_json_overrides(self) -> None:
        parser = build_parser()
        base = ["refine", "--locator-results", "x", "--config", "y"]
        defaults = parser.parse_args(base)
        self.assertIsNone(defaults.trace_root)
        self.assertIsNone(defaults.max_upstream_calls)
        self.assertIsNone(defaults.max_downstream_calls)
        self.assertIsNone(defaults.max_internal_calls)
        overridden = parser.parse_args(base + [
            "--max-upstream-calls", "8",
            "--max-downstream-calls", "16",
            "--max-internal-calls", "12",
        ])
        self.assertEqual(
            (
                overridden.max_upstream_calls,
                overridden.max_downstream_calls,
                overridden.max_internal_calls,
            ),
            (8, 16, 12),
        )
        external = parser.parse_args(base + ["--trace-root", "trace-run"])
        self.assertEqual(external.trace_root, "trace-run")


if __name__ == "__main__":
    unittest.main()
