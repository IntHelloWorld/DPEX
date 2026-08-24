import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mllmfl.infrastructure.plantuml import ensure_rendered


def _valid_png_header(width: int = 10, height: int = 10) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(
        ">II", width, height
    )


class OnDemandPlantUMLTests(unittest.TestCase):
    @patch("mllmfl.infrastructure.plantuml._renderer_identity")
    @patch("mllmfl.infrastructure.plantuml.render")
    def test_renders_once_then_reuses_content_addressed_cache(
        self, render_mock, identity_mock
    ):
        identity_mock.return_value = {"kind": "test", "version": 1}

        def fake_render(puml_path, *_args):
            png = puml_path.with_suffix(".png")
            png.write_bytes(_valid_png_header())
            return png

        render_mock.side_effect = fake_render
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            puml = root / "D-001.puml"
            image = root / "D-001.png"
            puml.write_text("@startuml\n@enduml\n", encoding="utf-8")

            first_path, first_hit = ensure_rendered(
                puml, image, "plantuml", None, 30, 4096
            )
            second_path, second_hit = ensure_rendered(
                puml, image, "plantuml", None, 30, 4096
            )
            manifest = json.loads(
                (root / "D-001.png.render.json").read_text(encoding="utf-8")
            )

        self.assertEqual(first_path, image)
        self.assertEqual(second_path, image)
        self.assertFalse(first_hit)
        self.assertTrue(second_hit)
        self.assertEqual(render_mock.call_count, 1)
        self.assertEqual(manifest["schema"], "plantuml-render-cache")

    @patch("mllmfl.infrastructure.plantuml._renderer_identity")
    @patch("mllmfl.infrastructure.plantuml.render")
    def test_changed_puml_invalidates_cached_png(self, render_mock, identity_mock):
        identity_mock.return_value = {"kind": "test", "version": 1}

        def fake_render(puml_path, *_args):
            png = puml_path.with_suffix(".png")
            png.write_bytes(_valid_png_header())
            return png

        render_mock.side_effect = fake_render
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            puml = root / "D-001.puml"
            image = root / "D-001.png"
            puml.write_text("@startuml\nA -> B\n@enduml\n", encoding="utf-8")
            ensure_rendered(puml, image, "plantuml", None, 30, 4096)
            puml.write_text("@startuml\nA -> C\n@enduml\n", encoding="utf-8")
            _, cache_hit = ensure_rendered(
                puml, image, "plantuml", None, 30, 4096
            )

        self.assertFalse(cache_hit)
        self.assertEqual(render_mock.call_count, 2)

    @patch("mllmfl.infrastructure.plantuml._renderer_identity")
    @patch("mllmfl.infrastructure.plantuml.render")
    def test_failed_render_does_not_replace_existing_cache(
        self, render_mock, identity_mock
    ):
        identity_mock.return_value = {"kind": "test", "version": 1}
        render_mock.side_effect = RuntimeError("bad diagram")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            puml = root / "D-001.puml"
            image = root / "D-001.png"
            puml.write_text("@startuml\n@enduml\n", encoding="utf-8")
            image.write_bytes(b"previous")

            with self.assertRaisesRegex(RuntimeError, "bad diagram"):
                ensure_rendered(puml, image, "plantuml", None, 30, 4096)

            self.assertEqual(image.read_bytes(), b"previous")
            self.assertFalse((root / "D-001.png.render.json").exists())


if __name__ == "__main__":
    unittest.main()
