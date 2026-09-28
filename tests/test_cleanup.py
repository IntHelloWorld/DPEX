import tempfile
import unittest
from pathlib import Path

from dpex.infrastructure.layout import RunLayout
from dpex.stages import cleanup


class CleanupTests(unittest.TestCase):
    def test_legacy_cleanup_is_exact_and_dry_run_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory))
            layout.ensure()
            bug_dir = layout.artifacts / "Closure/bug_131"
            nested = bug_dir / "triggers/trigger_1"
            nested.mkdir(parents=True)
            targets = [
                bug_dir / "trace.json",
                nested / "execution_sliced.json",
                nested / "execution_compressed.json",
            ]
            for index, path in enumerate(targets, 1):
                path.write_bytes(b"x" * index)
            preserved = [
                nested / "execution.json",
                nested / "almost_execution_compressed.json",
                bug_dir / "refinement.json",
            ]
            for path in preserved:
                path.write_text("keep")

            preview = cleanup.run(
                layout, ["Closure"], {"131"}, apply=False
            )
            self.assertEqual(len(preview), 3)
            self.assertEqual({item["status"] for item in preview}, {"DRY_RUN"})
            self.assertTrue(all(path.is_file() for path in targets))

            removed = cleanup.run(
                layout, ["Closure"], {"131"}, apply=True
            )
            self.assertEqual(sum(item["bytes"] for item in removed), 6)
            self.assertFalse(any(path.exists() for path in targets))
            self.assertTrue(all(path.is_file() for path in preserved))


if __name__ == "__main__":
    unittest.main()
