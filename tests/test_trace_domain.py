import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from dpex.domain.trace import build_trace, load_events, project_execution, validate_trace
from dpex.domain.assertion_folding import (
    fold_successful_assertions,
    validate_assertion_folding,
)
from dpex.stages.trace import (
    _suite_only_targets,
    _write_trace_provenance,
    _write_trace_suites,
    archive_failed_raw_trace,
    assertion_range_argument,
    classpath_has_class,
    java_xml_compatibility_arguments,
    load_trace_archive_configuration,
    load_trace_configuration,
    DEFAULT_DEGRADATION_RAW_SIZE_BYTES,
    run as run_trace,
)
from dpex.domain.refinement_trace import validate_refinement_trace
from dpex.infrastructure.io import read_zstd_json, write_zstd_json
from dpex.infrastructure.layout import RunLayout
from dpex.infrastructure.trace_store import (
    METHOD_SUMMARY_NAME,
    TRACE_STORE_NAME,
    SQLiteTraceTopology,
    execution_to_store,
    final_trace_archive_path,
)
from dpex.domain.schemas import validate_trace_suite


def events(exit_type="RETURN"):
    result = [
        {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
        {"type": "ENTER", "seq": 2, "ts_ns": 10, "thread_id": 1, "thread_name": "main",
         "invocation_id": 1, "parent_id": 0, "class": "p.Test", "method": "testCase", "descriptor": "()V"},
        {"type": "ENTER", "seq": 3, "ts_ns": 20, "thread_id": 1, "thread_name": "main",
         "invocation_id": 2, "parent_id": 1, "class": "p.Service", "method": "run", "descriptor": "(I)V"},
        {"type": exit_type, "seq": 4, "ts_ns": 30, "thread_id": 1,
         "invocation_id": 2, "duration_ns": 10},
        {"type": "RETURN", "seq": 5, "ts_ns": 40, "thread_id": 1,
         "invocation_id": 1, "duration_ns": 30},
        {"type": "TEST_END", "seq": 6, "successful": exit_type == "RETURN"},
    ]
    if exit_type == "THROW":
        result[3].update({"exception_class": "p.Failure", "message": "bad"})
        result.insert(-1, {"type": "TEST_FAILURE", "seq": 5,
                           "exception_class": "p.Failure", "message": "bad"})
        result[-1]["seq"] = 7
    return result


def value_item(index=None, declared_type="int", runtime_type="java.lang.Integer",
               kind="number", text="3", truncated=False):
    value = {
        "declared_type": declared_type,
        "runtime_type": runtime_type,
        "kind": kind,
        "text": text,
        "truncated": truncated,
    }
    if index is not None:
        value["index"] = index
    return value


def v4_events(capture_values=True):
    config = {
        "capture_values": capture_values,
        "value_max_chars": 120,
        "value_max_items": 8,
        "value_max_depth": 2,
        "value_max_arguments_chars": 480,
    }
    result = events()
    result[0].update({"agent_protocol_version": 4, "value_capture": config})
    if capture_values:
        result[1]["arguments"] = {
            "count": 0, "items": [], "omitted_count": 0, "truncated": False,
        }
        result[2]["arguments"] = {
            "count": 1, "items": [value_item(0)],
            "omitted_count": 0, "truncated": False,
        }
        result[3]["return_value"] = value_item(
            declared_type="void", runtime_type="", kind="void", text=""
        )
        result[4]["return_value"] = value_item(
            declared_type="void", runtime_type="", kind="void", text=""
        )
    return result


def v5_events(capture_values=True):
    result = v4_events(capture_values)
    result[0]["agent_protocol_version"] = 5
    result[0]["value_capture"] = {
        "capture_values": capture_values,
        "value_string_edge_chars": 10,
        "value_container_edge_items": 2,
        "value_nested_container_edge_items": 1,
        "value_max_depth": 2,
        "value_max_arguments": 8,
    }
    return result


class EventParsingTests(unittest.TestCase):
    def test_loads_trace_capture_policy_only_from_json(self):
        expected = {
            "capture_values": True,
            "value_string_edge_chars": 10,
            "value_container_edge_items": 2,
            "value_nested_container_edge_items": 1,
            "value_max_depth": 2,
            "value_max_arguments": 8,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"trace": expected}), encoding="utf-8")
            self.assertEqual(load_trace_configuration(path), {
                **expected,
                "degradation_raw_size_bytes": DEFAULT_DEGRADATION_RAW_SIZE_BYTES,
                "retry_without_values_on_timeout": True,
            })
            self.assertFalse(load_trace_archive_configuration(path))
            path.write_text(json.dumps({"trace": {
                **expected, "archive_final_sqlite": True,
            }}), encoding="utf-8")
            self.assertEqual(load_trace_configuration(path), {
                **expected,
                "degradation_raw_size_bytes": DEFAULT_DEGRADATION_RAW_SIZE_BYTES,
                "retry_without_values_on_timeout": True,
            })
            self.assertTrue(load_trace_archive_configuration(path))
            path.write_text(json.dumps({"trace": {
                **expected, "archive_final_sqlite": "yes",
            }}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be boolean"):
                load_trace_configuration(path)
            path.write_text(json.dumps({"trace": {
                **expected, "retry_without_values_on_timeout": "yes",
            }}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be boolean"):
                load_trace_configuration(path)
            invalid = {**expected, "value_max_arguments_chars": 480}
            path.write_text(json.dumps({"trace": invalid}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must contain"):
                load_trace_configuration(path)

    def test_finds_a_compiled_class_on_directory_classpath(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "p" / "Example.class"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"class")
            self.assertTrue(classpath_has_class(directory, "p.Example"))
            self.assertFalse(classpath_has_class(directory, "p.Missing"))

    def test_patches_only_legacy_jxpath_document_ls_on_modular_java(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            java_home = root / "jdk"
            module = java_home / "jmods" / "java.xml.jmod"
            module.parent.mkdir(parents=True)
            module.write_bytes(b"module")
            xerces = root / "xerces.jar"
            member = "org/w3c/dom/ls/DocumentLS.class"
            with zipfile.ZipFile(xerces, "w") as archive:
                archive.writestr(member, b"legacy-interface")
                archive.writestr("org/w3c/dom/html/HTMLDocument.class", b"conflict")
            output = root / "trigger"

            arguments = java_xml_compatibility_arguments(
                "JxPath", str(xerces), output, str(java_home)
            )

            self.assertEqual(arguments, [
                "--patch-module", f"java.xml={output / 'java_xml_patch'}",
            ])
            self.assertEqual(
                (output / "java_xml_patch" / member).read_bytes(),
                b"legacy-interface",
            )
            self.assertFalse(
                (output / "java_xml_patch/org/w3c/dom/html/HTMLDocument.class")
                .exists()
            )
            self.assertEqual(
                java_xml_compatibility_arguments(
                    "Chart", str(xerces), output, str(java_home)
                ),
                [],
            )

    def test_builds_full_trace_with_parent_chain_and_exit(self):
        trace = build_trace(events("THROW"))
        self.assertEqual(trace["schema_version"], 3)
        self.assertEqual(trace["calls"][0]["parent_chain"], [1])
        self.assertEqual(trace["calls"][0]["exit_type"], "THROW")
        validate_trace(trace)

    def test_v4_strictly_validates_values_and_capture_status(self):
        trace = build_trace(v4_events())
        self.assertEqual(trace["schema_version"], 4)
        self.assertEqual(trace["invocations"][1]["arguments"]["count"], 1)
        self.assertEqual(trace["invocations"][1]["return_value"]["kind"], "void")

        invalid_count = v4_events()
        invalid_count[2]["arguments"]["count"] = 2
        with self.assertRaisesRegex(ValueError, "argument counts"):
            build_trace(invalid_count)

        invalid_truncation = v4_events()
        invalid_truncation[2]["arguments"]["truncated"] = True
        with self.assertRaisesRegex(ValueError, "truncation"):
            build_trace(invalid_truncation)

        missing_return = v4_events()
        missing_return[3].pop("return_value")
        with self.assertRaisesRegex(ValueError, "return capture"):
            build_trace(missing_return)

        throw_with_return = v4_events()
        throw_with_return[3]["type"] = "THROW"
        with self.assertRaisesRegex(ValueError, "THROW invocation has return"):
            build_trace(throw_with_return)

    def test_v4_disabled_capture_has_no_synthesized_values(self):
        trace = build_trace(v4_events(False))
        self.assertEqual(trace["schema_version"], 4)
        self.assertFalse(any("arguments" in item for item in trace["invocations"]))
        self.assertFalse(any("return_value" in item for item in trace["invocations"]))
        legacy = build_trace(events())
        self.assertEqual(legacy["schema_version"], 3)
        self.assertFalse(any("arguments" in item for item in legacy["invocations"]))

    def test_v5_rejects_removed_argument_character_budget(self):
        trace = build_trace(v5_events())
        self.assertEqual(trace["schema_version"], 6)
        invalid = v5_events()
        invalid[0]["value_capture"]["value_max_arguments_chars"] = 480
        with self.assertRaisesRegex(ValueError, "capture configuration"):
            build_trace(invalid)
        unknown_protocol = v5_events()
        unknown_protocol[0]["agent_protocol_version"] = 6
        with self.assertRaisesRegex(ValueError, "protocol version"):
            build_trace(unknown_protocol)

    def test_rejects_duplicate_invocation(self):
        value = events()
        value.insert(2, dict(value[1]))
        with self.assertRaisesRegex(ValueError, "duplicate invocation"):
            build_trace(value)

    def test_rejects_exit_without_enter_and_unclosed_invocation(self):
        with self.assertRaisesRegex(ValueError, "requires at least one ENTER"):
            build_trace([{"type": "RETURN", "invocation_id": 7}])
        with self.assertRaisesRegex(ValueError, "exit without ENTER"):
            build_trace([
                {"type": "ENTER", "invocation_id": 1, "parent_id": 0},
                {"type": "RETURN", "invocation_id": 7},
            ])
        with self.assertRaisesRegex(ValueError, "unclosed invocations"):
            build_trace(events()[:-3])

    def test_recovers_event_write_gaps_during_terminal_stack_overflow(self):
        value = events("THROW")
        value[3] = {
            "type": "THROW", "seq": 4, "ts_ns": 30, "thread_id": 1,
            "invocation_id": 999, "duration_ns": 10,
            "exception_class": "java.lang.StackOverflowError", "message": "",
        }
        value[-2]["exception_class"] = "java.lang.StackOverflowError"
        trace = build_trace(value)
        self.assertEqual(
            trace["event_recovery"],
            {
                "reason": "terminal_stack_overflow",
                "ignored_exit_without_enter_ids": [999],
                "synthesized_throw_invocation_ids": [2],
            },
        )
        recovered = next(
            item for item in trace["invocations"] if item["invocation_id"] == 2
        )
        self.assertEqual(recovered["exit_type"], "THROW")
        self.assertEqual(recovered["exception_class"], "java.lang.StackOverflowError")
        self.assertEqual(trace["calls"][0]["exit_type"], "THROW")

    def test_load_events_rejects_corrupt_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text('{"type":"ENTER"}\nnot-json\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 2"):
                load_events(path)

    def test_removes_complete_class_initializer_subtree(self):
        value = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V"},
            {"type": "ENTER", "seq": 3, "invocation_id": 2, "parent_id": 1,
             "class": "p.StaticState", "method": "<clinit>", "descriptor": "()V"},
            {"type": "ENTER", "seq": 4, "invocation_id": 3, "parent_id": 2,
             "class": "p.Helper", "method": "load", "descriptor": "()V"},
            {"type": "RETURN", "seq": 5, "invocation_id": 3},
            {"type": "RETURN", "seq": 6, "invocation_id": 2},
            {"type": "RETURN", "seq": 7, "invocation_id": 1},
            {"type": "TEST_END", "seq": 8, "successful": True},
        ]
        trace = build_trace(value)
        self.assertEqual(trace["invocation_count"], 1)
        self.assertEqual(trace["call_count"], 0)
        self.assertNotIn("<clinit>", json.dumps(trace))
        self.assertNotIn("p.Helper.load", json.dumps(trace))


class ExecutionProjectionTests(unittest.TestCase):
    def test_trace_suite_accepts_per_trigger_capture_timeout_fallback(self):
        capture = {
            "capture_values": True,
            "value_string_edge_chars": 10,
            "value_container_edge_items": 2,
            "value_nested_container_edge_items": 1,
            "value_max_depth": 2,
            "value_max_arguments": 8,
        }
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory))
            layout.ensure()
            grouped = {("P", "1"): []}
            for index, capture_values in enumerate((True, False), 1):
                trigger = layout.trigger_dir("P", "1", index)
                trigger.mkdir(parents=True)
                trace = build_trace(v5_events(capture_values))
                execution = project_execution(trace, "p.Test", "testCase")
                execution.update({"project": "P", "process_exit_code": 1})
                pruned, folding = fold_successful_assertions(execution)
                execution_to_store(
                    pruned,
                    trigger / TRACE_STORE_NAME,
                    trigger / METHOD_SUMMARY_NAME,
                    test="p.Test::testCase",
                    assertion_folding=folding,
                    defect_context={
                        "schema": "defect-context",
                        "schema_version": 1,
                        "test": "p.Test::testCase",
                        "error_stack": "AssertionError",
                        "test_output": "failed",
                    },
                )
                effective = {**capture, "capture_values": capture_values}
                _write_trace_provenance(
                    trigger,
                    requested_capture=capture,
                    effective_capture=effective,
                    capture_attempt_count=1 if capture_values else 2,
                    fallback_reason=None if capture_values else "capture_timeout",
                    storage_mode="normal" if capture_values else "degraded",
                )
                grouped[("P", "1")].append(
                    ("P", "1", str(index), trigger)
                )
            _write_trace_suites(
                layout,
                grouped,
                trace_config={
                    **capture,
                    "degradation_raw_size_bytes": (
                        DEFAULT_DEGRADATION_RAW_SIZE_BYTES
                    ),
                    "retry_without_values_on_timeout": True,
                },
                retain_debug_artifacts=True,
            )
            summary = json.loads((
                layout.artifacts / "P/bug_1/trace_suite_summary.json"
            ).read_text())
            self.assertEqual(
                [item["capture_values"] for item in summary["tests"]],
                [True, False],
            )
            self.assertEqual(
                summary["tests"][1]["fallback_reason"], "capture_timeout"
            )
            self.assertEqual(
                len(_suite_only_targets(layout, ["P"], {"1"}, None)), 2
            )

    def test_trace_suite_consolidates_one_lean_normalized_trace(self):
        execution = project_execution(build_trace(events()), "p.Test", "testCase")
        execution.update({"project": "P", "process_exit_code": 1})
        pruned, folding = fold_successful_assertions(execution)
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory))
            layout.ensure()
            trigger = layout.trigger_dir("P", "1", 1)
            trigger.mkdir(parents=True)
            (trigger / "collect.json").write_text("collected")
            (trigger / "trigger_test.txt").write_text("p.Test::testCase\n")
            legacy_duplicates = [
                trigger / "execution.json",
                trigger / "execution_assertion_pruned.json",
                trigger / "trace_index.json",
                trigger / "raw_events.jsonl",
            ]
            for path in legacy_duplicates:
                path.write_text("obsolete")
            execution_to_store(
                pruned, trigger / TRACE_STORE_NAME,
                trigger / METHOD_SUMMARY_NAME,
                test="p.Test::testCase", assertion_folding=folding,
                defect_context={
                    "schema": "defect-context",
                    "schema_version": 1,
                    "test": "p.Test::testCase",
                    "error_stack": "AssertionError",
                    "test_output": "failed",
                },
            )
            grouped = {("P", "1"): [("P", "1", "1", trigger)]}
            _write_trace_suites(
                layout, grouped,
                trace_config={
                    "capture_values": False,
                    "value_string_edge_chars": 10,
                    "value_container_edge_items": 2,
                    "value_nested_container_edge_items": 1,
                    "value_max_depth": 2,
                    "value_max_arguments": 8,
                    "degradation_raw_size_bytes": DEFAULT_DEGRADATION_RAW_SIZE_BYTES,
                },
                retain_debug_artifacts=False,
                archive_final_sqlite=True,
            )
            suite = json.loads((
                layout.artifacts / "P/bug_1/trace_suite.json"
            ).read_text())
            trace_path = (
                layout.artifacts / "P/bug_1" / suite["tests"][0]["trace"]
            )
            self.assertFalse(trace_path.exists())
            self.assertTrue(final_trace_archive_path(trace_path).is_file())
            validate_trace_suite(suite, trace_path.parent.parent)
            topology = SQLiteTraceTopology.open(trace_path)
            trace = topology.trace
            self.assertEqual(suite["schema_version"], 3)
            suite_summary = json.loads((
                layout.artifacts / "P/bug_1/trace_suite_summary.json"
            ).read_text())
            self.assertEqual(suite_summary["schema_version"], 3)
            self.assertEqual(
                suite_summary["tests"][0]["capture_attempt_count"], 1
            )
            legacy_summary = {
                **suite_summary,
                "schema_version": 2,
                "trace_config": {
                    key: value
                    for key, value in suite_summary["trace_config"].items()
                    if key != "retry_without_values_on_timeout"
                },
                "tests": [{
                    key: value
                    for key, value in item.items()
                    if key in {"test_id", "trace_fingerprint", "call_count"}
                } for item in suite_summary["tests"]],
            }
            summary_path = (
                layout.artifacts / "P/bug_1/trace_suite_summary.json"
            )
            summary_path.write_text(json.dumps(legacy_summary))
            self.assertEqual(
                len(_suite_only_targets(layout, ["P"], {"1"}, None)), 1
            )
            summary_path.write_text(json.dumps(suite_summary))
            self.assertEqual(
                {key: suite_summary["trace_config"][key] for key in trace["capture"]},
                trace["capture"],
            )
            self.assertEqual(suite_summary["tests"][0]["call_count"], 1)
            self.assertEqual(trace["failure"]["error_stack"], "AssertionError")
            self.assertEqual(trace["call_count"], 1)
            topology.close()
            self.assertFalse(any(trace_path.parent.glob("*.materializing")))
            self.assertFalse((trigger / TRACE_STORE_NAME).exists())
            self.assertFalse((trigger / "collect.json").exists())
            self.assertFalse((trigger / "trigger_test.txt").exists())
            self.assertFalse(any(path.exists() for path in legacy_duplicates))
            suite_path = layout.artifacts / "P/bug_1/trace_suite.json"
            old_suite = {**suite, "schema_version": 1}
            suite_path.write_text(json.dumps(old_suite))
            self.assertEqual(
                _suite_only_targets(layout, ["P"], {"1"}, None), []
            )
            suite_path.write_text(json.dumps(suite))
            recovered = _suite_only_targets(
                layout, ["P"], {"1"}, None
            )
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0][0:3], ("P", "1", "1"))
            self.assertEqual(recovered[0][4], "p.Test::testCase")
            self.assertEqual(recovered[0][5]["call_count"], 1)
            # Suite discovery is deliberately metadata-only and never opens
            # the large final trace payload.
            trace_path.write_bytes(b"not-a-zstd-stream")
            self.assertEqual(
                len(_suite_only_targets(layout, ["P"], {"1"}, None)), 1
            )

            agent_jar = layout.root / "agent.jar"
            agent_jar.write_bytes(b"jar")
            config_path = layout.root / "config.json"
            trace_config = {
                "capture_values": True,
                "value_string_edge_chars": 10,
                "value_container_edge_items": 2,
                "value_nested_container_edge_items": 1,
                "value_max_depth": 2,
                "value_max_arguments": 8,
            }
            config_path.write_text(json.dumps({"trace": trace_config}))

            def retrace(*args, **kwargs):
                output = args[1]
                self.assertEqual(args[8], {
                    **trace_config,
                    "degradation_raw_size_bytes": DEFAULT_DEGRADATION_RAW_SIZE_BYTES,
                    "retry_without_values_on_timeout": True,
                })
                self.assertEqual(
                    (output / "trigger_test.txt").read_text().strip(),
                    "p.Test::testCase",
                )
                self.assertFalse((output / "collect.json").exists())
                execution_to_store(
                    pruned, output / TRACE_STORE_NAME,
                    output / METHOD_SUMMARY_NAME,
                    test="p.Test::testCase", assertion_folding=folding,
                    defect_context={
                        "schema": "defect-context",
                        "schema_version": 1,
                        "test": "p.Test::testCase",
                        "error_stack": "AssertionError",
                        "test_output": "failed",
                    },
                )
                _write_trace_provenance(
                    output,
                    requested_capture=trace_config,
                    effective_capture={**trace_config, "capture_values": False},
                    capture_attempt_count=2,
                    fallback_reason="capture_timeout",
                    storage_mode="degraded",
                )
                return {"call_count": pruned["call_count"]}

            with (
                patch(
                    "dpex.stages.trace.defects4j_environment",
                    return_value={},
                ),
                patch("dpex.stages.trace.trace_trigger", side_effect=retrace),
            ):
                rerun = run_trace(
                    layout, ["P"], {"1"}, None, agent_jar,
                    None, None, config_path, 5, force=True,
                )
            self.assertEqual(rerun[0]["status"], "OK")
            self.assertTrue((
                layout.artifacts / "P/bug_1/trace_suite.json"
            ).is_file())
            self.assertFalse(trigger.exists())

    def test_failed_raw_trace_is_stream_compressed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            raw = output / "raw_events.jsonl"
            raw.write_text('{"type":"TEST_START"}\n', encoding="utf-8")
            target = archive_failed_raw_trace(output)
            self.assertEqual(target, output / "raw_events.failed.jsonl.zst")
            self.assertFalse(raw.exists())
            import zstandard
            with zstandard.open(target, "rb") as handle:
                self.assertEqual(handle.read(), b'{"type":"TEST_START"}\n')

    def test_folds_only_complete_passed_assertion_subtrees(self):
        raw = [
            {"type": "TEST_START", "seq": 1, "class": "p.Test", "method": "testCase"},
            {"type": "ENTER", "seq": 2, "invocation_id": 1, "parent_id": 0,
             "class": "p.Test", "method": "testCase", "descriptor": "()V",
             "thread_id": 1, "thread_name": "main", "origin_test_line": 0},
            {"type": "ASSERT_START", "seq": 3, "assertion_id": "A001",
             "source_start_line": 10, "source_end_line": 10,
             "thread_id": 1, "thread_name": "main"},
            {"type": "ENTER", "seq": 4, "invocation_id": 2, "parent_id": 1,
             "class": "p.Service", "method": "observe", "descriptor": "()V",
             "thread_id": 1, "thread_name": "main", "origin_test_line": 10},
            {"type": "ENTER", "seq": 5, "invocation_id": 3, "parent_id": 2,
             "class": "p.Helper", "method": "read", "descriptor": "()V",
             "thread_id": 1, "thread_name": "main", "origin_test_line": 10},
            {"type": "RETURN", "seq": 6, "invocation_id": 3,
             "thread_id": 1, "duration_ns": 1},
            {"type": "RETURN", "seq": 7, "invocation_id": 2,
             "thread_id": 1, "duration_ns": 3},
            {"type": "ASSERT_PASS", "seq": 8, "assertion_id": "A001",
             "source_start_line": 10, "source_end_line": 10,
             "thread_id": 1, "thread_name": "main"},
            {"type": "ENTER", "seq": 9, "invocation_id": 4, "parent_id": 1,
             "class": "p.Service", "method": "mutate", "descriptor": "()V",
             "thread_id": 1, "thread_name": "main", "origin_test_line": 11},
            {"type": "RETURN", "seq": 10, "invocation_id": 4,
             "thread_id": 1, "duration_ns": 1},
            {"type": "ASSERT_START", "seq": 11, "assertion_id": "A002",
             "source_start_line": 12, "source_end_line": 12,
             "thread_id": 1, "thread_name": "main"},
            {"type": "ENTER", "seq": 12, "invocation_id": 5, "parent_id": 1,
             "class": "p.Service", "method": "broken", "descriptor": "()V",
             "thread_id": 1, "thread_name": "main", "origin_test_line": 12},
            {"type": "THROW", "seq": 13, "invocation_id": 5,
             "thread_id": 1, "duration_ns": 1,
             "exception_class": "java.lang.AssertionError", "message": "bad"},
            {"type": "ASSERT_FAIL", "seq": 14, "assertion_id": "A002",
             "source_start_line": 12, "source_end_line": 12,
             "thread_id": 1, "thread_name": "main",
             "exception_class": "java.lang.AssertionError", "message": "bad"},
            {"type": "THROW", "seq": 15, "invocation_id": 1,
             "thread_id": 1, "duration_ns": 13,
             "exception_class": "java.lang.AssertionError", "message": "bad"},
            {"type": "TEST_FAILURE", "seq": 16,
             "exception_class": "java.lang.AssertionError", "message": "bad"},
            {"type": "TEST_END", "seq": 17, "successful": False},
        ]
        execution = project_execution(build_trace(raw), "p.Test", "testCase")
        pruned, metadata = fold_successful_assertions(execution)

        self.assertIs(validate_assertion_folding(metadata), metadata)
        self.assertEqual(metadata["successful_assertion_count"], 1)
        self.assertEqual(metadata["failed_assertion_count"], 1)
        self.assertEqual(metadata["folded_call_count"], 2)
        self.assertEqual(metadata["folds"][0]["invocation_ids"], [2, 3])
        self.assertEqual(
            [call["callee"] for call in pruned["calls"]],
            ["p.Service.mutate", "p.Service.broken"],
        )

    def test_extracts_assertion_ranges_without_dependency_slicing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Test.java"
            source.write_text(
                "package p; class Test { void testCase() {\n"
                "  int value = helper();\n"
                "  assert value > 0;\n"
                "  assertEquals(1,\n"
                "      value);\n"
                "} int helper() { return 1; } }\n",
                encoding="utf-8",
            )
            value = assertion_range_argument(source, "p.Test", "testCase")
        self.assertEqual(value, "A001:3-3;A002:4-5")

    def test_preserves_every_filtered_call_and_thread(self):
        trace = build_trace(events())
        execution = project_execution(trace, "p.Test", "testCase")
        self.assertEqual(execution["schema"], "fullchain-execution")
        self.assertEqual(execution["original_call_count"], 1)
        self.assertEqual(execution["filtered_call_count"], 1)
        self.assertEqual(execution["call_count"], 1)
        self.assertEqual(execution["calls"][0]["thread_id"], 1)

    def test_does_not_compress_repeated_sibling_calls(self):
        repeated = events()
        repeated.insert(4, {
            "type": "ENTER", "seq": 5, "ts_ns": 31, "thread_id": 1,
            "thread_name": "main", "invocation_id": 3, "parent_id": 1,
            "class": "p.Service", "method": "run", "descriptor": "(I)V",
        })
        repeated.insert(5, {
            "type": "RETURN", "seq": 6, "ts_ns": 32, "thread_id": 1,
            "invocation_id": 3, "duration_ns": 1,
        })
        repeated[6]["seq"] = 7
        repeated[7]["seq"] = 8
        execution = project_execution(build_trace(repeated), "p.Test", "testCase")
        self.assertEqual(execution["call_count"], 2)
        self.assertTrue(all(
            "count" not in call
            and "context" not in call
            and "invocation_ids" not in call
            for call in execution["calls"]
        ))
