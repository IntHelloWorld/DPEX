import json
import tempfile
import unittest
from pathlib import Path

from scripts.count_uml_compression import collect_bug_statistics


class CompressionStatisticsTests(unittest.TestCase):
    def _trigger(
        self,
        root: Path,
        number: int,
        before: int | None,
        after: int | None,
    ) -> Path:
        directory = (
            root / "artifacts" / "Chart" / "bug_4" / "triggers"
            / f"trigger_{number}"
        )
        directory.mkdir(parents=True)
        if before is not None and after is not None:
            (directory / "execution_compressed.json").write_text(
                json.dumps({
                    "schema": "fullchain-compressed-execution",
                    "schema_version": 2,
                    "source_call_count": before,
                    "represented_call_count": before,
                    "displayed_call_count": after,
                }),
                encoding="utf-8",
            )
        return directory

    def test_aggregates_triggers_in_natural_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._trigger(root, 10, 100, 40)
            self._trigger(root, 2, 50, 10)

            rows, total = collect_bug_statistics(root, "Chart", "4")

        self.assertEqual([row["trigger"] for row in rows], [
            "trigger_2", "trigger_10",
        ])
        self.assertEqual(total["before"], 150)
        self.assertEqual(total["after"], 50)
        self.assertEqual(total["saved"], 100)
        self.assertAlmostEqual(total["reduction_percent"], 200 / 3)
        self.assertEqual(total["compression_ratio"], 3.0)
        self.assertEqual(total["status"], "OK")

    def test_reports_missing_and_corrupt_artifacts_without_counting_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._trigger(root, 1, 20, 5)
            self._trigger(root, 2, None, None)
            corrupt = self._trigger(root, 3, None, None)
            (corrupt / "execution_compressed.json").write_text(
                "{broken", encoding="utf-8"
            )

            rows, total = collect_bug_statistics(root, "Chart", "4")

        self.assertEqual([row["status"] for row in rows], [
            "OK", "ERROR", "ERROR",
        ])
        self.assertIn("missing", rows[1]["error"])
        self.assertIn("cannot read", rows[2]["error"])
        self.assertEqual(total["successful_trigger_count"], 1)
        self.assertEqual(total["before"], 20)
        self.assertEqual(total["after"], 5)
        self.assertEqual(total["status"], "PARTIAL")

    def test_can_select_one_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._trigger(root, 1, 20, 5)
            self._trigger(root, 2, 30, 10)

            rows, total = collect_bug_statistics(
                root, "Chart", "4", "trigger_2"
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trigger"], "trigger_2")
        self.assertEqual(total["before"], 30)
        self.assertEqual(total["after"], 10)

    def test_rejects_bug_without_trigger_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                root / "artifacts" / "Chart" / "bug_4" / "triggers"
            ).mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "no trigger directories"):
                collect_bug_statistics(root, "Chart", "4")


if __name__ == "__main__":
    unittest.main()
