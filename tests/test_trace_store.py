import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import zstandard

from dpex.domain.assertion_folding import fold_successful_assertions
from dpex.domain.refinement_trace import (
    build_method_catalog,
    build_method_catalog_from_keys,
    build_refinement_trace,
    validate_refinement_trace,
)
from dpex.domain.trace import build_trace, project_execution
from dpex.infrastructure.io import read_zstd_json
from dpex.infrastructure.plantuml import ensure_rendered, validate_png
from dpex.infrastructure.process import run_command_with_zstd_fifo
from dpex.infrastructure.trace_store import (
    METHOD_SUMMARY_NAME,
    TRACE_STORE_ARCHIVE_NAME,
    TRACE_STORE_NAME,
    archive_final_trace_store,
    archive_trace_store,
    ensure_trace_store,
    final_trace_archive_path,
    final_trace_sha256,
    is_fast_trace_store,
    raw_events_to_fast_store,
    raw_events_to_degraded_store,
    raw_events_to_store,
    read_available_trace_summary,
    write_refinement_trace_from_store,
    finalize_trace_store,
    SQLiteTraceTopology,
)
from dpex.domain.focus_viewport import plan_focus_viewport
from dpex.stages.refine.focus_graph import focus_graph_diagram_nodes


CAPTURE_DISABLED = {
    "capture_values": False,
    "value_string_edge_chars": 10,
    "value_container_edge_items": 2,
    "value_nested_container_edge_items": 1,
    "value_max_depth": 2,
    "value_max_arguments": 8,
}

DEGRADED_RENDER_RESULT_ROOT = (
    Path(__file__).resolve().parent
    / "generated/focus_viewport/degraded_store_rendering"
)
PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def assertion_events():
    return [
        {"type": "TEST_START", "seq": 1, "class": "p.Test",
         "method": "testCase", "agent_protocol_version": 5,
         "value_capture": CAPTURE_DISABLED},
        {"type": "ENTER", "seq": 2, "ts_ns": 10, "thread_id": 1,
         "thread_name": "main", "invocation_id": 1, "parent_id": 0,
         "class": "p.Test", "method": "testCase", "descriptor": "()V",
         "origin_test_line": 0},
        {"type": "ASSERT_START", "seq": 3, "assertion_id": "A001",
         "source_start_line": 10, "source_end_line": 10,
         "thread_id": 1, "thread_name": "main"},
        {"type": "ENTER", "seq": 4, "ts_ns": 20, "thread_id": 1,
         "thread_name": "main", "invocation_id": 2, "parent_id": 1,
         "class": "p.Service", "method": "observe", "descriptor": "()V",
         "origin_test_line": 10},
        {"type": "ENTER", "seq": 5, "ts_ns": 21, "thread_id": 1,
         "thread_name": "main", "invocation_id": 3, "parent_id": 2,
         "class": "p.Helper", "method": "read", "descriptor": "()V",
         "origin_test_line": 10},
        {"type": "RETURN", "seq": 6, "ts_ns": 22, "thread_id": 1,
         "invocation_id": 3, "duration_ns": 1},
        {"type": "RETURN", "seq": 7, "ts_ns": 23, "thread_id": 1,
         "invocation_id": 2, "duration_ns": 3},
        {"type": "ASSERT_PASS", "seq": 8, "assertion_id": "A001",
         "source_start_line": 10, "source_end_line": 10,
         "thread_id": 1, "thread_name": "main"},
        {"type": "ENTER", "seq": 9, "ts_ns": 30, "thread_id": 1,
         "thread_name": "main", "invocation_id": 4, "parent_id": 1,
         "class": "p.Service", "method": "mutate", "descriptor": "()V",
         "origin_test_line": 11},
        {"type": "RETURN", "seq": 10, "ts_ns": 31, "thread_id": 1,
         "invocation_id": 4, "duration_ns": 1},
        {"type": "RETURN", "seq": 11, "ts_ns": 40, "thread_id": 1,
         "invocation_id": 1, "duration_ns": 30},
        {"type": "TEST_FAILURE", "seq": 12,
         "exception_class": "java.lang.AssertionError", "message": "bad"},
        {"type": "TEST_END", "seq": 13, "successful": False,
         "failure_count": 1},
    ]


def window_folding_events() -> list[dict]:
    tree = (
        "p.Test", "testWindow", [
            ("p.Outer", "farBefore", []),
            ("p.Parent", "run", [
                ("p.Near", "before1", []),
                ("p.Near", "before2", []),
                ("p.Service", "focus", [
                    ("p.Child", "child1", [
                        ("p.Deep", "deep1", []),
                    ]),
                    ("p.Child", "child2", []),
                ]),
                ("p.Near", "after1", []),
                ("p.Near", "after2", []),
            ]),
            ("p.Outer", "farAfter", []),
        ],
    )
    events = [{
        "type": "TEST_START", "seq": 1, "class": "p.Test",
        "method": "testWindow", "agent_protocol_version": 5,
        "value_capture": CAPTURE_DISABLED,
    }]
    seq = 2
    next_invocation_id = 1

    def emit(node: tuple, parent_id: int) -> None:
        nonlocal seq, next_invocation_id
        class_name, method, children = node
        invocation_id = next_invocation_id
        next_invocation_id += 1
        events.append({
            "type": "ENTER", "seq": seq, "ts_ns": seq,
            "thread_id": 1, "thread_name": "main",
            "invocation_id": invocation_id, "parent_id": parent_id,
            "class": class_name, "method": method, "descriptor": "()V",
            "origin_test_line": seq,
        })
        seq += 1
        for child in children:
            emit(child, invocation_id)
        events.append({
            "type": "RETURN", "seq": seq, "ts_ns": seq,
            "thread_id": 1, "invocation_id": invocation_id,
            "duration_ns": 1,
        })
        seq += 1

    emit(tree, 0)
    events.append({
        "type": "TEST_END", "seq": seq, "successful": False,
        "failure_count": 1,
    })
    return events


class FifoCompressionTests(unittest.TestCase):
    @staticmethod
    def _writer(fifo: Path) -> list[str]:
        return [
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'event\\n')",
            str(fifo),
        ]

    def test_fifo_is_streamed_to_a_verified_zstd_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fifo = root / "raw_events.fifo"
            target = root / "raw_events.jsonl.zst"
            command = [
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'one\\ntwo\\n')",
                str(fifo),
            ]
            result = run_command_with_zstd_fifo(
                command, fifo_path=fifo, target=target, cwd=root, timeout=10
            )
            self.assertEqual(result.returncode, 0)
            self.assertFalse(fifo.exists())
            with zstandard.open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"one\ntwo\n")

    def test_compressor_exit_and_incomplete_stream_are_distinct(self):
        scripts = {
            "compressor": (
                "#!/usr/bin/env python3\n"
                "import pathlib,sys\n"
                "pathlib.Path(sys.argv[-1]).read_bytes()\n"
                "raise SystemExit(7)\n"
            ),
            "incomplete": (
                "#!/usr/bin/env python3\n"
                "import pathlib,sys\n"
                "if '--test' in sys.argv: raise SystemExit(9)\n"
                "pathlib.Path(sys.argv[-1]).read_bytes()\n"
                "sys.stdout.buffer.write(b'not-zstd')\n"
            ),
        }
        for expected, script in scripts.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                zstd = root / "fake-zstd"
                zstd.write_text(script, encoding="utf-8")
                zstd.chmod(0o700)
                fifo = root / "raw_events.fifo"
                target = root / "raw_events.jsonl.zst"
                pattern = (
                    "zstd compression failed with exit 7"
                    if expected == "compressor"
                    else "incomplete or corrupt Zstd trace stream"
                )
                with self.assertRaisesRegex(RuntimeError, pattern):
                    run_command_with_zstd_fifo(
                        self._writer(fifo),
                        fifo_path=fifo,
                        target=target,
                        cwd=root,
                        timeout=10,
                        zstd=zstd,
                    )
                self.assertFalse(target.exists())
                self.assertTrue((root / "raw_events.jsonl.incomplete.zst").is_file())


class SQLiteTraceConverterTests(unittest.TestCase):
    maxDiff = None

    def test_unfolded_store_retains_successful_assertion_calls_and_raw_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.jsonl.zst"
            with zstandard.open(raw, "wt", encoding="utf-8") as handle:
                for event in assertion_events():
                    handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            summary = raw_events_to_store(
                raw, root / TRACE_STORE_NAME, root / METHOD_SUMMARY_NAME,
                project="P", test="p.Test::testCase", test_class="p.Test",
                test_method="testCase", process_exit_code=1,
                assertion_instrumentation={"configured_ranges": "10:10"},
                defect_context={"error_stack": "failure", "test_output": "failed"},
                capture_config=CAPTURE_DISABLED, fold_assertions=False,
            )
            self.assertEqual(summary["call_count"], 3)
            catalog, method_ids, fingerprint = build_method_catalog_from_keys(
                tuple(item) for item in summary["methods"]
            )
            target = root / "T1.trace.sqlite3"
            finalize_trace_store(
                root / TRACE_STORE_NAME, target, project="P", test_id="T1",
                test="p.Test::testCase", method_ids=method_ids,
                catalog_fingerprint=fingerprint,
            )
            with SQLiteTraceTopology.open(target) as topology:
                folding = topology.trace["assertion_folding"]
                self.assertEqual(folding["strategy"], "assertion-folding-disabled")
                self.assertEqual(folding["folded_call_count"], 0)
                self.assertEqual(folding["successful_assertion_count"], 1)
                window = topology.continuous_event_window(2, before=0, after=0)
                self.assertEqual(
                    [(item["type"], item["invocation"]["invocation_id"])
                     for item in window["events"]],
                    [("CALL", 2)],
                )
                self.assertGreater(window["omitted_after"], 0)

    def test_continuous_event_window_uses_deterministic_edge_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.jsonl.zst"
            with zstandard.open(raw, "wt", encoding="utf-8") as handle:
                for event in assertion_events():
                    handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            summary = raw_events_to_store(
                raw, root / TRACE_STORE_NAME, root / METHOD_SUMMARY_NAME,
                project="P", test="p.Test::testCase", test_class="p.Test",
                test_method="testCase", process_exit_code=1,
                assertion_instrumentation={"configured_ranges": "10:10"},
                defect_context={"error_stack": "failure", "test_output": "failed"},
                capture_config=CAPTURE_DISABLED, fold_assertions=False,
            )
            _, method_ids, fingerprint = build_method_catalog_from_keys(
                tuple(item) for item in summary["methods"]
            )
            target = root / "T1.trace.sqlite3"
            finalize_trace_store(
                root / TRACE_STORE_NAME, target, project="P", test_id="T1",
                test="p.Test::testCase", method_ids=method_ids,
                catalog_fingerprint=fingerprint,
            )
            with SQLiteTraceTopology.open(target) as topology:
                first = topology.continuous_event_window(3, before=1, after=1)
                second = topology.continuous_event_window(3, before=1, after=1)
                self.assertEqual(first, second)
                self.assertEqual(first["event_count"], 3)
                self.assertEqual(
                    [item["type"] for item in first["events"]],
                    ["CALL", "CALL", "RETURN"],
                )
                at_start = topology.continuous_event_window(2, before=3, after=1)
                self.assertEqual(at_start["event_count"], 5)
                self.assertEqual(at_start["omitted_before"], 0)
                self.assertEqual(
                    [item["type"] for item in at_start["events"]],
                    ["CALL", "CALL", "RETURN", "RETURN", "CALL"],
                )

    def test_final_sqlite_archive_is_lossless_and_opens_on_demand(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.jsonl.zst"
            with zstandard.open(raw, "wt", encoding="utf-8") as handle:
                for event in assertion_events():
                    handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            summary = raw_events_to_store(
                raw, root / TRACE_STORE_NAME, root / METHOD_SUMMARY_NAME,
                project="P", test="p.Test::testCase", test_class="p.Test",
                test_method="testCase", process_exit_code=1,
                assertion_instrumentation={"configured_ranges": ""},
                defect_context={"error_stack": "failure", "test_output": "failed"},
                capture_config=CAPTURE_DISABLED,
            )
            catalog, method_ids, fingerprint = build_method_catalog_from_keys(
                tuple(item) for item in summary["methods"]
            )
            target = root / "T1.trace.sqlite3"
            expected = finalize_trace_store(
                root / TRACE_STORE_NAME, target, project="P", test_id="T1",
                test="p.Test::testCase", method_ids=method_ids,
                catalog_fingerprint=fingerprint,
            )
            archive = archive_final_trace_store(target)
            self.assertEqual(archive, final_trace_archive_path(target))
            self.assertFalse(target.exists())
            self.assertTrue(archive.is_file())
            self.assertEqual(final_trace_sha256(target), expected)
            with SQLiteTraceTopology.open(target) as topology:
                self.assertEqual(topology.trace["test_id"], "T1")
                self.assertEqual(topology.trace["call_count"], summary["call_count"])
                self.assertTrue(topology.methods)
            self.assertFalse(any(root.glob("*.materializing")))

            complete = archive.read_bytes()
            archive.write_bytes(complete[:-1])
            with self.assertRaisesRegex(ValueError, "cannot (decompress|materialize)"):
                SQLiteTraceTopology.open(target)
            self.assertFalse(any(root.glob("*.materializing")))

            archive.write_bytes(b"not a zstd frame")
            with self.assertRaisesRegex(ValueError, "cannot (decompress|materialize)"):
                SQLiteTraceTopology.open(target)
            self.assertFalse(any(root.glob("*.materializing")))

    def test_degraded_shared_subtree_uses_the_visible_dag_parent(self):
        events = [{
            "type": "TEST_START", "seq": 1, "class": "p.Test",
            "method": "testCase", "agent_protocol_version": 5,
            "value_capture": CAPTURE_DISABLED,
        }, {
            "type": "ENTER", "seq": 2, "ts_ns": 2, "thread_id": 1,
            "thread_name": "main", "invocation_id": 1, "parent_id": 0,
            "class": "p.Test", "method": "testCase", "descriptor": "()V",
            "origin_test_line": 0,
        }]
        seq = 3
        invocation_id = 2
        parent_ids = {}
        for parent_method in ("first", "second"):
            parent_id = invocation_id
            parent_ids[parent_method] = parent_id
            child_id = invocation_id + 1
            events.extend([{
                "type": "ENTER", "seq": seq, "ts_ns": seq,
                "thread_id": 1, "thread_name": "main",
                "invocation_id": parent_id, "parent_id": 1,
                "class": "p.Service", "method": parent_method,
                "descriptor": "()V", "origin_test_line": 10,
            }, {
                "type": "ENTER", "seq": seq + 1, "ts_ns": seq + 1,
                "thread_id": 1, "thread_name": "main",
                "invocation_id": child_id, "parent_id": parent_id,
                "class": "p.Helper", "method": "shared",
                "descriptor": "()V", "origin_test_line": 10,
            }, {
                "type": "RETURN", "seq": seq + 2, "ts_ns": seq + 2,
                "thread_id": 1, "invocation_id": child_id,
                "duration_ns": 1,
            }, {
                "type": "RETURN", "seq": seq + 3, "ts_ns": seq + 3,
                "thread_id": 1, "invocation_id": parent_id,
                "duration_ns": 3,
            }])
            seq += 4
            invocation_id += 2
        events.extend([{
            "type": "RETURN", "seq": seq, "ts_ns": seq, "thread_id": 1,
            "invocation_id": 1, "duration_ns": seq,
        }, {
            "type": "TEST_END", "seq": seq + 1, "successful": False,
            "failure_count": 1,
        }])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.jsonl.zst"
            with zstandard.open(raw, "wt", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            summary = raw_events_to_degraded_store(
                raw, root / TRACE_STORE_NAME, root / METHOD_SUMMARY_NAME,
                project="P", test="p.Test::testCase", test_class="p.Test",
                test_method="testCase", process_exit_code=1,
                assertion_instrumentation={"configured_ranges": ""},
                defect_context={"error_stack": "failure", "test_output": "failed"},
                capture_config=CAPTURE_DISABLED, threshold_bytes=1,
            )
            catalog, method_ids, fingerprint = build_method_catalog_from_keys(
                tuple(item) for item in summary["methods"]
            )
            target = root / "T1.trace.sqlite3"
            finalize_trace_store(
                root / TRACE_STORE_NAME, target, project="P", test_id="T1",
                test="p.Test::testCase", method_ids=method_ids,
                catalog_fingerprint=fingerprint,
            )
            topology = SQLiteTraceTopology.open(target)
            plan = plan_focus_viewport(
                diagram_id="D1", focus_invocation_id=parent_ids["second"],
                topology=topology, max_upstream_calls=1,
                max_downstream_calls=1, max_internal_calls=2,
            )
            shared = next(
                item for item in plan["_numbered_items"].values()
                if item["invocation"]["method"] == "shared"
            )
            second = plan["_numbered_items"][parent_ids["second"]]
            self.assertEqual(shared["call"]["caller_method"], "second")
            self.assertEqual(
                shared["invocation"]["parent_id"], parent_ids["second"]
            )
            self.assertLess(
                shared["invocation"]["enter_seq"],
                second["invocation"]["enter_seq"],
            )
            self.assertGreater(
                shared["invocation"]["display_enter_seq"],
                second["invocation"]["display_enter_seq"],
            )
            self.assertLess(
                shared["invocation"]["display_exit_seq"],
                second["invocation"]["display_exit_seq"],
            )
            graph_root = root / "inspection_graphs"
            graph_root.mkdir()
            focus_graph_diagram_nodes(
                graph_root,
                {"entry_diagram_id": "D1", "nodes": [plan]},
                method_ids,
                "T1",
                "degraded shared subtree",
            )
            puml = (graph_root / "D1.puml").read_text(encoding="utf-8")
            self.assertLess(puml.index("second()"), puml.index("shared()"))
            topology.connection.close()

    def test_fast_sidecar_matches_existing_trace_semantics(self):
        capture = {**CAPTURE_DISABLED, "capture_values": True}
        empty_arguments = {
            "count": 0, "items": [], "omitted_count": 0,
            "truncated": False,
        }
        void_result = {
            "declared_type": "void", "runtime_type": "", "kind": "void",
            "text": "", "truncated": False,
        }
        events = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test",
             "method": "testCase", "agent_protocol_version": 5,
             "value_capture": capture},
            {"type": "ENTER", "seq": 2, "ts_ns": 10, "thread_id": 1,
             "thread_name": "main", "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V",
             "origin_test_line": 0, "arguments": empty_arguments},
            {"type": "ENTER", "seq": 3, "ts_ns": 20, "thread_id": 1,
             "thread_name": "main", "invocation_id": 2, "parent_id": 1,
             "class": "p.Service", "method": "outer", "descriptor": "(I)V",
             "origin_test_line": 10,
             "arguments": {
                 "count": 1, "omitted_count": 0, "truncated": False,
                 "items": [{
                     "index": 0, "declared_type": "int",
                     "runtime_type": "java.lang.Integer", "kind": "number",
                     "text": "7", "truncated": False,
                 }],
             }},
            {"type": "ENTER", "seq": 4, "ts_ns": 21, "thread_id": 1,
             "thread_name": "main", "invocation_id": 3, "parent_id": 2,
             "class": "p.Helper", "method": "inner", "descriptor": "()V",
             "origin_test_line": 10, "arguments": empty_arguments},
            {"type": "RETURN", "seq": 5, "ts_ns": 22, "thread_id": 1,
             "invocation_id": 3, "duration_ns": 1,
             "return_value": void_result},
            {"type": "RETURN", "seq": 6, "ts_ns": 23, "thread_id": 1,
             "invocation_id": 2, "duration_ns": 3,
             "return_value": void_result},
            {"type": "ENTER", "seq": 7, "ts_ns": 30, "thread_id": 1,
             "thread_name": "main", "invocation_id": 4, "parent_id": 1,
             "class": "p.Service", "method": "fail", "descriptor": "()V",
             "origin_test_line": 11, "arguments": empty_arguments},
            {"type": "THROW", "seq": 8, "ts_ns": 31, "thread_id": 1,
             "invocation_id": 4, "duration_ns": 1,
             "exception_class": "java.lang.IllegalStateException",
             "message": "bad"},
            {"type": "RETURN", "seq": 9, "ts_ns": 40, "thread_id": 1,
             "invocation_id": 1, "duration_ns": 30,
             "return_value": void_result},
            {"type": "TEST_FAILURE", "seq": 10,
             "exception_class": "java.lang.AssertionError", "message": "bad"},
            {"type": "TEST_END", "seq": 11, "successful": False,
             "failure_count": 1},
        ]
        instrumentation = {
            "schema": "assertion-instrumentation",
            "schema_version": 1,
            "configured_ranges": "",
        }
        defect_context = {
            "schema": "defect-context",
            "schema_version": 1,
            "test": "p.Test::testCase",
            "error_stack": "java.lang.AssertionError: bad",
            "test_output": "failed",
        }
        full = build_trace(events)
        full.update({
            "project": "P",
            "test": {"class": "p.Test", "method": "testCase"},
            "process_exit_code": 1,
            "assertion_instrumentation": instrumentation,
        })
        execution = project_execution(full, "p.Test", "testCase")
        execution["project"] = "P"
        execution["process_exit_code"] = 1
        pruned, folding = fold_successful_assertions(execution)
        catalog, method_ids, catalog_fingerprint = build_method_catalog([pruned])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw_events.jsonl.zst"
            with zstandard.open(raw, "wt", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            general = root / "general"
            general.mkdir()
            raw_events_to_store(
                raw, general / TRACE_STORE_NAME, general / METHOD_SUMMARY_NAME,
                project="P", test="p.Test::testCase", test_class="p.Test",
                test_method="testCase", process_exit_code=1,
                assertion_instrumentation=instrumentation,
                defect_context=defect_context,
                capture_config=capture,
            )
            general_target = general / "T1.refinement-trace.json.zst"
            general_fingerprint = write_refinement_trace_from_store(
                general / TRACE_STORE_NAME, general_target, project="P",
                test_id="T1", test="p.Test::testCase", method_ids=method_ids,
                catalog_fingerprint=catalog_fingerprint,
            )
            expected = validate_refinement_trace(
                read_zstd_json(general_target)
            )
            summary = raw_events_to_fast_store(
                raw, root / TRACE_STORE_NAME, root / METHOD_SUMMARY_NAME,
                project="P", test="p.Test::testCase", test_class="p.Test",
                test_method="testCase", process_exit_code=1,
                assertion_instrumentation=instrumentation,
                defect_context=defect_context,
                capture_config=capture,
            )
            store = root / TRACE_STORE_NAME
            self.assertTrue(is_fast_trace_store(store))
            self.assertEqual(summary["call_count"], 3)
            with sqlite3.connect(store) as connection:
                tables = {
                    str(row[0]) for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("fast_nodes", tables)
                self.assertNotIn("invocations", tables)
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM fast_nodes WHERE is_call=1"
                    ).fetchone()[0],
                    3,
                )
            self.assertTrue(raw.is_file())
            self.assertTrue(is_fast_trace_store(store))
            target = root / "T1.refinement-trace.json.zst"
            fingerprint = write_refinement_trace_from_store(
                store, target, project="P", test_id="T1",
                test="p.Test::testCase", method_ids=method_ids,
                catalog_fingerprint=catalog_fingerprint,
            )
            actual = validate_refinement_trace(read_zstd_json(target))
        self.assertEqual(actual, expected)
        self.assertEqual(fingerprint, general_fingerprint)

    def test_streamed_converter_matches_the_existing_trace_semantics(self):
        events = assertion_events()
        instrumentation = {
            "schema": "assertion-instrumentation",
            "schema_version": 1,
            "configured_ranges": "A001:10-10",
        }
        defect_context = {
            "schema": "defect-context",
            "schema_version": 1,
            "test": "p.Test::testCase",
            "error_stack": "java.lang.AssertionError: bad",
            "test_output": "failed",
        }

        full = build_trace(events)
        full.update({
            "project": "P",
            "test": {"class": "p.Test", "method": "testCase"},
            "process_exit_code": 1,
            "assertion_instrumentation": instrumentation,
        })
        execution = project_execution(full, "p.Test", "testCase")
        execution["project"] = "P"
        execution["process_exit_code"] = 1
        pruned, folding = fold_successful_assertions(execution)
        catalog, method_ids, catalog_fingerprint = build_method_catalog([pruned])
        expected = build_refinement_trace(
            pruned,
            project="P",
            test_id="T1",
            test="p.Test::testCase",
            method_ids=method_ids,
            catalog_fingerprint=catalog_fingerprint,
            assertion_folding=folding,
            error_stack=defect_context["error_stack"],
            test_output=defect_context["test_output"],
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw_events.jsonl.zst"
            with zstandard.open(raw, "wt", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event) + "\n")
            summary = raw_events_to_store(
                raw,
                root / TRACE_STORE_NAME,
                root / METHOD_SUMMARY_NAME,
                project="P",
                test="p.Test::testCase",
                test_class="p.Test",
                test_method="testCase",
                process_exit_code=1,
                assertion_instrumentation=instrumentation,
                defect_context=defect_context,
                capture_config=CAPTURE_DISABLED,
            )
            self.assertEqual(summary["call_count"], 1)
            self.assertEqual(summary["methods"], [["p.Service", "mutate", "()V"]])
            store = root / TRACE_STORE_NAME
            with sqlite3.connect(store) as connection:
                invocation_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(invocations)")
                }
                self.assertNotIn("exit_seq", invocation_columns)
                self.assertNotIn("return_value_json", invocation_columns)
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM exits").fetchone()[0],
                    4,
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM calls").fetchone()[0],
                    3,
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM folded").fetchone()[0],
                    2,
                )
            self.assertTrue(store.is_file())
            self.assertEqual(
                read_available_trace_summary(root, ""), summary
            )
            streamed_catalog, streamed_ids, streamed_catalog_fingerprint = (
                build_method_catalog_from_keys(tuple(item) for item in summary["methods"])
            )
            self.assertEqual(streamed_catalog, catalog)
            self.assertEqual(streamed_catalog_fingerprint, catalog_fingerprint)
            target = root / "T1.refinement-trace.json.zst"
            fingerprint = write_refinement_trace_from_store(
                store,
                target,
                project="P",
                test_id="T1",
                test="p.Test::testCase",
                method_ids=streamed_ids,
                catalog_fingerprint=streamed_catalog_fingerprint,
            )
            actual = validate_refinement_trace(read_zstd_json(target))
        self.assertEqual(fingerprint, expected["fingerprint"])
        self.assertEqual(actual, expected)

    def test_corrupt_zstd_is_not_accepted_as_a_partial_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw_events.jsonl.zst"
            raw.write_bytes(b"not a zstd frame")
            with self.assertRaisesRegex(ValueError, "incomplete or corrupt Zstd"):
                raw_events_to_store(
                    raw,
                    root / TRACE_STORE_NAME,
                    root / METHOD_SUMMARY_NAME,
                    project="P",
                    test="p.Test::testCase",
                    test_class="p.Test",
                    test_method="testCase",
                    process_exit_code=1,
                    assertion_instrumentation={},
                    defect_context={},
                    capture_config=CAPTURE_DISABLED,
                )
            self.assertFalse((root / TRACE_STORE_NAME).exists())

    def test_degraded_store_compresses_repeated_subgraphs_and_renders_valid_png_without_values(
        self,
    ):
        capture = {**CAPTURE_DISABLED, "capture_values": True}
        args = {
            "count": 1, "omitted_count": 0, "truncated": False,
            "items": [{
                "index": 0, "declared_type": "java.lang.String",
                "runtime_type": "java.lang.String", "kind": "string",
                "text": "SECRET", "truncated": False,
            }],
        }
        void = {
            "declared_type": "void", "runtime_type": "", "kind": "void",
            "text": "", "truncated": False,
        }
        events = [{
            "type": "TEST_START", "seq": 1, "class": "p.Test",
            "method": "testCase", "agent_protocol_version": 5,
            "value_capture": capture,
        }, {
            "type": "ENTER", "seq": 2, "ts_ns": 1, "thread_id": 1,
            "thread_name": "main", "invocation_id": 1, "parent_id": 0,
            "class": "p.Test", "method": "testCase", "descriptor": "()V",
            "origin_test_line": 0,
            "arguments": {**args, "count": 0, "items": []},
        }]
        seq = 3
        invocation_id = 2
        for _ in range(3):
            for method in ("a", "b"):
                current = invocation_id
                events.append({
                    "type": "ENTER", "seq": seq, "ts_ns": seq,
                    "thread_id": 1, "thread_name": "main",
                    "invocation_id": current, "parent_id": 1,
                    "class": "p.Service", "method": method,
                    "descriptor": "(Ljava/lang/String;)V",
                    "origin_test_line": 10, "arguments": args,
                })
                seq += 1
                if method == "a":
                    invocation_id += 1
                    child = invocation_id
                    events.extend([{
                        "type": "ENTER", "seq": seq, "ts_ns": seq,
                        "thread_id": 1, "thread_name": "main",
                        "invocation_id": child, "parent_id": current,
                        "class": "p.Helper", "method": "x", "descriptor": "()V",
                        "origin_test_line": 10, "arguments": {**args, "count": 0, "items": []},
                    }, {
                        "type": "RETURN", "seq": seq + 1, "ts_ns": seq + 1,
                        "thread_id": 1, "invocation_id": child,
                        "duration_ns": 1, "return_value": void,
                    }])
                    seq += 2
                events.append({
                    "type": "RETURN", "seq": seq, "ts_ns": seq,
                    "thread_id": 1, "invocation_id": current,
                    "duration_ns": 1, "return_value": void,
                })
                seq += 1
                invocation_id += 1
        events.extend([{
            "type": "RETURN", "seq": seq, "ts_ns": seq, "thread_id": 1,
            "invocation_id": 1, "duration_ns": seq, "return_value": void,
        }, {
            "type": "TEST_END", "seq": seq + 1, "successful": False,
            "failure_count": 1,
        }])
        root = DEGRADED_RENDER_RESULT_ROOT
        if root.exists():
            shutil.rmtree(root)
        graph_root = root / "inspection_graphs/degraded"
        graph_root.mkdir(parents=True)
        raw = root / "raw_events.jsonl.zst"
        try:
            with zstandard.open(raw, "wt", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            summary = raw_events_to_degraded_store(
                raw, root / TRACE_STORE_NAME, root / METHOD_SUMMARY_NAME,
                project="P", test="p.Test::testCase", test_class="p.Test",
                test_method="testCase", process_exit_code=1,
                assertion_instrumentation={"configured_ranges": ""},
                defect_context={"error_stack": "failure", "test_output": "failed"},
                capture_config=capture, threshold_bytes=1,
            )
            catalog, method_ids, fingerprint = build_method_catalog_from_keys(
                tuple(item) for item in summary["methods"]
            )
            target = root / "traces/T1.trace.sqlite3"
            target.parent.mkdir()
            finalize_trace_store(
                root / TRACE_STORE_NAME, target, project="P", test_id="T1",
                test="p.Test::testCase", method_ids=method_ids,
                catalog_fingerprint=fingerprint,
            )
            topology = SQLiteTraceTopology.open(target)
            self.assertEqual(topology.trace["call_count"], 9)
            self.assertEqual(topology.stored_call_count, 3)
            self.assertFalse(topology.trace["capture"]["capture_values"])
            self.assertFalse(topology.exact_omission_counts)
            root_id = int(topology.connection.execute(
                "SELECT invocation_id FROM query_nodes WHERE is_call=0"
            ).fetchone()[0])
            self.assertEqual(
                topology.repetition_groups(root_id),
                ({"start": 0, "pattern_length": 2, "repeat_count": 3},),
            )
            a_id = next(iter(topology.method_invocations[
                next(item["method_id"] for item in catalog if item["function"].endswith(".a"))
            ]))
            plan = plan_focus_viewport(
                diagram_id="D1", focus_invocation_id=a_id, topology=topology,
                max_upstream_calls=1, max_downstream_calls=1,
                max_internal_calls=2,
            )
            nodes, _, _, _ = focus_graph_diagram_nodes(
                graph_root, {"entry_diagram_id": "D1", "nodes": [plan]},
                method_ids, "T1", "degraded",
            )
            puml_path = graph_root / "D1.puml"
            image_path = graph_root / "D1.png"
            rendered_path, cache_hit = ensure_rendered(
                puml_path,
                image_path,
                jar=Path(__file__).resolve().parents[1] / "lib/plantuml.jar",
                timeout=30,
                limit_size=32768,
            )
            validate_png(rendered_path, 32768)
            png_header = rendered_path.read_bytes()[:24]
            width = int.from_bytes(png_header[16:20], "big")
            height = int.from_bytes(png_header[20:24], "big")
            puml = puml_path.read_text(encoding="utf-8")
            self.assertTrue(topology.degradation["enabled"])
            self.assertEqual(
                topology.degradation["reason"], "raw_trace_size_threshold"
            )
            self.assertIn("loop repeated sequence ×3", puml)
            self.assertIn(
                "LOOP_START | repetitions=3", nodes[0]["_execution_text"]
            )
            self.assertIn(" LOOP_END", nodes[0]["_execution_text"])
            self.assertNotIn("SECRET", puml)
            self.assertNotIn("SECRET", nodes[0]["_execution_text"])
            self.assertIsNone(nodes[0]["omitted_call_count"])
            self.assertFalse(cache_hit)
            self.assertEqual(png_header[:8], PNG_HEADER)
            self.assertGreater(width, 0)
            self.assertGreater(height, 0)
            self.assertLess(width, 32768)
            self.assertLess(height, 32768)

            result = {
                "schema": "degraded-render-test-result",
                "schema_version": 1,
                "test": (
                    "SQLiteTraceConverterTests."
                    "test_degraded_store_compresses_repeated_subgraphs_and_"
                    "renders_valid_png_without_values"
                ),
                "status": "PASS",
                "degradation": topology.degradation,
                "logical_call_count": topology.trace["call_count"],
                "stored_call_count": topology.stored_call_count,
                "capture_values": topology.trace["capture"]["capture_values"],
                "exact_omission_counts": topology.exact_omission_counts,
                "diagram": {
                    "puml": puml_path.relative_to(root).as_posix(),
                    "image": rendered_path.relative_to(root).as_posix(),
                    "render_manifest": rendered_path.with_suffix(
                        ".png.render.json"
                    ).relative_to(root).as_posix(),
                    "width": width,
                    "height": height,
                    "valid_png": True,
                    "repeated_sequence_rendered": True,
                    "captured_values_omitted": True,
                },
            }
            result_path = root / "result.json"
            result_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                json.loads(result_path.read_text(encoding="utf-8")), result
            )
            self.assertTrue(
                rendered_path.with_suffix(".png.render.json").is_file()
            )
        finally:
            if "topology" in locals():
                topology.connection.close()

    def test_degraded_window_folding_matches_non_degraded_behavior(self):
        root = (
            Path(__file__).resolve().parent
            / "generated/focus_viewport/degraded_window_folding_equivalence"
        )
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        raw_path = root / "raw_events.jsonl.zst"
        with zstandard.open(raw_path, "wt", encoding="utf-8") as handle:
            for event in window_folding_events():
                handle.write(json.dumps(event, separators=(",", ":")) + "\n")

        summaries = {}
        for mode, converter in (
            ("normal", raw_events_to_store),
            ("degraded", raw_events_to_degraded_store),
        ):
            conversion = root / mode / "conversion"
            conversion.mkdir(parents=True)
            arguments = {
                "project": "P",
                "test": "p.Test::testWindow",
                "test_class": "p.Test",
                "test_method": "testWindow",
                "process_exit_code": 1,
                "assertion_instrumentation": {"configured_ranges": ""},
                "defect_context": {
                    "error_stack": "failure",
                    "test_output": "failed",
                },
                "capture_config": CAPTURE_DISABLED,
            }
            if mode == "degraded":
                arguments["threshold_bytes"] = 1
            summaries[mode] = converter(
                raw_path,
                conversion / TRACE_STORE_NAME,
                conversion / METHOD_SUMMARY_NAME,
                **arguments,
            )
        self.assertEqual(
            summaries["normal"]["methods"], summaries["degraded"]["methods"]
        )
        catalog, method_ids, fingerprint = build_method_catalog_from_keys(
            tuple(item) for item in summaries["normal"]["methods"]
        )
        trace_paths = {}
        for mode in ("normal", "degraded"):
            trace_path = root / mode / "traces/T1.trace.sqlite3"
            trace_path.parent.mkdir()
            finalize_trace_store(
                root / mode / "conversion" / TRACE_STORE_NAME,
                trace_path,
                project="P",
                test_id="T1",
                test="p.Test::testWindow",
                method_ids=method_ids,
                catalog_fingerprint=fingerprint,
            )
            trace_paths[mode] = trace_path

        topologies = {
            mode: SQLiteTraceTopology.open(path)
            for mode, path in trace_paths.items()
        }
        try:
            focus_method_id = next(
                item["method_id"]
                for item in catalog
                if item["function"] == "p.Service.focus"
            )
            plans = {}
            for mode, topology in topologies.items():
                focus_id = next(iter(
                    topology.method_invocations[focus_method_id]
                ))
                plans[mode] = plan_focus_viewport(
                    diagram_id="D1",
                    focus_invocation_id=focus_id,
                    topology=topology,
                    max_upstream_calls=2,
                    max_downstream_calls=1,
                    max_internal_calls=1,
                )

            def visible_calls(plan):
                return [
                    [
                        int(item["representative_invocation_id"]),
                        str(item["invocation"]["class"]),
                        str(item["invocation"]["method"]),
                    ]
                    for item in plan["visible_items"]
                ]

            fold_fields = (
                "kind", "scope", "anchor_invocation_id",
                "first_invocation_id", "last_invocation_id",
                "enter_seq", "exit_seq", "boundary_context",
            )

            def fold_boundaries(plan):
                return [
                    {key: fold.get(key) for key in fold_fields}
                    for fold in plan["folds"]
                ]

            normal_plan = plans["normal"]
            degraded_plan = plans["degraded"]
            self.assertFalse(topologies["normal"].degradation["enabled"])
            self.assertTrue(topologies["degraded"].degradation["enabled"])
            self.assertTrue(topologies["normal"].exact_omission_counts)
            self.assertFalse(topologies["degraded"].exact_omission_counts)
            self.assertEqual(
                visible_calls(degraded_plan), visible_calls(normal_plan)
            )
            self.assertEqual(
                fold_boundaries(degraded_plan), fold_boundaries(normal_plan)
            )
            for field in (
                "upstream_visible_call_count",
                "downstream_visible_call_count",
                "internal_visible_call_count",
                "visible_represented_call_count",
                "omitted_region_count",
                "has_omitted_calls",
            ):
                self.assertEqual(degraded_plan[field], normal_plan[field])
            self.assertEqual(normal_plan["omitted_call_count"], 6)
            self.assertIsNone(degraded_plan["omitted_call_count"])
            self.assertEqual(normal_plan["omitted_region_count"], 5)
            fold_scopes = [
                fold["scope"] for fold in degraded_plan["folds"]
            ]
            self.assertEqual(fold_scopes.count("OUTER_CONTEXT"), 1)
            self.assertEqual(fold_scopes.count("CHILDREN"), 4)
            degraded_outer = next(
                fold for fold in degraded_plan["folds"]
                if fold.get("scope") == "OUTER_CONTEXT"
            )
            self.assertTrue(degraded_outer["boundary_context"])
            self.assertTrue(degraded_outer["leading_calls_omitted"])
            self.assertTrue(degraded_outer["trailing_calls_omitted"])

            render_results = {}
            normalized_puml = {}
            for mode, plan in plans.items():
                graph_root = root / mode / "inspection_graphs"
                graph_root.mkdir()
                nodes, entry_id, failures, _ = focus_graph_diagram_nodes(
                    graph_root,
                    {"entry_diagram_id": "D1", "nodes": [plan]},
                    method_ids,
                    "T1",
                    f"{mode} window folding",
                )
                self.assertEqual(entry_id, "D1")
                self.assertEqual(failures, [])
                puml_path = graph_root / "D1.puml"
                image_path = graph_root / "D1.png"
                ensure_rendered(
                    puml_path,
                    image_path,
                    jar=(
                        Path(__file__).resolve().parents[1]
                        / "lib/plantuml.jar"
                    ),
                    timeout=30,
                    limit_size=32768,
                )
                validate_png(image_path, 32768)
                header = image_path.read_bytes()[:24]
                self.assertEqual(header[:8], PNG_HEADER)
                puml = puml_path.read_text(encoding="utf-8")
                for visible_method in (
                    "before2()", "focus()", "child1()", "after1()",
                ):
                    self.assertIn(visible_method, puml)
                for omitted_method in (
                    "farBefore()", "before1()", "deep1()",
                    "child2()", "after2()", "farAfter()",
                ):
                    self.assertNotIn(omitted_method, puml)
                normalized_puml[mode] = "\n".join(
                    "title window folding"
                    if line.startswith("title ")
                    else re.sub(
                        r"\.\.\. omit \d+ calls \.\.\.",
                        "... omitted calls ...",
                        line,
                    )
                    for line in puml.splitlines()
                )
                render_results[mode] = {
                    "puml": puml_path.relative_to(root).as_posix(),
                    "image": image_path.relative_to(root).as_posix(),
                    "width": int.from_bytes(header[16:20], "big"),
                    "height": int.from_bytes(header[20:24], "big"),
                    "valid_png": True,
                    "visible_calls": visible_calls(plan),
                    "fold_boundaries": fold_boundaries(plan),
                    "omitted_call_count": nodes[0]["omitted_call_count"],
                    "omitted_region_count": nodes[0]["omitted_region_count"],
                    "rendered_omission_marker_count": sum(
                        "omitted calls" in line or " omit " in line
                        for line in puml.splitlines()
                    ),
                }

            self.assertEqual(
                normalized_puml["degraded"], normalized_puml["normal"]
            )
            self.assertEqual(
                render_results["normal"]["rendered_omission_marker_count"], 6
            )
            self.assertEqual(
                render_results["degraded"]["rendered_omission_marker_count"], 6
            )

            result = {
                "schema": "degraded-window-folding-comparison",
                "schema_version": 1,
                "status": "PASS",
                "window": {
                    "max_upstream_calls": 2,
                    "max_downstream_calls": 1,
                    "max_internal_calls": 1,
                },
                "storage_modes": {
                    "normal_degradation_enabled": False,
                    "degraded_degradation_enabled": True,
                    "normal_exact_omission_counts": True,
                    "degraded_exact_omission_counts": False,
                },
                "normal": render_results["normal"],
                "degraded": render_results["degraded"],
                "comparison": {
                    "visible_calls_equal": True,
                    "fold_boundaries_equal": True,
                    "window_counts_equal": True,
                    "rendered_omission_regions_equal": (
                        normalized_puml["degraded"] == normalized_puml["normal"]
                    ),
                    "expected_difference": (
                        "degraded mode omits exact omitted-call counts"
                    ),
                },
            }
            result_path = root / "result.json"
            result_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                json.loads(result_path.read_text(encoding="utf-8")), result
            )
        finally:
            for topology in topologies.values():
                topology.connection.close()


if __name__ == "__main__":
    unittest.main()
