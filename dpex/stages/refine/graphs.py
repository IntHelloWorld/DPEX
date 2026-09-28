import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from dpex.domain.focus_viewport import plan_focus_viewport
from dpex.domain.schemas import validate_trace_suite
from dpex.infrastructure.io import append_jsonl, read_json
from dpex.infrastructure.trace_store import SQLiteTraceTopology
from dpex.infrastructure.method_location import (
    java_executables,
    resolve_method_location,
    resolve_source_method_reference,
    same_parameters,
    signature_parameter_types,
)
from dpex.infrastructure.plantuml import ensure_rendered, runtime_settings
from dpex.infrastructure.sequence_diagram import make_continuous_execution_text
from .context import (
    AGENT_VARIANT_RAW_TRACE,
    agent_variant_policy,
    refinement_agent_variant,
)
from .focus_graph import focus_graph_diagram_nodes


FIND_METHOD_INVOCATION_ID_TOOL = {
    "type": "function",
    "name": "find_method_invocation_id",
    "description": (
        "Find where a source-anchored Java method was invoked in one selected "
        "failing-test trace. Use this tool when runtime behavior for a particular "
        "candidate or newly discovered source method could help the diagnosis and "
        "you need an exact invocation_id before inspecting its execution context. "
        "It returns ordered invocation IDs and caller signatures only; it does not "
        "render a graph, and an invocation match does not imply that the method is "
        "the expected patch location."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "test_id": {
                "type": "string",
                "description": "Failing-test ID from the prompt, for example T1.",
            },
            "name": {
                "type": "string",
                "description": (
                    "Declared Java method name, copied from a locator candidate or source."
                ),
            },
            "line": {
                "type": "string",
                "description": (
                    "POSIX source path and declaration-name line, for example "
                    "src/main/java/p/Service.java:42."
                ),
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Zero-based invocation offset; default 0.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum invocations to return; default 50.",
            },
        },
        "required": ["test_id", "name", "line"],
        "additionalProperties": False,
    },
}


EXECUTION_GRAPH_TOOL = {
    "type": "function",
    "name": "inspect_execution_graph",
    "description": (
        "Render the local execution context around one exact runtime invocation. "
        "Use this tool when calls, returns, throws, arguments, values, or surrounding "
        "control flow may help distinguish the expected patch location from a "
        "downstream failure manifestation. Pass a test-scoped invocation_id returned "
        "by find_method_invocation_id or copied from another graph. The selected "
        "invocation defines the viewport center only and is not highlighted or "
        "presumed faulty. Read the rendered sequence diagram from top to bottom. "
        "Participants are runtime classes, solid arrows are method calls, and "
        "dashed arrows are returns or throws. Each call arrow begins with its exact "
        "test-scoped invocation_id. A self-directed `... omit N calls ...` arrow "
        "represents exactly N hidden dynamic calls. The result also reports the "
        "center method's source anchor."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "invocation_id": {
                "type": "string",
                "description": (
                    "Exact test-scoped invocation ID, for example T1-C32."
                ),
            },
        },
        "required": ["invocation_id"],
        "additionalProperties": False,
    },
}


EXECUTION_TEXT_TOOL = {
    "type": "function",
    "name": "inspect_execution_graph",
    "description": (
        "Return the local execution context around one exact runtime invocation "
        "as a chronological text trace, without an image. Use this tool when "
        "calls, returns, throws, arguments, values, or surrounding control flow "
        "may help distinguish the expected patch location from a downstream "
        "failure manifestation. Pass a test-scoped invocation_id returned by "
        "find_method_invocation_id or copied directly from a prior trace. "
        "execution_trace uses the mllmfl-runtime-events-v2 "
        "contract and contains numbered CALL, "
        "RETURN, THROW, OMIT, LOOP_START, and LOOP_END records. A call reference "
        "such as T1-C2 is the exact test-scoped invocation_id accepted by this "
        "tool; no ID conversion is needed. TEST denotes a test boundary, "
        "not a navigable invocation. "
        "RETURN and THROW records point back to it with return_of or throw_of. "
        "OMIT records preserve the same hidden-call accounting as the image "
        "viewport. The selected invocation defines the viewport center only and "
        "is not presumed faulty."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "invocation_id": {
                "type": "string",
                "description": (
                    "Exact test-scoped invocation ID, for example T1-C32."
                ),
            },
        },
        "required": ["invocation_id"],
        "additionalProperties": False,
    },
}


RAW_TRACE_TOOL = {
    **EXECUTION_TEXT_TOOL,
    "description": (
        "Return a deterministic contiguous CALL/RETURN/THROW event slice centered "
        "on one exact runtime invocation's CALL event. The slice is read directly "
        "from the retained event-ordered execution store after base filtering and "
        "assertion folding. TRUNCATED_START and TRUNCATED_END report omitted event "
        "counts. A CALL and its RETURN or THROW may be in different slices. Exact "
        "test-scoped IDs such as T1-C32 can be passed back to this tool to change "
        "focus. Degraded trace stores are unsupported."
    ),
}


@dataclass(frozen=True)
class _TraceRecord:
    test_id: str
    test: str
    trigger: str
    trace_path: Path
    topology: SQLiteTraceTopology


@dataclass(frozen=True)
class _Invocation:
    invocation_id: str
    method_id: str
    signature: str
    test_id: str
    test: str
    trigger: str
    raw_invocation_id: int
    topology: SQLiteTraceTopology


def _normalized_function(signature: str) -> str:
    start = signature.rfind("(")
    if start <= 0 or not signature.endswith(")"):
        raise ValueError("invalid Java method signature")
    function = signature[:start].strip().replace("$", ".")
    if not function or "." not in function:
        raise ValueError("method signature must include a class and method")
    class_name, method = function.rsplit(".", 1)
    if method == class_name.rsplit(".", 1)[-1]:
        method = "<init>"
    return f"{class_name}.{method}"


class MethodExecutionGraphs:
    def __init__(
        self,
        bug_dir: Path,
        config: Dict[str, Any],
        timeout: int,
        *,
        output_dir: Path | None = None,
        max_upstream_calls: int = 6,
        max_downstream_calls: int = 6,
        max_internal_calls: int = 10,
        workspace: Path | None = None,
        allowed_test_ids: list[str] | None = None,
        retain_debug_artifacts: bool = False,
    ) -> None:
        if max_upstream_calls < 1:
            raise ValueError("max_upstream_calls must be positive")
        if max_downstream_calls < 1:
            raise ValueError("max_downstream_calls must be positive")
        if max_internal_calls < 1:
            raise ValueError("max_internal_calls must be positive")
        self.trace_dir = bug_dir
        self.bug_dir = output_dir if output_dir is not None else bug_dir
        self.config = config
        self.agent_variant = refinement_agent_variant(config)
        self.policy = agent_variant_policy(self.agent_variant)
        self.timeout = timeout
        self.max_upstream_calls = max_upstream_calls
        self.max_downstream_calls = max_downstream_calls
        self.max_internal_calls = max_internal_calls
        self.workspace = workspace
        self.retain_debug_artifacts = retain_debug_artifacts
        uml_cfg = config.get("uml") or {}
        self.raw_events_before = int(uml_cfg.get("raw_trace_events_before", 20))
        self.raw_events_after = int(uml_cfg.get("raw_trace_events_after", 20))
        if self.raw_events_before < 0 or self.raw_events_after < 0:
            raise ValueError("raw trace event budgets must be non-negative")
        self.suite = validate_trace_suite(
            read_json(self.trace_dir / "trace_suite.json")
        )
        known_test_ids = {
            str(item["test_id"]) for item in self.suite["tests"]
        }
        if allowed_test_ids is None:
            self.allowed_test_ids = known_test_ids
        elif (
            not allowed_test_ids
            or len(allowed_test_ids) != len(set(allowed_test_ids))
            or any(item not in known_test_ids for item in allowed_test_ids)
        ):
            raise ValueError("allowed failing-test IDs must select trace suite tests")
        else:
            self.allowed_test_ids = set(allowed_test_ids)
        self.catalog = [dict(item) for item in self.suite["method_catalog"]]
        self.catalog_by_id = {
            str(item["method_id"]): item for item in self.catalog
        }
        self.method_ids = {
            (
                str(item["function"]).rsplit(".", 1)[0],
                str(item["function"]).rsplit(".", 1)[1],
                str(item.get("descriptor") or ""),
            ): str(item["method_id"])
            for item in self.catalog
        }
        self._records = self._load_trace_records()
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._node_roots: Dict[str, Path] = {}
        self._entry_cache: Dict[str, str] = {}
        self._focus_methods: Dict[str, Dict[str, str]] = {}
        self.viewed: list[str] = []
        self._viewed_set: set[str] = set()
        self.inspected_method_ids: list[str] = []
        self.inspected_invocation_ids: list[str] = []
        self.queried_methods: list[Dict[str, str]] = []
        self.inspection_windows: list[Dict[str, Any]] = []

    def _load_trace_records(self) -> Dict[str, _TraceRecord]:
        records: Dict[str, _TraceRecord] = {}
        try:
            for spec in self.suite["tests"]:
                test_id = str(spec["test_id"])
                if test_id not in self.allowed_test_ids:
                    continue
                trace_path = self.trace_dir / Path(*Path(str(spec["trace"])).parts)
                topology = SQLiteTraceTopology.open(trace_path)
                trace = topology.trace
                if (
                    trace["test_id"] != test_id
                    or trace["test"] != spec["test"]
                    or trace["method_catalog_fingerprint"]
                    != self.suite["method_catalog_fingerprint"]
                    or any(
                        method_id not in self.catalog_by_id
                        for method_id in topology.methods
                        if str(method_id).startswith("M")
                    )
                ):
                    topology.close()
                    raise ValueError(f"trace suite payload mismatch for {test_id}")
                folding = topology.trace["assertion_folding"]
                folded_count = int(folding.get("folded_call_count") or 0)
                if self.policy.assertion_folding == "disabled" and folded_count:
                    topology.close()
                    raise ValueError(
                        "no-assertion-folding requires an unfolded trace source; "
                        f"{test_id} removed {folded_count} calls"
                    )
                if (
                    self.agent_variant in {
                        "no-values", AGENT_VARIANT_RAW_TRACE,
                        "no-assertion-folding",
                    }
                    and topology.degradation.get("enabled")
                ):
                    topology.close()
                    raise ValueError(
                        f"{self.agent_variant} does not support degraded trace source "
                        f"{test_id}"
                    )
                record = _TraceRecord(
                    test_id=test_id,
                    test=str(trace["test"]),
                    trigger=str(spec["trigger"]),
                    trace_path=trace_path,
                    topology=topology,
                )
                if test_id in records:
                    topology.close()
                    raise ValueError(f"duplicate failing-test ID: {test_id}")
                records[test_id] = record
            return records
        except Exception:
            for record in records.values():
                record.topology.close()
            raise

    def close(self) -> None:
        for record in self._records.values():
            record.topology.close()

    def available_method_ids(self) -> set[str]:
        return {
            method_id
            for record in self._records.values()
            for method_id in record.topology.method_invocations
        }

    def failure_evidence(
        self, test_ids: list[str]
    ) -> list[Dict[str, str]]:
        result = []
        for test_id in test_ids:
            record = self._records.get(test_id)
            if record is None:
                raise ValueError(f"unknown selected failing test: {test_id}")
            failure = record.topology.trace["failure"]
            result.append({
                "test_id": test_id,
                "test": record.test,
                "error_stack": str(failure["error_stack"]),
                "test_output": str(failure["test_output"]),
            })
        return result

    def _matching_method_ids(self, signature: str) -> list[str]:
        normalized = signature.strip().replace("$", ".")
        if not normalized:
            raise ValueError("signature must be a non-empty string")
        exact = [
            str(item["method_id"])
            for item in self.catalog
            if str(item["signature"]).replace("$", ".") == normalized
        ]
        if exact:
            return exact
        function = _normalized_function(normalized)
        parameters = signature_parameter_types(normalized)
        result = []
        for item in self.catalog:
            catalog_function = str(item["function"]).replace("$", ".")
            if not (
                catalog_function == function
                or catalog_function.endswith("." + function)
            ):
                continue
            if same_parameters(
                signature_parameter_types(str(item["signature"])), parameters
            ):
                result.append(str(item["method_id"]))
        return result

    @staticmethod
    def _page(arguments: Dict[str, Any]) -> tuple[int, int]:
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", 50)
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            raise ValueError("offset must be a non-negative integer")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ValueError("limit must be an integer from 1 to 100")
        return offset, limit

    @staticmethod
    def _source_reference(line: str) -> tuple[str, int]:
        match = re.fullmatch(r"(.+\.java):([1-9]\d*)", line)
        if match is None:
            raise ValueError(
                "line must be a relative Java source path and declaration line"
            )
        return match.group(1), int(match.group(2))

    def find_invocation_ids(self, arguments: Any) -> Dict[str, Any]:
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "tool arguments must be an object"}
        test_id = str(arguments.get("test_id") or "").strip()
        name = str(arguments.get("name") or "").strip()
        line = str(arguments.get("line") or "").strip()
        try:
            if not {"test_id", "name", "line"}.issubset(arguments) or any(
                key not in {"test_id", "name", "line", "offset", "limit"}
                for key in arguments
            ):
                raise ValueError(
                    "pass test_id, name, line, and optional offset and limit"
                )
            if not name:
                raise ValueError("name must be a non-empty declared method name")
            record = self._records.get(test_id)
            if record is None:
                raise ValueError(
                    "unknown test_id; available failing tests: "
                    + ", ".join(sorted(self._records))
                )
            offset, limit = self._page(arguments)
            if self.workspace is None:
                raise ValueError("buggy-project workspace is unavailable")
            source_file, declaration_line = self._source_reference(line)
            _, signature = resolve_source_method_reference(
                self.workspace, source_file, declaration_line, name
            )
            method_ids = self._matching_method_ids(signature)
            if not method_ids:
                raise ValueError(
                    "this exact source method is absent from every recorded "
                    "failing-test execution; do not retry it with a different "
                    "line or guessed runtime ID. Use bash to inspect its source. "
                    "You may retain a locator candidate using source evidence, "
                    "or select another source method."
                )
            if len(method_ids) != 1:
                matches = [
                    str(self.catalog_by_id[item]["signature"])
                    for item in method_ids
                ]
                raise ValueError(
                    "source method is ambiguous in the runtime catalog; matches: "
                    + ", ".join(matches)
                )
            method_id = method_ids[0]
            values = record.topology.method_invocations.get(method_id) or ()
            if not values:
                raise ValueError(
                    f"this exact source method has no invocation in failing test "
                    f"{test_id}; do not retry it with a different line or guessed "
                    "runtime ID. Query another listed failing test, or use bash "
                    "source inspection and retain/remove the candidate from that "
                    "evidence."
                )
        except ValueError as error:
            return {"ok": False, "error": str(error)}

        queried_method = {"name": name, "line": line}
        if queried_method not in self.queried_methods:
            self.queried_methods.append(queried_method)
        page = values[offset:offset + limit]
        invocations = []
        for raw_id in page:
            invocations.append({
                "invocation_id": f"{test_id}-C{raw_id}",
                "caller": record.topology.caller_signature(raw_id),
            })
        return {
            "ok": True,
            "test_id": test_id,
            "test": record.test,
            "method": queried_method,
            "invocation_count": len(values),
            "invocations": invocations,
        }

    def _build_start(self, invocation_id: str) -> tuple[str, _Invocation]:
        match = re.fullmatch(r"(T[1-9]\d*)-C([1-9]\d*)", invocation_id)
        record = self._records.get(match.group(1)) if match is not None else None
        raw_id = int(match.group(2)) if match is not None else 0
        if record is None or not record.topology.has_call(raw_id):
            raise ValueError(
                "unknown invocation_id; use an ID returned by "
                "find_method_invocation_id or visible in an execution graph"
            )
        method_id = record.topology.method_id(raw_id)
        catalog = self.catalog_by_id.get(method_id)
        if catalog is None:
            raise ValueError("runtime invocation method is absent from suite catalog")
        selected = _Invocation(
            invocation_id=invocation_id,
            method_id=method_id,
            signature=str(catalog["signature"]),
            test_id=record.test_id,
            test=record.test,
            trigger=record.trigger,
            raw_invocation_id=raw_id,
            topology=record.topology,
        )
        existing = self._entry_cache.get(invocation_id)
        if existing is not None:
            return existing, selected

        topology = selected.topology
        focus_id = selected.raw_invocation_id
        namespace = (
            f"{selected.test_id}-{selected.method_id}-C"
            f"{focus_id}"
        )
        root = self.bug_dir / "inspection_graphs"
        directory = root / namespace
        directory.mkdir(parents=True, exist_ok=True)
        uml_cfg = self.config.get("uml") or {}
        command, jar = runtime_settings(self.config)
        limit_size = int(uml_cfg.get("plantuml_limit_size", 32768))
        rendering = {
            "plantuml_command": command,
            "plantuml_jar": str(jar) if jar else None,
            "limit_size": limit_size,
        }
        entry_id = f"{namespace}-D1"
        planned_node = plan_focus_viewport(
            diagram_id=entry_id,
            focus_invocation_id=focus_id,
            topology=topology,
            max_upstream_calls=self.max_upstream_calls,
            max_downstream_calls=self.max_downstream_calls,
            max_internal_calls=self.max_internal_calls,
        )
        nodes, rendered_entry_id, failures, _ = focus_graph_diagram_nodes(
            directory,
            planned_graph={
                "entry_diagram_id": entry_id,
                "entry_reason": "selected_method_invocation",
                "nodes": [planned_node],
            },
            global_method_ids=self.method_ids,
            invocation_id_prefix=selected.test_id,
            diagram_title=f"Invocation ID: {selected.invocation_id}",
            show_values=self.policy.value_visibility != "hidden",
        )
        if failures or rendered_entry_id != entry_id or len(nodes) != 1:
            raise RuntimeError("focus viewport generation failed")
        runtime_node = dict(nodes[0])
        runtime_node["_rendering"] = rendering
        self._nodes[entry_id] = runtime_node
        self._node_roots[entry_id] = root
        self._entry_cache[invocation_id] = entry_id
        return entry_id, selected

    def _focus_method(self, selected: _Invocation) -> Dict[str, str]:
        existing = self._focus_methods.get(selected.method_id)
        if existing is not None:
            return dict(existing)
        if self.workspace is None:
            raise ValueError("buggy-project workspace is unavailable")
        catalog = self.catalog_by_id[selected.method_id]
        location = resolve_method_location(
            self.workspace,
            str(catalog["function"]),
            descriptor=str(catalog.get("descriptor") or ""),
            signature=str(catalog["signature"]),
        )
        source_path = self.workspace / Path(*Path(location.source_file).parts)
        matches = [
            item for item in java_executables(
                source_path.read_text(encoding="utf-8", errors="replace")
            )
            if item.function == location.function
            and item.start_line == location.start_line
            and item.end_line == location.end_line
        ]
        if len(matches) != 1:
            raise ValueError(
                "cannot resolve focused method declaration line: "
                f"{selected.signature}"
            )
        executable = matches[0]
        canonical_name = executable.function.rsplit(".", 1)[-1]
        declared_name = (
            executable.function.rsplit(".", 2)[-2].rsplit("$", 1)[-1]
            if canonical_name == "<init>" else canonical_name
        )
        result = {
            "name": declared_name,
            "line": f"{location.source_file}:{executable.declaration_line}",
        }
        self._focus_methods[selected.method_id] = result
        return dict(result)

    def viewed_diagram_id(self, invocation_id: str) -> str:
        diagram_id = self._entry_cache.get(invocation_id)
        if diagram_id is None or diagram_id not in self._viewed_set:
            raise ValueError("invocation graph has not been viewed")
        return diagram_id

    def _image_path(self, diagram_id: str) -> Path:
        if diagram_id not in self._nodes:
            raise ValueError(f"unknown diagram_id: {diagram_id}")
        node = self._nodes[diagram_id]
        root = self._node_roots[diagram_id]
        image_path = root / Path(*Path(str(node["image"])).parts)
        puml_path = root / Path(*Path(str(node["puml"])).parts)
        rendering = node["_rendering"]
        command, jar = runtime_settings(self.config, rendering)
        ensure_rendered(
            puml_path,
            image_path,
            command,
            jar,
            self.timeout,
            int(rendering["limit_size"]),
        )
        return image_path

    def image_path(self, diagram_id: str) -> Path:
        if diagram_id not in self._viewed_set:
            raise ValueError(f"diagram has not been viewed: {diagram_id}")
        return self._image_path(diagram_id)

    def _inspect(
        self, arguments: Any, *, include_image: bool
    ) -> tuple[Dict[str, Any], Path | None]:
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "tool arguments must be an object"}, None
        invocation_id = str(arguments.get("invocation_id") or "").strip()
        if not invocation_id or any(key != "invocation_id" for key in arguments):
            return {
                "ok": False,
                "error": "pass exactly one invocation_id",
            }, None
        diagram_id = ""
        try:
            diagram_id, selected = self._build_start(invocation_id)
            focus_method = self._focus_method(selected)
            image_path = self._image_path(diagram_id) if include_image else None
        except (OSError, RuntimeError, ValueError) as error:
            if diagram_id in self._nodes and self.retain_debug_artifacts:
                append_jsonl(self.bug_dir / "refine_render_errors.jsonl", {
                    "schema": "on-demand-render-error",
                    "schema_version": 1,
                    "diagram_id": diagram_id,
                    "error": str(error),
                })
            return {"ok": False, "error": str(error)}, None
        node = self._nodes[diagram_id]
        if include_image and diagram_id not in self._viewed_set:
            self._viewed_set.add(diagram_id)
            self.viewed.append(diagram_id)
        result: Dict[str, Any] = {
            "ok": True,
            "invocation_id": invocation_id,
            "visible_call_count": int(node["visible_call_count"]),
            "focus_method": focus_method,
        }
        if not include_image:
            result["test_id"] = selected.test_id
            result["execution_trace"] = str(node["_execution_text"])
        if selected.method_id not in self.inspected_method_ids:
            self.inspected_method_ids.append(selected.method_id)
        if invocation_id not in self.inspected_invocation_ids:
            self.inspected_invocation_ids.append(invocation_id)
        return result, image_path

    def inspect(self, arguments: Any) -> tuple[Dict[str, Any], Path | None]:
        return self._inspect(arguments, include_image=True)

    def inspect_text(self, arguments: Any) -> Dict[str, Any]:
        if self.agent_variant == AGENT_VARIANT_RAW_TRACE:
            return self._inspect_raw_text(arguments)
        result, _ = self._inspect(arguments, include_image=False)
        if result.get("ok"):
            self.inspection_windows.append({
                "invocation_id": str(result["invocation_id"]),
                "window_mode": "structured-invocations",
                "event_count": len(str(result["execution_trace"]).splitlines()),
                "call_count": int(result["visible_call_count"]),
            })
        return result

    def _inspect_raw_text(self, arguments: Any) -> Dict[str, Any]:
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "tool arguments must be an object"}
        invocation_id = str(arguments.get("invocation_id") or "").strip()
        if not invocation_id or any(key != "invocation_id" for key in arguments):
            return {"ok": False, "error": "pass exactly one invocation_id"}
        try:
            match = re.fullmatch(r"(T[1-9]\d*)-C([1-9]\d*)", invocation_id)
            record = self._records.get(match.group(1)) if match is not None else None
            raw_id = int(match.group(2)) if match is not None else 0
            if record is None or not record.topology.has_call(raw_id):
                raise ValueError(
                    "unknown invocation_id; use an ID returned by "
                    "find_method_invocation_id or visible in an execution trace"
                )
            window = record.topology.continuous_event_window(
                raw_id, before=self.raw_events_before, after=self.raw_events_after
            )
            method_id = record.topology.method_id(raw_id)
            catalog = self.catalog_by_id.get(method_id)
            if catalog is None:
                raise ValueError("runtime invocation method is absent from suite catalog")
            selected = _Invocation(
                invocation_id, method_id, str(catalog["signature"]), record.test_id,
                record.test, record.trigger, raw_id, record.topology,
            )
            focus_method = self._focus_method(selected)
            execution_trace = make_continuous_execution_text(
                window["events"], test_id=record.test_id,
                show_values=self.policy.value_visibility != "hidden",
                omitted_before=int(window["omitted_before"]),
                omitted_after=int(window["omitted_after"]),
            )
        except (OSError, RuntimeError, ValueError) as error:
            return {"ok": False, "error": str(error)}
        if method_id not in self.inspected_method_ids:
            self.inspected_method_ids.append(method_id)
        if invocation_id not in self.inspected_invocation_ids:
            self.inspected_invocation_ids.append(invocation_id)
        audit = {
            "invocation_id": invocation_id,
            "window_mode": "continuous-events",
            "event_count": int(window["event_count"]),
            "call_count": int(window["call_count"]),
            "omitted_before": int(window["omitted_before"]),
            "omitted_after": int(window["omitted_after"]),
        }
        self.inspection_windows.append(audit)
        return {
            "ok": True,
            "invocation_id": invocation_id,
            "test_id": record.test_id,
            "visible_event_count": int(window["event_count"]),
            "visible_call_count": int(window["call_count"]),
            "truncated_start": bool(window["omitted_before"]),
            "truncated_end": bool(window["omitted_after"]),
            "focus_method": focus_method,
            "execution_trace": execution_trace,
        }

    def execution_policy(self) -> Dict[str, str]:
        return self.policy.to_dict()

    def trace_sources(self) -> list[Dict[str, Any]]:
        specs = {str(item["test_id"]): item for item in self.suite["tests"]}
        result = []
        for test_id, record in sorted(self._records.items()):
            folding = record.topology.trace["assertion_folding"]
            result.append({
                "test_id": test_id,
                "trace": str(specs[test_id]["trace"]),
                "trace_fingerprint": str(specs[test_id]["trace_fingerprint"]),
                "storage_mode": "degraded"
                if record.topology.degradation.get("enabled") else "normal",
                "assertion_folding_strategy": str(
                    folding.get("strategy") or "legacy-enabled"
                ),
                "folded_call_count": int(folding.get("folded_call_count") or 0),
            })
        return result
