import tempfile
import unittest
from pathlib import Path

from mllmfl.domain.test_slice import (
    extract_statements,
    select_statements,
    slice_execution,
    validate_slice_metadata,
)
from mllmfl.domain.trace import build_trace, project_execution


SOURCE = """package p;
class T {
    public void testCase() {
        Renderer r = new Renderer();
        assertNotNull(r.items());
        Dataset dataset = new Dataset();
        Plot plot = new Plot();
        plot.setDataset(dataset);
        plot.setRenderer(r);
        assertEquals(0, r.items().size());
        dataset.add(1);
        Items lic = r.items();
        assertEquals(1, lic.size());
        assertEquals("S1", lic.get(0).label());
    }
}
"""


class StatementSelectionTests(unittest.TestCase):
    def test_selects_transitive_setup_and_skips_prior_assertions(self):
        statements = extract_statements(SOURCE, "p.T", "testCase")
        selected = select_statements(statements, 14)
        code = "\n".join(item.code for item in selected)
        self.assertIn("new Renderer", code)
        self.assertIn("setDataset", code)
        self.assertIn("setRenderer", code)
        self.assertIn("dataset.add", code)
        self.assertIn("Items lic", code)
        self.assertIn('assertEquals("S1"', code)
        self.assertNotIn("assertNotNull", code)
        self.assertNotIn("assertEquals(0", code)
        self.assertNotIn("assertEquals(1", code)

    def test_uses_ast_for_multi_declarations_control_dependencies_and_lambdas(self):
        source = """package p;
class T {
    void testComplex() {
        Renderer r = new Renderer();
        Dataset first = new Dataset(), second = new Dataset();
        Runnable ignored = () -> { Other hidden = new Other(); hidden.run(); };
        if (enabled(r)) {
            second.add(1);
            Result value = r.render(second);
            assertEquals(1, value.size());
        }
    }
}
"""
        statements = extract_statements(source, "p.T", "testComplex")
        declaration = next(item for item in statements if "Dataset first" in item.code)
        lambda_declaration = next(item for item in statements if "Runnable ignored" in item.code)
        self.assertEqual(declaration.definitions, frozenset({"first", "second"}))
        self.assertEqual(lambda_declaration.definitions, frozenset({"ignored"}))
        self.assertNotIn("hidden", lambda_declaration.references)

        selected = select_statements(statements, 10)
        selected_code = "\n".join(item.code for item in selected)
        self.assertIn("(enabled(r))", selected_code)
        self.assertIn("second.add(1)", selected_code)
        self.assertNotIn("Runnable ignored", selected_code)

    def test_parse_error_or_missing_method_returns_no_statements(self):
        self.assertEqual(extract_statements("class T { void testCase( {", "T", "testCase"), [])
        self.assertEqual(extract_statements("class T {}", "T", "testCase"), [])

    def test_slice_schema_rejects_corrupt_statement_metadata(self):
        with self.assertRaisesRegex(ValueError, "statement kind"):
            validate_slice_metadata({
                "schema": "test-boundary-slice",
                "schema_version": 2,
                "applied": True,
                "selected_statements": [{
                    "kind": "unknown",
                    "start_line": 1,
                    "end_line": 1,
                    "definitions": [],
                    "references": [],
                }],
            })

    def test_filters_dynamic_calls_by_selected_statement_lines(self):
        events = [
            {"type": "TEST_START", "seq": 1, "class": "p.T", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.T", "method": "testCase", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.Renderer", "method": "items", "descriptor": "()V",
             "origin_test_line": 5},
            {"type": "RETURN", "seq": 4, "invocation_id": 2},
            {"type": "ENTER", "seq": 5, "invocation_id": 3, "parent_id": 1,
             "class": "p.Dataset", "method": "add", "descriptor": "(I)V",
             "origin_test_line": 11},
            {"type": "RETURN", "seq": 6, "invocation_id": 3},
            {"type": "ENTER", "seq": 7, "invocation_id": 4, "parent_id": 1,
             "class": "p.Renderer", "method": "items", "descriptor": "()V",
             "origin_test_line": 12},
            {"type": "RETURN", "seq": 8, "invocation_id": 4},
            {"type": "ENTER", "seq": 9, "invocation_id": 5, "parent_id": 1,
             "class": "p.Items", "method": "get", "descriptor": "(I)V",
             "origin_test_line": 14},
            {"type": "RETURN", "seq": 10, "invocation_id": 5},
            {"type": "THROW", "seq": 11, "invocation_id": 1,
             "exception_class": "java.lang.AssertionError"},
            {"type": "TEST_FAILURE", "seq": 12,
             "exception_class": "java.lang.AssertionError", "source_line": 14},
            {"type": "TEST_END", "seq": 13, "successful": False},
        ]
        execution = project_execution(build_trace(events), "p.T", "testCase")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "T.java"
            source.write_text(SOURCE, encoding="utf-8")
            sliced = slice_execution(execution, source, "p.T", "testCase")
        methods = [call["callee_method"] for call in sliced["calls"]]
        self.assertNotIn("items", methods[:1])
        self.assertIn("add", methods)
        self.assertIn("get", methods)
        self.assertTrue(sliced["slice"]["applied"])
        self.assertEqual(sliced["slice"]["schema_version"], 2)
        self.assertEqual(
            sliced["slice"]["strategy"], "tree-sitter-test-method-backward-slice"
        )
        self.assertLess(sliced["call_count"], execution["call_count"])

    def test_missing_failure_line_preserves_complete_execution(self):
        events = [
            {"type": "TEST_START", "seq": 1, "class": "p.T", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.T", "method": "testCase", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.Service", "method": "run", "descriptor": "()V",
             "origin_test_line": 4},
            {"type": "RETURN", "seq": 4, "invocation_id": 2},
            {"type": "RETURN", "seq": 5, "invocation_id": 1},
            {"type": "TEST_END", "seq": 6, "successful": True},
        ]
        execution = project_execution(build_trace(events), "p.T", "testCase")
        sliced = slice_execution(execution, None, "p.T", "testCase")
        self.assertFalse(sliced["slice"]["applied"])
        self.assertEqual(sliced["calls"], execution["calls"])


if __name__ == "__main__":
    unittest.main()
