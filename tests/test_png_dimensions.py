import unittest

from scripts.check_png_dimensions import diagram_call_counts


class PngDimensionTests(unittest.TestCase):
    def test_reads_adaptive_graph_call_counts(self):
        self.assertEqual(
            diagram_call_counts({
                "represented_call_count": 17,
                "visible_call_count": 4,
            }),
            (17, 4),
        )

    def test_keeps_legacy_uml_index_call_counts(self):
        self.assertEqual(
            diagram_call_counts({"call_count": 9, "displayed_call_count": 3}),
            (9, 3),
        )


if __name__ == "__main__":
    unittest.main()
