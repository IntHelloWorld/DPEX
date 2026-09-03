import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mllmfl.domain.schemas import (
    validate_localization_input,
    validate_refinement,
)
from mllmfl.domain.assertion_folding import fold_successful_assertions
from mllmfl.domain.focus_viewport import plan_focus_viewport
from mllmfl.domain.refinement_trace import (
    RefinementTraceTopology,
    build_method_catalog,
    build_refinement_trace,
    validate_refinement_trace,
)
from mllmfl.infrastructure.io import write_zstd_json
from mllmfl.infrastructure.layout import RunLayout
from mllmfl.stages.refine.agent import run_agent
from mllmfl.stages.refine.adapters import _autofl_diagnosis
from mllmfl.stages.refine.context import (
    SYSTEM_PROMPT,
    build_prompt,
    selected_trace_tests,
)
from mllmfl.stages.refine.input import load_localization_input
from mllmfl.stages.refine.graphs import (
    EXECUTION_GRAPH_TOOL,
    FIND_METHOD_INVOCATION_ID_TOOL,
    MethodExecutionGraphs,
)
from mllmfl.stages.refine.parsing import validate_model_refinement
from mllmfl.stages.refine.shell import (
    BASH_TOOL,
    execute_bash,
    validate_bash_command,
    validate_max_output_chars,
)
from mllmfl.stages.refine.stage import _refine_bug
from mllmfl.infrastructure.method_location import resolve_source_method_reference


def candidate() -> dict:
    return {
        "candidate_id": "L001",
        "function": "p.Service.run",
        "signature": "p.Service.run()",
        "source_file": "src/p/Service.java",
        "start_line": 3,
        "end_line": 7,
        "rank": 1,
        "score": 0.8,
        "reason": "upstream",
    }


def write_fixture_sources(workspace: Path, execution: dict) -> dict[str, str]:
    methods: dict[str, list[str]] = {}
    for invocation in execution["invocations"]:
        class_name = str(invocation["class"])
        method = str(invocation["method"])
        if method == "<init>":
            continue
        methods.setdefault(class_name, [])
        if method not in methods[class_name]:
            methods[class_name].append(method)
    references = {}
    for class_name, names in methods.items():
        package, simple_name = class_name.rsplit(".", 1)
        path = workspace / Path(*package.split(".")) / f"{simple_name}.java"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"package {package};", f"class {simple_name} {{"]
        for name in names:
            lines.append(f"  void {name}() {{}}")
            references[f"{class_name}.{name}()"] = (
                f"{path.relative_to(workspace).as_posix()}:{len(lines)}"
            )
        lines.append("}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return references


def execution_fixture() -> dict:
    specs = [
        (1, 0, "p.Root", "root"),
        (2, 1, "p.Service", "run"),
        (3, 2, "p.Helper", "first"),
        (4, 3, "p.Helper", "second"),
        (5, 4, "p.Helper", "tooDeep"),
        (6, 1, "p.Service", "run"),
    ]
    invocations = [{
        "invocation_id": invocation_id,
        "parent_id": parent_id,
        "class": class_name,
        "method": method,
        "descriptor": "()V",
        "thread_id": 1,
        "thread_name": "main",
        "enter_seq": invocation_id * 2,
        "enter_ns": invocation_id * 100,
        "origin_test_line": invocation_id,
        "exit_seq": invocation_id * 2 + 1,
        "exit_ns": invocation_id * 100 + 10,
        "exit_type": "RETURN",
        "duration_ns": 10,
        "exception_class": "",
        "message": "",
    } for invocation_id, parent_id, class_name, method in specs]
    by_id = {item["invocation_id"]: item for item in invocations}
    calls = []
    for invocation in invocations[1:]:
        parent = by_id[invocation["parent_id"]]
        calls.append({
            "caller": f"{parent['class']}.{parent['method']}",
            "callee": f"{invocation['class']}.{invocation['method']}",
            "caller_class": parent["class"],
            "callee_class": invocation["class"],
            "caller_method": parent["method"],
            "callee_method": invocation["method"],
            "caller_descriptor": "()V",
            "callee_descriptor": "()V",
            "parent_invocation_id": parent["invocation_id"],
            "invocation_id": invocation["invocation_id"],
            "parent_chain": [],
            "thread_id": 1,
            "enter_seq": invocation["enter_seq"],
            "exit_seq": invocation["exit_seq"],
            "exit_type": "RETURN",
            "origin_test_line": invocation["origin_test_line"],
        })
    return {
        "schema": "fullchain-execution",
        "schema_version": 3,
        "test": {"class": "p.ServiceTest", "method": "fails"},
        "original_call_count": len(calls),
        "filtered_call_count": len(calls),
        "call_count": len(calls),
        "test_start": None,
        "test_end": None,
        "test_failures": [],
        "invocations": invocations,
        "calls": calls,
    }


def semantic_sibling_execution_fixture() -> dict:
    specs = [
        (1, None, "p.RootTest", "fails", 1, 40),
        (2, 1, "p.Service", "focus", 2, 9),
        (3, 2, "p.Helper", "levelOne", 3, 8),
        (4, 3, "p.Helper", "levelTwo", 4, 7),
        (5, 4, "p.Helper", "leaf", 5, 6),
        (6, 1, "p.WorkerA", "siblingA", 10, 11),
        (7, 1, "p.WorkerB", "siblingB", 12, 13),
        (8, 1, "p.WorkerC", "siblingC", 14, 15),
        (9, 1, "p.WorkerD", "siblingD", 16, 17),
    ]
    invocations = [{
        "invocation_id": invocation_id,
        "parent_id": parent_id,
        "class": class_name,
        "method": method,
        "descriptor": "()V",
        "thread_id": 1,
        "thread_name": "main",
        "enter_seq": enter_seq,
        "enter_ns": enter_seq * 100,
        "origin_test_line": enter_seq,
        "exit_seq": exit_seq,
        "exit_ns": exit_seq * 100,
        "exit_type": "RETURN",
        "duration_ns": (exit_seq - enter_seq) * 100,
        "exception_class": "",
        "message": "",
    } for (
        invocation_id, parent_id, class_name, method, enter_seq, exit_seq
    ) in specs]
    by_id = {item["invocation_id"]: item for item in invocations}
    calls = []
    for invocation in invocations[1:]:
        parent = by_id[invocation["parent_id"]]
        calls.append({
            "caller": f"{parent['class']}.{parent['method']}",
            "callee": f"{invocation['class']}.{invocation['method']}",
            "caller_class": parent["class"],
            "callee_class": invocation["class"],
            "caller_method": parent["method"],
            "callee_method": invocation["method"],
            "caller_descriptor": "()V",
            "callee_descriptor": "()V",
            "parent_invocation_id": parent["invocation_id"],
            "invocation_id": invocation["invocation_id"],
            "parent_chain": [],
            "thread_id": 1,
            "enter_seq": invocation["enter_seq"],
            "exit_seq": invocation["exit_seq"],
            "exit_type": "RETURN",
            "origin_test_line": invocation["origin_test_line"],
        })
    return {
        "schema": "fullchain-execution",
        "schema_version": 3,
        "test": {"class": "p.RootTest", "method": "fails"},
        "original_call_count": len(calls),
        "filtered_call_count": len(calls),
        "call_count": len(calls),
        "test_start": None,
        "test_end": None,
        "test_failures": [],
        "invocations": invocations,
        "calls": calls,
    }


def write_trace_suite(
    bug_dir: Path,
    entries: list[tuple[str, str, int, dict]],
    *,
    project: str = "P",
    bug: str = "1",
) -> tuple[list[dict[str, str]], dict[tuple[str, str, str], str], str]:
    pruned_entries = []
    for test_id, test, trigger, execution in entries:
        pruned, folding = fold_successful_assertions(execution)
        pruned_entries.append((test_id, test, trigger, pruned, folding))
    catalog, method_ids, fingerprint = build_method_catalog(
        item[3] for item in pruned_entries
    )
    tests = []
    for test_id, test, trigger, execution, folding in pruned_entries:
        trace = build_refinement_trace(
            execution,
            project=project,
            test_id=test_id,
            test=test,
            method_ids=method_ids,
            catalog_fingerprint=fingerprint,
            assertion_folding=folding,
            error_stack="",
            test_output="",
        )
        relative = f"traces/{test_id}.refinement-trace.json.zst"
        write_zstd_json(bug_dir / relative, trace)
        tests.append({
            "test_id": test_id,
            "test": test,
            "trigger": trigger,
            "trace": relative,
            "trace_fingerprint": trace["fingerprint"],
        })
    (bug_dir / "trace_suite.json").write_text(json.dumps({
        "schema": "execution-trace-suite",
        "schema_version": 2,
        "project": project,
        "bug": bug,
        "method_catalog_fingerprint": fingerprint,
        "method_catalog": catalog,
        "test_count": len(tests),
        "tests": tests,
    }), encoding="utf-8")
    return catalog, method_ids, fingerprint


def normalized_topology(execution: dict) -> RefinementTraceTopology:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_trace_suite(root, [("T1", "p.T::test", 1, execution)])
        suite = json.loads((root / "trace_suite.json").read_text())
        trace_path = root / suite["tests"][0]["trace"]
        from mllmfl.infrastructure.io import read_zstd_json
        return RefinementTraceTopology.build(read_zstd_json(trace_path))


class RefinementSchemaTests(unittest.TestCase):
    def test_locator_resolves_unique_generic_method_from_erased_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            source = workspace / "src/p/Wrapper.java"
            source.parent.mkdir(parents=True)
            source.write_text(
                "package p;\n"
                "class Wrapper<T> {\n"
                "  void write(q.Writer out, T value) {}\n"
                "}\n",
                encoding="utf-8",
            )
            path = root / "input.json"
            path.write_text(json.dumps({
                "schema": "fault-localization-input",
                "schema_version": 1,
                "project": "P",
                "bug": "1",
                "locator": {"name": "external"},
                "ranking": [{
                    "candidate_id": "L001",
                    "function": "Wrapper.write",
                    "signature": (
                        "Wrapper.write(q.Writer, java.lang.Object)"
                    ),
                    "rank": 1,
                    "reason": "upstream",
                    "source_file": "src/p/Wrapper.java",
                    "start_line": 3,
                    "end_line": 3,
                }],
            }), encoding="utf-8")

            loaded = load_localization_input(path, "P", "1", workspace)

            self.assertEqual(
                loaded["ranking"][0]["function"], "p.Wrapper.write"
            )
            self.assertEqual(
                loaded["ranking"][0]["source_file"], "src/p/Wrapper.java"
            )

    def test_system_prompt_briefly_describes_graph_tools_and_elements(self):
        paragraphs = SYSTEM_PROMPT.split("\n\n")
        self.assertEqual(len(paragraphs), 6)
        self.assertIn("find_method_invocation_id", paragraphs[2])
        self.assertIn("inspect_execution_graph", paragraphs[2])
        self.assertIn("tests excluded by the upstream locator", paragraphs[2])
        self.assertNotIn("offset", paragraphs[2])
        self.assertIn("solid arrows are method calls", paragraphs[3])
        self.assertIn("dashed arrows are returns or throws", paragraphs[3])
        self.assertIn("invocation_id", paragraphs[3])
        self.assertIn("omit N calls", paragraphs[3])
        self.assertIn("exact", paragraphs[3])

    def test_input_source_range_must_match_buggy_java_ast(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            source = workspace / "src/p/Service.java"
            source.parent.mkdir(parents=True)
            source.write_text(
                "package p;\nclass Service {\n  void run() {}\n}\n",
                encoding="utf-8",
            )
            path = root / "input.json"
            value = {
                "schema": "fault-localization-input",
                "schema_version": 1,
                "project": "P",
                "bug": "1",
                "locator": {"name": "ochiai"},
                "ranking": [{
                    **candidate(),
                    "source_file": "src/p/Service.java",
                    "start_line": 3,
                    "end_line": 3,
                }],
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            loaded = load_localization_input(path, "P", "1", workspace)
            self.assertEqual(loaded["ranking"][0]["start_line"], 3)
            value["ranking"][0]["start_line"] = 2
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match"):
                load_localization_input(path, "P", "1", workspace)

    def test_validates_input_and_refinement_audits(self):
        source = {
            "schema": "fault-localization-input",
            "schema_version": 1,
            "project": "P",
            "bug": "1",
            "locator": {"name": "ochiai"},
            "failing_tests": ["p.ServiceTest::fails"],
            "ranking": [candidate()],
        }
        self.assertIs(validate_localization_input(source), source)
        with self.assertRaisesRegex(ValueError, "failing tests"):
            validate_localization_input({
                **source,
                "failing_tests": [
                    "p.ServiceTest::fails", "p.ServiceTest::fails",
                ],
            })
        refined = {
            "schema": "fault-localization-refinement",
            "schema_version": 4,
            "project": "P",
            "bug": "1",
            "status": "OK",
            "model": "vision",
            "locator": {"name": "ochiai"},
            "top_k": 1,
            "input_fingerprint": "a" * 64,
            "configuration_fingerprint": "b" * 64,
            "suite_fingerprint": "c" * 64,
            "input_ranking": [candidate()],
            "ranking": [{
                **candidate(),
                "rank": 1,
                "original_rank": 1,
                "reason": "runtime evidence",
            }],
            "rejected_candidate_ids": [],
            "tool_rounds": 1,
            "diagram_view_count": 1,
            "terminal_command_count": 1,
            "viewed_diagrams": ["T1-M1-C1-D1"],
            "inspected_candidate_ids": ["L001"],
            "candidate_runtime_method_ids": {"L001": "M1"},
            "inspected_invocation_ids": ["T1-C1"],
            "queried_methods": [{
                "name": "run", "line": "src/p/Service.java:3",
            }],
            "finalization_attempts": [{
                "response_id": "resp-1",
                "max_tokens": 16384,
                "finish_reason": "stop",
                "content_empty": False,
                "usage": {"completion_tokens": 200},
            }],
        }
        self.assertIs(validate_refinement(refined), refined)
        refined_v5 = {
            **refined,
            "schema_version": 5,
            "test_count": 1,
            "tests": [{
                "test_id": "T1", "test": "p.ServiceTest::fails",
            }],
        }
        self.assertIs(validate_refinement(refined_v5), refined_v5)
        refined_v6 = {
            key: value for key, value in refined_v5.items()
            if key != "finalization_attempts"
        }
        refined_v6.update({
            "schema_version": 6,
            "request_count": 3,
            "usage": {"input_tokens": 20, "output_tokens": 5},
            "finalization_attempt_count": 1,
            "final_length_retry_count": 0,
            "final_finish_reason": "stop",
        })
        self.assertIs(validate_refinement(refined_v6), refined_v6)
        with self.assertRaisesRegex(ValueError, "aggregate usage audit"):
            validate_refinement({
                **refined_v6,
                "usage": {"input_tokens": "20"},
            })
        with self.assertRaisesRegex(ValueError, "inspection audit"):
            validate_refinement({
                **refined_v5,
                "viewed_diagrams": ["T2-M1-C1-D1"],
            })
        invalid = {
            **refined,
            "candidate_runtime_method_ids": {"L001": "M01"},
        }
        with self.assertRaisesRegex(ValueError, "refinement.*audit"):
            validate_refinement(invalid)
        invalid = {**refined, "inspected_invocation_ids": ["T1-C01"]}
        with self.assertRaisesRegex(ValueError, "refinement.*audit"):
            validate_refinement(invalid)
        invalid = {**refined, "viewed_diagrams": ["T1-M1-C1-D01"]}
        with self.assertRaisesRegex(ValueError, "refinement.*audit"):
            validate_refinement(invalid)
        invalid = {
            **refined,
            "finalization_attempts": [{
                **refined["finalization_attempts"][0],
                "content_empty": True,
            }],
        }
        with self.assertRaisesRegex(ValueError, "finalization audit"):
            validate_refinement(invalid)
        refined["rejected_candidate_ids"] = ["L001"]
        with self.assertRaisesRegex(ValueError, "rejected candidate"):
            validate_refinement(refined)

    def test_refinement_schema_accepts_a_new_source_resolved_candidate(self):
        input_candidate = candidate()
        value = {
            "schema": "fault-localization-refinement",
            "schema_version": 4,
            "project": "P",
            "bug": "1",
            "status": "OK",
            "model": "vision",
            "locator": {"name": "ochiai"},
            "top_k": 2,
            "input_fingerprint": "a" * 64,
            "configuration_fingerprint": "b" * 64,
            "suite_fingerprint": "c" * 64,
            "input_ranking": [input_candidate],
            "ranking": [{
                "candidate_id": "N001",
                "function": "p.Helper.fix",
                "signature": "p.Helper.fix(int)",
                "source_file": "src/p/Helper.java",
                "start_line": 9,
                "end_line": 12,
                "rank": 1,
                "original_rank": None,
                "reason": "runtime state first becomes invalid here",
            }],
            "rejected_candidate_ids": ["L001"],
            "tool_rounds": 1,
            "diagram_view_count": 0,
            "terminal_command_count": 1,
            "viewed_diagrams": [],
            "inspected_candidate_ids": [],
            "candidate_runtime_method_ids": {},
            "inspected_invocation_ids": [],
            "queried_methods": [],
            "finalization_attempts": [{
                "response_id": "resp-1",
                "max_tokens": 16384,
                "finish_reason": "stop",
                "content_empty": False,
                "usage": {},
            }],
        }
        self.assertIs(validate_refinement(value), value)

    def test_source_method_reference_uses_the_declaration_name_line(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "src/p/Service.java"
            source.parent.mkdir(parents=True)
            source.write_text(
                "package p;\nclass Service {\n  @Deprecated\n"
                "  void run(String value) {}\n  void run(int value) {}\n}\n",
                encoding="utf-8",
            )
            location, signature = resolve_source_method_reference(
                workspace, "src/p/Service.java", 4, "run"
            )
            self.assertEqual(location.start_line, 3)
            self.assertEqual(signature, "p.Service.run(String)")
            with self.assertRaisesRegex(ValueError, "declaration-name line"):
                resolve_source_method_reference(
                    workspace, "src/p/Service.java", 3, "run"
                )
            with self.assertRaisesRegex(ValueError, "name does not match"):
                resolve_source_method_reference(
                    workspace, "src/p/Service.java", 4, "other"
                )

    def test_model_output_accepts_source_anchored_existing_or_new_methods(self):
        value = [{
            "method": {
                "name": "run",
                "line": "src/p/Service.java:3",
            },
            "reason": "source and trace agree",
        }]
        self.assertEqual(
            validate_model_refinement(value, 2),
            value,
        )
        value[0]["method"]["line"] = "../Fixed.java:3"
        with self.assertRaisesRegex(ValueError, "method line"):
            validate_model_refinement(value, 2)

    def test_locator_ranking_uses_agent_source_method_format(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "src/p/Service.java"
            source.parent.mkdir(parents=True)
            source.write_text(
                "package p;\nclass Service {\n  @Deprecated\n"
                "  void run() {}\n}\n",
                encoding="utf-8",
            )
            prompt = build_prompt(
                "P",
                "1",
                {"name": "locator"},
                [{
                    **candidate(),
                    "start_line": 3,
                    "end_line": 4,
                }],
                [],
                workspace,
            )
        locator_json = prompt.split("[Locator Ranking]\n", 1)[1]
        self.assertEqual(json.loads(locator_json), [{
            "method": {
                "name": "run",
                "line": "src/p/Service.java:4",
            },
            "reason": "upstream",
        }])
        self.assertNotIn("p.Service.run()", locator_json)

    def test_adapts_autofl_prediction_without_using_grading_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            source = workspace / "src/p/Service.java"
            source.parent.mkdir(parents=True)
            source.write_text(
                "package p; class Service { void run() {} void truth() {} }\n",
                encoding="utf-8",
            )
            predictions = root / "autofl" / "predictions"
            predictions.mkdir(parents=True)
            result_path = predictions / "XFL-P_1.json"
            result_path.write_text(json.dumps({
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "The test `['p.ServiceTest.fails()', "
                            "'p.OtherTest.breaks()']` failed.\n"
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": "## Diagnosis\nThe runtime state is corrupted.",
                        "reasoning_content": "private reasoning",
                    },
                    {
                        "role": "user",
                        "content": "Provide culprit signatures only.",
                    },
                    {
                        "role": "assistant",
                        "content": (
                            '<｜｜DSML｜｜parameter name="signature" string="true">'
                            "p.Service.run()"
                            "</｜｜DSML｜｜parameter>"
                        ),
                    },
                ],
                "interaction_records": {"step_histories": []},
                "buggy_methods": {
                    "p.Service.truth()": {
                        "is_found": True,
                        "matching_answer": [],
                    },
                },
            }), encoding="utf-8")
            loaded = load_localization_input(
                root / "autofl", "P", "1", workspace
            )
            self.assertEqual(loaded["locator"]["name"], "AutoFL")
            self.assertEqual(
                [item["signature"] for item in loaded["ranking"]],
                ["p.Service.run()"],
            )
            self.assertEqual(loaded["failing_tests"], [
                "p.ServiceTest::fails", "p.OtherTest::breaks",
            ])
            self.assertEqual(
                loaded["ranking"][0]["reason"],
                "## Diagnosis\nThe runtime state is corrupted.",
            )
            self.assertNotIn("private reasoning", loaded["ranking"][0]["reason"])
            value = json.loads(result_path.read_text(encoding="utf-8"))
            value["messages"][-1]["content"] = ""
            result_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "prediction is empty"):
                load_localization_input(root / "autofl", "P", "1", workspace)

    def test_autofl_reason_falls_back_only_when_diagnosis_turn_is_absent(self):
        value = {"messages": [
            {"role": "tool", "content": "not a diagnosis"},
            {"role": "user", "content": "Provide culprit signatures only."},
            {"role": "assistant", "content": "p.Service.run()"},
        ]}
        self.assertEqual(_autofl_diagnosis(value), "AutoFL final prediction")

    def test_selects_only_locator_failing_tests_in_locator_order(self):
        suite = {"tests": [
            {"test_id": "T1", "test": "p.FirstTest::fails"},
            {"test_id": "T2", "test": "p.SecondTest::fails"},
            {"test_id": "T3", "test": "p.ThirdTest::fails"},
        ]}
        selected = selected_trace_tests({
            "failing_tests": [
                "p.ThirdTest::fails", "p.FirstTest::fails",
            ],
        }, suite)
        self.assertEqual(
            [item["test_id"] for item in selected], ["T3", "T1"]
        )
        with self.assertRaisesRegex(ValueError, "absent from the trace suite"):
            selected_trace_tests({
                "failing_tests": ["p.MissingTest::fails"],
            }, suite)


class BashToolTests(unittest.TestCase):
    def test_runs_in_project_root_and_merges_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = execute_bash(
                "printf 'out'; printf 'err' >&2",
                1000,
                workspace,
                5,
            )
            cwd_result = execute_bash("pwd", 1000, workspace, 5)
        self.assertEqual(result, {
            "ok": True,
            "exit_code": 0,
            "output": "outerr",
            "truncated": False,
        })
        self.assertEqual(cwd_result["output"].strip(), str(workspace.resolve()))

    def test_reports_original_character_count_only_when_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            result = execute_bash(
                "printf 'abcdef'", 3, Path(directory), 5
            )
        self.assertEqual(result["output"], "abc")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["original_output_chars"], 6)

    def test_tool_requires_command_and_maximum_output(self):
        parameters = BASH_TOOL["parameters"]
        self.assertEqual(BASH_TOOL["name"], "bash")
        self.assertEqual(
            parameters["required"], ["command", "max_output_chars"]
        )

    def test_accepts_shell_syntax_but_rejects_invalid_payloads(self):
        command = "rg 'class ' src | head && printf '%s\\n' \"$(pwd)\""
        self.assertEqual(validate_bash_command(command), command)
        for value, message in (
            ("", "non-empty"),
            (None, "non-empty"),
            ("echo before\x00after", "NUL"),
            ("x" * 20001, "20000"),
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaisesRegex(ValueError, message):
                    validate_bash_command(value)

        for command in (
            "git log --oneline",
            "/usr/bin/git diff HEAD^",
            "defects4j checkout -p Closure -v 48f -w fixed",
            "cat .git/HEAD",
            "cat framework/projects/Closure/patches/48.src.patch",
        ):
            with self.subTest(forbidden_command=command):
                with self.assertRaisesRegex(
                    ValueError, "history, patches, or fixed versions"
                ):
                    validate_bash_command(command)

        self.assertEqual(validate_max_output_chars(1), 1)
        self.assertEqual(validate_max_output_chars(50000), 50000)
        for value in (None, True, 0, 50001, "100"):
            with self.subTest(max_output_chars=value):
                with self.assertRaises(ValueError):
                    validate_max_output_chars(value)

    def test_rejects_paths_outside_buggy_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "Source.java"
            source.write_text("class Source {}\n", encoding="utf-8")
            allowed = execute_bash(
                f"cat {source}", 1000, workspace, 5
            )
            outside = execute_bash("cat /etc/passwd", 1000, workspace, 5)
            parent = execute_bash("find .. -type f", 1000, workspace, 5)
        self.assertTrue(allowed["ok"])
        self.assertFalse(outside["ok"])
        self.assertIn("inside the buggy-project workspace", outside["error"])
        self.assertFalse(parent["ok"])
        self.assertIn("inside the buggy-project workspace", parent["error"])

    def test_timeout_returns_merged_timeout_output(self):
        with tempfile.TemporaryDirectory() as directory:
            result = execute_bash("sleep 5", 1000, Path(directory), 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 124)
        self.assertIn("[TIMEOUT] 1s", result["output"])


class GraphToolContractTests(unittest.TestCase):
    def test_graph_tool_rejects_diagram_navigation(self):
        graphs = MethodExecutionGraphs.__new__(MethodExecutionGraphs)

        result, image = graphs.inspect({"diagram_id": "D1"})

        self.assertFalse(result["ok"])
        self.assertIn("one invocation_id", result["error"])
        self.assertIsNone(image)

    def test_graph_tools_reject_locator_excluded_failing_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            bug_dir = Path(directory)
            execution = execution_fixture()
            workspace = bug_dir / "workspace"
            references = write_fixture_sources(workspace, execution)
            write_trace_suite(bug_dir, [
                ("T1", "p.ServiceTest::fails1", 1, execution),
                ("T2", "p.ServiceTest::fails2", 2, execution),
            ])
            # Excluded tests are not part of this Agent task and their large
            # indexes must not be loaded merely to initialize the selected one.
            (bug_dir / "traces/T2.refinement-trace.json.zst").write_bytes(
                b"not zstd"
            )
            graphs = MethodExecutionGraphs(
                bug_dir,
                {"uml": {}},
                30,
                workspace=workspace,
                allowed_test_ids=["T1"],
            )
            included = graphs.find_invocation_ids({
                "test_id": "T1",
                "name": "run",
                "line": references["p.Service.run()"],
            })
            excluded = graphs.find_invocation_ids({
                "test_id": "T2",
                "name": "run",
                "line": references["p.Service.run()"],
            })
        self.assertTrue(included["ok"])
        self.assertFalse(excluded["ok"])
        self.assertIn("available failing tests: T1", excluded["error"])

    def test_puml_is_created_only_after_inspecting_a_selected_occurrence(self):
        with tempfile.TemporaryDirectory() as directory:
            bug_dir = Path(directory)
            execution = execution_fixture()
            workspace = bug_dir / "workspace"
            references = write_fixture_sources(workspace, execution)
            write_trace_suite(bug_dir, [
                ("T1", "p.ServiceTest::fails", 1, execution),
            ])
            graphs = MethodExecutionGraphs(
                bug_dir,
                {"uml": {}},
                30,
                max_upstream_calls=8,
                max_downstream_calls=16,
                max_internal_calls=16,
                workspace=workspace,
            )
            lookup = graphs.find_invocation_ids({
                "test_id": "T1",
                "name": "run",
                "line": references["p.Service.run()"],
            })
            self.assertTrue(lookup["ok"])
            self.assertEqual(list(bug_dir.rglob("*.puml")), [])
            fake_image = bug_dir / "fake.png"
            with patch.object(graphs, "_image_path", return_value=fake_image):
                result, image = graphs.inspect({
                    "invocation_id": lookup["invocations"][0]["invocation_id"]
                })
                direct_result, _ = graphs.inspect({"invocation_id": "T1-C3"})
            self.assertTrue(result["ok"])
            self.assertTrue(direct_result["ok"])
            self.assertEqual(direct_result["invocation_id"], "T1-C3")
            self.assertEqual(set(result), {
                "ok", "invocation_id", "visible_call_count",
                "omitted_call_count", "has_omitted_calls",
                "omitted_region_count", "focus_method",
            })
            self.assertEqual(result["focus_method"], {
                "name": "run",
                "line": references["p.Service.run()"],
            })
            self.assertEqual(direct_result["focus_method"], {
                "name": "first",
                "line": references["p.Helper.first()"],
            })
            self.assertEqual(len(graphs._records), 1)
            self.assertEqual(image, fake_image)
            self.assertTrue(list(bug_dir.rglob("*.puml")))

    def test_semantic_omissions_use_regions_and_render_one_png(self):
        with tempfile.TemporaryDirectory() as directory:
            bug_dir = Path(directory)
            execution = semantic_sibling_execution_fixture()
            workspace = bug_dir / "workspace"
            references = write_fixture_sources(workspace, execution)
            write_trace_suite(bug_dir, [
                ("T1", "p.RootTest::fails", 1, execution),
            ])
            graphs = MethodExecutionGraphs(
                bug_dir,
                {"uml": {
                    "plantuml_jar": str(
                        Path(__file__).resolve().parents[1]
                        / "lib/plantuml.jar"
                    ),
                    "plantuml_limit_size": 32768,
                }},
                30,
                max_upstream_calls=8,
                max_downstream_calls=2,
                max_internal_calls=4,
                workspace=workspace,
            )
            lookup = graphs.find_invocation_ids({
                "test_id": "T1",
                "name": "focus",
                "line": references["p.Service.focus()"],
            })
            invocation_id = lookup["invocations"][0]["invocation_id"]
            result, entry_png = graphs.inspect({"invocation_id": invocation_id})
            self.assertTrue(result["ok"])
            self.assertTrue(entry_png.is_file())
            self.assertEqual(entry_png.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

            entry_id = graphs.viewed_diagram_id(invocation_id)
            entry_node = graphs._nodes[entry_id]
            self.assertEqual(result, {
                "ok": True,
                "invocation_id": invocation_id,
                "visible_call_count": int(entry_node["visible_call_count"]),
                "omitted_call_count": int(entry_node["omitted_call_count"]),
                "has_omitted_calls": True,
                "omitted_region_count": 1,
                "focus_method": {
                    "name": "focus",
                    "line": references["p.Service.focus()"],
                },
            })
            self.assertEqual(entry_node["links"], [])
            self.assertTrue(entry_node["has_omitted_calls"])
            self.assertEqual(entry_node["omitted_region_count"], 1)
            self.assertEqual(
                entry_node["visible_represented_call_count"]
                + entry_node["omitted_call_count"],
                entry_node["represented_call_count"],
            )
            self.assertEqual(
                sum(item["represented_call_count"] for item in entry_node["folds"]),
                entry_node["omitted_call_count"],
            )
            self.assertEqual(
                len(list((bug_dir / "inspection_graphs").rglob("*.png"))), 1
            )

            puml = (
                bug_dir / "inspection_graphs"
                / entry_id.rsplit("-D", 1)[0]
                / f"{entry_id}.puml"
            ).read_text(encoding="utf-8")
            self.assertIn("title Invocation ID: T1-C2", puml)
            self.assertIn("T1-C2 focus()", puml)
            self.assertNotIn("M2 C2 focus()", puml)
            self.assertRegex(puml, r"\.\.\. omit \d+ calls \.\.\.")
            self.assertNotIn("note right of ", puml)
            self.assertNotIn("TO ", puml)
            self.assertNotIn("FROM ", puml)
            self.assertNotIn("VIEW ", puml)

    def test_refinement_trace_contains_topology_values_and_lookup_index(self):
        execution = execution_fixture()
        with tempfile.TemporaryDirectory() as directory:
            bug_dir = Path(directory)
            catalog, _, _ = write_trace_suite(bug_dir, [
                ("T1", "p.ServiceTest::fails", 1, execution),
            ])
            from mllmfl.infrastructure.io import read_zstd_json
            value = read_zstd_json(
                bug_dir / "traces/T1.refinement-trace.json.zst"
            )
        self.assertIs(validate_refinement_trace(value), value)
        self.assertEqual(value["schema_version"], 1)
        for obsolete in (
            "execution", "default_execution", "parent_chain",
            "thread_name", "duration_ns",
        ):
            self.assertNotIn(obsolete, json.dumps(value))
        service_id = next(
            item["method_id"] for item in catalog
            if item["signature"] == "p.Service.run()"
        )
        occurrences = next(
            item[1] for item in value["method_invocations"]
            if item[0] == service_id
        )
        self.assertEqual(occurrences, [2, 6])
        topology = RefinementTraceTopology.build(value)
        self.assertEqual(topology.caller_signature(2), "p.Root.root()")
        self.assertEqual(topology.subtree_call_count(2), 4)

    def test_focus_viewport_uses_independent_context_and_internal_budgets(self):
        execution = execution_fixture()
        plan = plan_focus_viewport(
            diagram_id="D1",
            focus_invocation_id=2,
            topology=normalized_topology(execution),
            max_upstream_calls=2,
            max_downstream_calls=3,
            max_internal_calls=3,
        )
        self.assertEqual(plan["selected_focus"]["representative_invocation_id"], 2)
        self.assertEqual(plan["focus"]["representative_invocation_id"], 1)
        self.assertEqual(
            [item["representative_invocation_id"] for item in plan["visible_items"]],
            [2, 3, 4, 5, 6],
        )
        self.assertEqual(plan["upstream_visible_call_count"], 1)
        self.assertEqual(plan["downstream_visible_call_count"], 1)
        self.assertEqual(plan["internal_visible_call_count"], 3)
        self.assertEqual(plan["structural_context_call_count"], 1)
        self.assertEqual(execution["call_count"], 5)
        self.assertEqual(len(execution["invocations"]), 6)
        self.assertEqual(plan["links"], [])
        self.assertFalse(plan["has_omitted_calls"])
        self.assertEqual(plan["omitted_region_count"], 0)

    def test_focus_viewport_ascends_when_nearby_siblings_are_exhausted(self):
        execution = execution_fixture()
        plan = plan_focus_viewport(
            diagram_id="D1",
            focus_invocation_id=5,
            topology=normalized_topology(execution),
            max_upstream_calls=2,
            max_downstream_calls=3,
            max_internal_calls=3,
        )
        self.assertEqual(plan["focus"]["representative_invocation_id"], 3)
        self.assertEqual(plan["selected_focus"]["representative_invocation_id"], 5)
        self.assertEqual(
            [item["representative_invocation_id"] for item in plan["visible_items"]],
            [4, 5],
        )
        self.assertEqual(plan["upstream_visible_call_count"], 2)
        self.assertEqual(plan["downstream_visible_call_count"], 0)
        self.assertEqual(plan["structural_context_call_count"], 2)
        self.assertTrue(plan["has_omitted_calls"])
        self.assertEqual(plan["omitted_region_count"], 1)
        self.assertEqual(plan["links"], [])

    def test_source_lookup_returns_exact_visible_runtime_invocation_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            bug_dir = Path(directory)
            execution = execution_fixture()
            workspace = bug_dir / "workspace"
            references = write_fixture_sources(workspace, execution)
            write_trace_suite(bug_dir, [
                ("T1", "p.ServiceTest::fails", 1, execution),
            ])
            graphs = MethodExecutionGraphs(
                bug_dir, {"uml": {}}, 30, workspace=workspace
            )
            result = graphs.find_invocation_ids({
                "test_id": "T1",
                "name": "run",
                "line": references["p.Service.run()"],
            })
        self.assertTrue(result["ok"])
        self.assertEqual(result, {
            "ok": True,
            "test_id": "T1",
            "test": "p.ServiceTest::fails",
            "method": {
                "name": "run",
                "line": references["p.Service.run()"],
            },
            "invocation_count": 2,
            "invocations": [
                {"invocation_id": "T1-C2", "caller": "p.Root.root()"},
                {"invocation_id": "T1-C6", "caller": "p.Root.root()"},
            ],
        })

    def test_graph_tools_separate_lookup_from_rendering(self):
        self.assertEqual(
            FIND_METHOD_INVOCATION_ID_TOOL["parameters"]["required"],
            ["test_id", "name", "line"],
        )
        self.assertEqual(
            set(FIND_METHOD_INVOCATION_ID_TOOL["parameters"]["properties"]),
            {"test_id", "name", "line", "offset", "limit"},
        )
        properties = EXECUTION_GRAPH_TOOL["parameters"]["properties"]
        self.assertEqual(set(properties), {"invocation_id"})
        self.assertEqual(
            EXECUTION_GRAPH_TOOL["parameters"]["required"], ["invocation_id"]
        )


class _FakeGraphs:
    def __init__(self, image: Path) -> None:
        self.image = image
        self.viewed = []
        self.inspected_method_ids = []
        self.inspected_invocation_ids = []
        self.queried_methods = []

    def find_invocation_ids(self, arguments):
        self.queried_methods.append({
            "name": arguments["name"], "line": arguments["line"],
        })
        return {
            "ok": True,
            "test_id": arguments["test_id"],
            "test": "p.ServiceTest::fails",
            "method": self.queried_methods[-1],
            "invocation_count": 2,
            "invocations": [
                {"invocation_id": "T1-C1", "caller": "p.Root.root()"},
                {"invocation_id": "T1-C9", "caller": "p.Caller.call()"},
            ],
        }

    def inspect(self, arguments):
        self.viewed.append("T1-M1-C1-D1")
        self.inspected_method_ids.append("M1")
        self.inspected_invocation_ids.append(arguments["invocation_id"])
        return ({
            "ok": True,
            "invocation_id": arguments["invocation_id"],
            "visible_call_count": 1,
            "has_omitted_calls": False,
            "omitted_region_count": 0,
            "focus_method": {
                "name": "run",
                "line": "src/p/Service.java:3",
            },
        }, self.image)

    def viewed_diagram_id(self, invocation_id: str) -> str:
        return self.viewed[0]

    def image_path(self, diagram_id: str) -> Path:
        self.asserted_diagram_id = diagram_id
        return self.image


class RefinementArtifactRetentionTests(unittest.TestCase):
    @patch("mllmfl.stages.refine.stage.run_agent")
    @patch("mllmfl.stages.refine.stage.runtime_method_ids")
    @patch("mllmfl.stages.refine.stage.build_prompt")
    @patch("mllmfl.stages.refine.stage.selected_trace_tests")
    @patch("mllmfl.stages.refine.stage.MethodExecutionGraphs")
    @patch("mllmfl.stages.refine.stage.load_localization_input")
    def test_lean_refinement_retains_model_interaction_evidence(
        self,
        load_input,
        graph_class,
        select_tests,
        prompt,
        runtime_ids,
        agent,
    ):
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout(Path(directory))
            layout.ensure()
            bug_dir = layout.artifacts / "P" / "bug_1"
            bug_dir.mkdir(parents=True)
            workspace = layout.workspace_dir("P", "1")
            workspace.mkdir(parents=True)
            (bug_dir / "trace_suite.json").write_text(json.dumps({
                "schema": "execution-trace-suite",
                "schema_version": 2,
                "project": "P",
                "bug": "1",
                "method_catalog_fingerprint": "f" * 64,
                "method_catalog": [{
                    "method_id": "M1",
                    "function": "p.Service.run",
                    "signature": "p.Service.run()",
                    "descriptor": "()V",
                }],
                "test_count": 1,
                "tests": [{
                    "test_id": "T1",
                    "test": "p.ServiceTest::fails",
                    "trigger": 1,
                    "trace": "traces/T1.refinement-trace.json.zst",
                    "trace_fingerprint": "a" * 64,
                }],
            }), encoding="utf-8")
            load_input.return_value = {
                "schema": "fault-localization-input",
                "schema_version": 1,
                "project": "P",
                "bug": "1",
                "locator": {"name": "test", "version": "1"},
                "failing_tests": ["p.ServiceTest::fails"],
                "ranking": [candidate()],
            }
            select_tests.return_value = [{
                "test_id": "T1", "test": "p.ServiceTest::fails",
            }]
            prompt.return_value = "prompt"
            runtime_ids.return_value = {"L001": "M1"}
            graphs = graph_class.return_value
            graphs.catalog = [{
                "method_id": "M1",
                "function": "p.Service.run",
                "signature": "p.Service.run()",
                "descriptor": "()V",
            }]
            graphs.failure_evidence.return_value = []
            graphs.available_method_ids.return_value = {"M1"}

            def run_fake_agent(*args):
                conversation_path = args[7]
                self.assertEqual(
                    conversation_path, bug_dir / "refine_conversation.jsonl"
                )
                conversation_path.write_text(
                    '{"role":"assistant","content":"final"}\n',
                    encoding="utf-8",
                )
                (bug_dir / "refine_response_usage.jsonl").write_text(
                    '{"schema":"refinement-response-usage"}\n',
                    encoding="utf-8",
                )
                image = bug_dir / "inspection_graphs" / "D1" / "D1.png"
                image.parent.mkdir(parents=True)
                image.write_bytes(b"png")
                return {
                    "ranking": [{
                        "input_candidate_id": "L001",
                        "reason": "runtime evidence",
                    }],
                    "model": "test-model",
                    "inspected_method_ids": ["M1"],
                    "tool_rounds": 1,
                    "diagram_view_count": 1,
                    "viewed_diagrams": ["T1-M1-C1-D1"],
                    "inspected_invocation_ids": ["T1-C1"],
                    "queried_methods": [{
                        "name": "run", "line": "src/p/Service.java:3",
                    }],
                    "terminal_command_count": 0,
                    "request_count": 1,
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "finalization_attempt_count": 1,
                    "final_length_retry_count": 0,
                    "final_finish_reason": "stop",
                }

            agent.side_effect = run_fake_agent
            result = _refine_bug(
                layout, "P", "1", Path("locator.json"), {"mllm": {}},
                30, 1, False, True, 6, 6, 10, False,
            )

            self.assertEqual(result["status"], "OK")
            self.assertTrue((bug_dir / "refine_conversation.jsonl").is_file())
            self.assertTrue((bug_dir / "refine_response_usage.jsonl").is_file())
            self.assertTrue(
                (bug_dir / "inspection_graphs" / "D1" / "D1.png").is_file()
            )


class RefinementAgentTests(unittest.TestCase):
    @patch("mllmfl.stages.refine.agent._post_response")
    def test_empty_length_final_retries_same_request_with_larger_limit(self, post):
        inspect_call = {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "inspect_execution_graph",
                "arguments": '{"invocation_id":"T1-C1"}',
            },
        }
        final = json.dumps([{
            "method": {"name": "run", "line": "src/p/Service.java:3"},
            "reason": "runtime and source evidence agree",
        }])
        post.side_effect = [
            (
                {"role": "assistant", "content": None,
                 "tool_calls": [inspect_call]},
                "vision", "resp-tool", [{
                    "role": "assistant", "content": None,
                    "tool_calls": [inspect_call],
                }], {"completion_tokens": 20}, "tool_calls",
            ),
            (
                {"role": "assistant", "content": "",
                 "reasoning_content": "unfinished"},
                "vision", "resp-length", [{
                    "role": "assistant", "content": "",
                    "reasoning_content": "unfinished",
                }], {
                    "completion_tokens": 16384,
                    "completion_tokens_details": {"reasoning_tokens": 16384},
                }, "length",
            ),
            (
                {"role": "assistant", "content": final,
                 "reasoning_content": "completed"},
                "vision", "resp-stop", [{
                    "role": "assistant", "content": final,
                    "reasoning_content": "completed",
                }], {"completion_tokens": 1200}, "stop",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "graph.png"
            image.write_bytes(b"png")
            source = root / "src/p/Service.java"
            source.parent.mkdir(parents=True)
            source.write_text(
                "package p;\nclass Service {\n  void run() {}\n}\n",
                encoding="utf-8",
            )
            result = run_agent(
                {"mllm": {
                    "invalid_final_json_retries": 0,
                    "max_tokens": 16384,
                    "final_length_retry_max_tokens": 32768,
                }},
                "prompt",
                [{**candidate(), "end_line": 3}],
                {"L001": "M1"},
                _FakeGraphs(image),
                root,
                30,
                root / "refine_conversation.jsonl",
                1,
            )
            usage_rows = [
                json.loads(line) for line in
                (root / "refine_response_usage.jsonl").read_text().splitlines()
            ]
        self.assertEqual(
            [call.kwargs["max_tokens"] for call in post.call_args_list],
            [16384, 16384, 32768],
        )
        self.assertEqual(
            [item["finish_reason"] for item in result["finalization_attempts"]],
            ["length", "stop"],
        )
        self.assertEqual(
            [item["max_tokens"] for item in result["finalization_attempts"]],
            [16384, 32768],
        )
        self.assertEqual(
            [item["content_empty"] for item in result["finalization_attempts"]],
            [True, False],
        )
        self.assertEqual([item["schema_version"] for item in usage_rows], [2, 2, 2])
        self.assertEqual(
            [item["requested_max_tokens"] for item in usage_rows],
            [16384, 16384, 32768],
        )

    @patch("mllmfl.stages.refine.agent._post_response")
    def test_empty_non_length_final_does_not_raise_token_limit(self, post):
        inspect_call = {
            "id": "call-1", "type": "function",
            "function": {
                "name": "inspect_execution_graph",
                "arguments": '{"invocation_id":"T1-C1"}',
            },
        }
        post.side_effect = [
            (
                {"role": "assistant", "content": None,
                 "tool_calls": [inspect_call]},
                "vision", "resp-tool", [{
                    "role": "assistant", "content": None,
                    "tool_calls": [inspect_call],
                }], {}, "tool_calls",
            ),
            (
                {"role": "assistant", "content": "",
                 "reasoning_content": "completed without visible text"},
                "vision", "resp-stop", [{
                    "role": "assistant", "content": "",
                    "reasoning_content": "completed without visible text",
                }], {"completion_tokens": 200}, "stop",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "graph.png"
            image.write_bytes(b"png")
            source = root / "src/p/Service.java"
            source.parent.mkdir(parents=True)
            source.write_text(
                "package p;\nclass Service {\n  void run() {}\n}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "response content is empty"):
                run_agent(
                    {"mllm": {
                        "invalid_final_json_retries": 0,
                        "max_tokens": 16384,
                        "final_length_retry_max_tokens": 32768,
                    }},
                    "prompt",
                    [{**candidate(), "end_line": 3}],
                    {"L001": "M1"},
                    _FakeGraphs(image),
                    root,
                    30,
                    root / "refine_conversation.jsonl",
                    1,
                )
        self.assertEqual(
            [call.kwargs["max_tokens"] for call in post.call_args_list],
            [16384, 16384],
        )

    @patch("mllmfl.stages.refine.agent._post_response")
    def test_agent_finds_occurrences_then_opens_one_and_returns_subset(self, post):
        find_call = {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "find_method_invocation_id",
                "arguments": (
                    '{"test_id":"T1","name":"run",'
                    '"line":"src/p/Service.java:3"}'
                ),
            },
        }
        inspect_call = {
            "id": "call-2",
            "type": "function",
            "function": {
                "name": "inspect_execution_graph",
                "arguments": '{"invocation_id":"T1-C9"}',
            },
        }
        ignored_call = {
            "id": "call-ignored",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": '{"command":"pwd","max_output_chars":100}',
            },
        }
        post.side_effect = [
            (
                {"role": "assistant", "content": None,
                 "tool_calls": [find_call, ignored_call]},
                "vision", "resp-1", [{
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [find_call, ignored_call],
                }], {}, "tool_calls",
            ),
            (
                {"role": "assistant", "content": "{}", "tool_calls": [inspect_call]},
                "vision", "resp-2", [{
                    "type": "function_call",
                    "call_id": "call-2",
                    "name": "inspect_execution_graph",
                    "arguments": '{"invocation_id":"T1-C9"}',
                }], {}, "tool_calls",
            ),
            (
                {"role": "assistant", "content": json.dumps([{
                        "method": {
                            "name": "run",
                            "line": "src/p/Service.java:3",
                        },
                        "reason": "the selected method returns the bad state",
                    }])},
                "vision", "resp-3", [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "final"}],
                }], {}, "stop",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "graph.png"
            image.write_bytes(b"png")
            source = root / "src/p/Service.java"
            source.parent.mkdir(parents=True)
            source.write_text(
                "package p;\nclass Service {\n  void run() {}\n}\n",
                encoding="utf-8",
            )
            graphs = _FakeGraphs(image)
            input_candidate = {**candidate(), "end_line": 3}
            result = run_agent(
                {"mllm": {"invalid_final_json_retries": 0}},
                "prompt",
                [input_candidate],
                {"L001": "M1"},
                graphs,
                root,
                30,
                root / "refine_conversation.jsonl",
                1,
            )
            conversation = (root / "refine_conversation.jsonl").read_text()
        self.assertEqual(result["ranking"][0]["input_candidate_id"], "L001")
        self.assertEqual(result["ranking"][0]["signature"], "p.Service.run()")
        self.assertEqual(result["viewed_diagrams"], ["T1-M1-C1-D1"])
        self.assertEqual(result["inspected_method_ids"], ["M1"])
        self.assertEqual(result["inspected_invocation_ids"], ["T1-C9"])
        self.assertEqual(result["queried_methods"], [{
            "name": "run", "line": "src/p/Service.java:3",
        }])
        self.assertEqual(result["finalization_attempts"], [{
            "response_id": "resp-3",
            "max_tokens": 4096,
            "finish_reason": "stop",
            "content_empty": False,
            "usage": {},
        }])
        self.assertEqual(post.call_args_list[0].kwargs["tools"][0]["name"],
                         "find_method_invocation_id")
        self.assertEqual(post.call_args_list[0].kwargs["tools"][1]["name"],
                         "inspect_execution_graph")
        self.assertEqual(post.call_args_list[0].kwargs["tools"][2]["name"],
                         "bash")
        second_input = post.call_args_list[1].args[1]
        replayed_assistant = next(
            item for item in second_input if item.get("role") == "assistant"
        )
        self.assertEqual(
            [call["id"] for call in replayed_assistant["tool_calls"]],
            ["call-1"],
        )
        third_input = post.call_args_list[2].args[1]
        self.assertEqual(third_input[-1]["content"][0]["type"], "image_url")
        self.assertNotIn("data:image", conversation)


if __name__ == "__main__":
    unittest.main()
