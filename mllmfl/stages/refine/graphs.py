import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from mllmfl.domain.focus_viewport import plan_focus_viewport
from mllmfl.domain.schemas import validate_trace_index, validate_trace_suite
from mllmfl.domain.trace import EXECUTION_SCHEMA, validate_trace
from mllmfl.infrastructure.io import append_jsonl, read_json
from mllmfl.infrastructure.method_location import (
    java_executables,
    resolve_method_location,
    resolve_source_method_reference,
    same_parameters,
    signature_parameter_types,
)
from mllmfl.infrastructure.plantuml import ensure_rendered, runtime_settings
from mllmfl.domain.execution_compression import compress_execution
from mllmfl.stages.trace import compression_recursion_limit

from .focus_graph import focus_graph_diagram_nodes


FIND_METHOD_INVOCATION_ID_TOOL = {
    "type": "function",
    "name": "find_method_invocation_id",
    "description": (
        "Find ordered runtime invocation IDs of one source-anchored method within "
        "one failing-test "
        "trace. Each invocation includes only its exact ID and caller signature; "
        "the lookup does not generate PlantUML or render an image."
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
        "Generate and render one local graph around one exact runtime invocation "
        "identified by a test-scoped invocation ID. IDs returned by "
        "find_method_invocation_id and IDs visible on a graph are both accepted. "
        "The result includes concise visible/omitted call counts and the focused "
        "source method anchor."
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


@dataclass(frozen=True)
class _TraceRecord:
    test_id: str
    test: str
    trigger: str
    execution_path: Path
    default_execution_path: Path
    methods: Dict[str, list[Dict[str, Any]]]


@dataclass(frozen=True)
class _Invocation:
    invocation_id: str
    method_id: str
    signature: str
    test_id: str
    test: str
    trigger: str
    context: Dict[str, Any]
    execution_path: Path


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
        max_upstream_calls: int = 6,
        max_downstream_calls: int = 6,
        max_internal_calls: int = 10,
        workspace: Path | None = None,
        allowed_test_ids: list[str] | None = None,
    ) -> None:
        if max_upstream_calls < 1:
            raise ValueError("max_upstream_calls must be positive")
        if max_downstream_calls < 1:
            raise ValueError("max_downstream_calls must be positive")
        if max_internal_calls < 1:
            raise ValueError("max_internal_calls must be positive")
        self.bug_dir = bug_dir
        self.config = config
        self.timeout = timeout
        self.max_upstream_calls = max_upstream_calls
        self.max_downstream_calls = max_downstream_calls
        self.max_internal_calls = max_internal_calls
        self.workspace = workspace
        self.suite = validate_trace_suite(
            read_json(bug_dir / "trace_suite.json")
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
        self._invocation_by_id = self._index_invocations()
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._node_roots: Dict[str, Path] = {}
        self._entry_cache: Dict[str, str] = {}
        self._focus_methods: Dict[str, Dict[str, str]] = {}
        self.viewed: list[str] = []
        self._viewed_set: set[str] = set()
        self.inspected_method_ids: list[str] = []
        self.inspected_invocation_ids: list[str] = []
        self.queried_methods: list[Dict[str, str]] = []

    def _load_trace_records(self) -> Dict[str, _TraceRecord]:
        records: Dict[str, _TraceRecord] = {}
        for spec in self.suite["tests"]:
            test_id = str(spec["test_id"])
            if test_id not in self.allowed_test_ids:
                continue
            index_path = self.bug_dir / Path(
                *Path(str(spec["trace_index"])).parts
            )
            index = validate_trace_index(read_json(index_path), index_path.parent)
            if (
                index["test_id"] != test_id
                or index["test"] != spec["test"]
                or index["method_catalog_fingerprint"]
                != self.suite["method_catalog_fingerprint"]
                or any(
                    method["method_id"] not in self.catalog_by_id
                    for method in index["methods"]
                )
            ):
                raise ValueError(f"trace suite index mismatch for {test_id}")
            methods = {
                str(item["method_id"]): [
                    dict(occurrence) for occurrence in item["occurrences"]
                ]
                for item in index["methods"]
            }
            record = _TraceRecord(
                test_id=test_id,
                test=str(index["test"]),
                trigger=str(spec["trigger"]),
                execution_path=index_path.parent / str(index["execution"]),
                default_execution_path=(
                    index_path.parent / str(index["default_execution"])
                ),
                methods=methods,
            )
            if test_id in records:
                raise ValueError(f"duplicate failing-test ID: {test_id}")
            records[test_id] = record
        return records

    def available_method_ids(self) -> set[str]:
        return {
            method_id
            for record in self._records.values()
            for method_id in record.methods
        }

    def _index_invocations(self) -> Dict[str, _Invocation]:
        result: Dict[str, _Invocation] = {}
        for test_id, record in self._records.items():
            for method_id, contexts in record.methods.items():
                catalog = self.catalog_by_id.get(method_id)
                if catalog is None:
                    raise ValueError(f"trace index references unknown method: {method_id}")
                for context in contexts:
                    raw_id = int(context["invocation_id"])
                    invocation_id = f"{test_id}-C{raw_id}"
                    selected = _Invocation(
                        invocation_id=invocation_id,
                        method_id=method_id,
                        signature=str(catalog["signature"]),
                        test_id=test_id,
                        test=record.test,
                        trigger=record.trigger,
                        context=dict(context),
                        execution_path=(
                            record.execution_path
                            if context.get("successful_assertion_fold_id") is not None
                            else record.default_execution_path
                        ),
                    )
                    if invocation_id in result:
                        raise ValueError(
                            f"duplicate runtime invocation ID: {invocation_id}"
                        )
                    result[invocation_id] = selected
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
                raise ValueError("source method is absent from recorded executions")
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
            all_values = record.methods.get(method_id) or []
            folded_values = [
                item for item in all_values
                if item.get("successful_assertion_fold_id") is not None
            ]
            values = [
                item for item in all_values
                if item.get("successful_assertion_fold_id") is None
            ]
            if not values:
                if folded_values:
                    raise ValueError(
                        "method occurs only inside passed-assertion folds for "
                        f"{test_id} and is hidden from refinement"
                    )
                raise ValueError(
                    f"source method has no invocation in failing test {test_id}"
                )
        except ValueError as error:
            return {"ok": False, "error": str(error)}

        queried_method = {"name": name, "line": line}
        if queried_method not in self.queried_methods:
            self.queried_methods.append(queried_method)
        page = values[offset:offset + limit]
        invocations = []
        for context in page:
            raw_id = int(context["invocation_id"])
            invocations.append({
                "invocation_id": f"{test_id}-C{raw_id}",
                "caller": str(context["caller_signature"]),
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
        selected = self._invocation_by_id.get(invocation_id)
        if selected is None:
            raise ValueError(
                "unknown invocation_id; use an ID returned by "
                "find_method_invocation_id or visible in an execution graph"
            )
        existing = self._entry_cache.get(invocation_id)
        if existing is not None:
            return existing, selected

        execution = validate_trace(read_json(selected.execution_path), EXECUTION_SCHEMA)
        focus_id = int(selected.context["invocation_id"])
        if not any(
            int(call["invocation_id"]) == focus_id
            for call in execution["calls"]
        ):
            raise ValueError("runtime invocation is absent from its execution trace")
        with compression_recursion_limit(execution):
            compressed = compress_execution(
                execution, protected_invocation_ids=frozenset({focus_id})
            )
        namespace = (
            f"{selected.test_id}-{selected.method_id}-C"
            f"{int(selected.context['invocation_id'])}"
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
        with compression_recursion_limit(execution):
            planned_node = plan_focus_viewport(
                diagram_id=entry_id,
                focus_invocation_id=focus_id,
                execution=execution,
                compressed=compressed,
                max_upstream_calls=self.max_upstream_calls,
                max_downstream_calls=self.max_downstream_calls,
                max_internal_calls=self.max_internal_calls,
            )
        nodes, rendered_entry_id, failures, _ = focus_graph_diagram_nodes(
            execution,
            directory,
            planned_graph={
                "entry_diagram_id": entry_id,
                "entry_reason": "selected_method_invocation",
                "nodes": [planned_node],
            },
            global_method_ids=self.method_ids,
            invocation_id_prefix=selected.test_id,
            diagram_title=f"Invocation ID: {selected.invocation_id}",
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

    def inspect(self, arguments: Any) -> tuple[Dict[str, Any], Path | None]:
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
            image_path = self._image_path(diagram_id)
        except (OSError, RuntimeError, ValueError) as error:
            if diagram_id in self._nodes:
                append_jsonl(self.bug_dir / "refine_render_errors.jsonl", {
                    "schema": "on-demand-render-error",
                    "schema_version": 1,
                    "diagram_id": diagram_id,
                    "error": str(error),
                })
            return {"ok": False, "error": str(error)}, None
        node = self._nodes[diagram_id]
        if diagram_id not in self._viewed_set:
            self._viewed_set.add(diagram_id)
            self.viewed.append(diagram_id)
        result: Dict[str, Any] = {
            "ok": True,
            "invocation_id": invocation_id,
            "visible_call_count": int(node["visible_call_count"]),
            "omitted_call_count": int(node.get("omitted_call_count") or 0),
            "focus_method": focus_method,
        }
        if selected.method_id not in self.inspected_method_ids:
            self.inspected_method_ids.append(selected.method_id)
        if invocation_id not in self.inspected_invocation_ids:
            self.inspected_invocation_ids.append(invocation_id)
        return result, image_path
