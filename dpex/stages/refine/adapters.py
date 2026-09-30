import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Protocol, Sequence

from dpex.infrastructure.io import read_json


@dataclass(frozen=True)
class AdaptedLocatorResult:
    path: Path
    locator: Dict[str, Any]
    ranking: list[Dict[str, Any]]
    failing_tests: tuple[str, ...] | None = None
    canonical_value: Dict[str, Any] | None = None


class LocatorResultAdapter(Protocol):
    name: str

    def candidate_paths(
        self, source: Path, project: str, bug: str
    ) -> Sequence[Path]: ...

    def matches(self, path: Path, value: Dict[str, Any]) -> bool: ...

    def adapt(
        self,
        path: Path,
        value: Dict[str, Any],
        project: str,
        bug: str,
    ) -> AdaptedLocatorResult: ...


class CanonicalLocatorAdapter:
    name = "canonical"

    def candidate_paths(
        self, source: Path, project: str, bug: str
    ) -> Sequence[Path]:
        if source.is_file():
            return (source,)
        return (
            source / project / f"bug_{bug}" / "locator_result.json",
            source / "artifacts" / project / f"bug_{bug}" / "locator_result.json",
            source / f"{project}-{bug}.json",
        )

    def matches(self, path: Path, value: Dict[str, Any]) -> bool:
        return value.get("schema") == "fault-localization-input"

    def adapt(
        self,
        path: Path,
        value: Dict[str, Any],
        project: str,
        bug: str,
    ) -> AdaptedLocatorResult:
        ranking = value.get("ranking")
        if not isinstance(ranking, list):
            raise ValueError("locator result ranking must be an array")
        return AdaptedLocatorResult(
            path=path,
            locator=dict(value["locator"]),
            ranking=list(ranking),
            failing_tests=(
                tuple(str(item).strip() for item in value["failing_tests"])
                if isinstance(value.get("failing_tests"), list)
                else None
            ),
            canonical_value=value,
        )


_AUTOFL_SIGNATURE = re.compile(
    r"(?:[A-Za-z_$][\w$]*\.)+[A-Za-z_$][\w$]*\([^()\r\n]*\)"
)


def _autofl_predictions(value: Dict[str, Any]) -> list[str]:
    messages = value.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("AutoFL result has no messages")
    final = messages[-1]
    if not isinstance(final, dict) or final.get("role") != "assistant":
        raise ValueError("AutoFL result has no final assistant prediction")
    content = final.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("AutoFL final prediction is empty")
    # This intentionally parses only the model's final answer. In particular,
    # buggy_methods.is_found is evaluator-derived data and must never be an input.
    result = []
    for match in _AUTOFL_SIGNATURE.finditer(content.replace("`", "")):
        signature = re.sub(r"\s+", " ", match.group(0)).strip()
        if signature not in result:
            result.append(signature)
    if not result:
        raise ValueError("AutoFL final prediction contains no method signature")
    return result


def _autofl_diagnosis(value: Dict[str, Any]) -> str:
    messages = value.get("messages")
    if isinstance(messages, list) and len(messages) >= 3:
        diagnosis = messages[-3]
        if isinstance(diagnosis, dict) and diagnosis.get("role") == "assistant":
            content = diagnosis.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    # Some historical AutoFL runs exhausted their tool budget without emitting
    # the diagnosis turn. Preserve their usable final prediction without
    # mistaking a tool result or private reasoning_content for the diagnosis.
    return "AutoFL final prediction"


def _autofl_failing_tests(value: Dict[str, Any]) -> tuple[str, ...]:
    messages = value.get("messages")
    if not isinstance(messages, list):
        raise ValueError("AutoFL result has no messages")
    raw_tests: Any = None
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        match = re.match(r"The test `(\[[^\r\n]*\])` failed\.", content)
        if match is None:
            continue
        try:
            raw_tests = ast.literal_eval(match.group(1))
        except (SyntaxError, ValueError) as error:
            raise ValueError("AutoFL failing-test list is invalid") from error
        break
    if (
        not isinstance(raw_tests, list)
        or not raw_tests
        or any(not isinstance(item, str) or not item.strip() for item in raw_tests)
    ):
        raise ValueError("AutoFL result has no failing-test selection")
    result = []
    for raw_test in raw_tests:
        test = raw_test.strip()
        if test.endswith("()"):
            test = test[:-2]
        if "::" not in test:
            if "." not in test:
                raise ValueError(f"invalid AutoFL failing test: {raw_test}")
            test_class, method = test.rsplit(".", 1)
            test = f"{test_class}::{method}"
        if test in result:
            raise ValueError(f"duplicate AutoFL failing test: {raw_test}")
        result.append(test)
    return tuple(result)


class AutoFLLocatorAdapter:
    name = "autofl-xfl"

    def candidate_paths(
        self, source: Path, project: str, bug: str
    ) -> Sequence[Path]:
        if source.is_file():
            return (source,)
        filename = f"XFL-{project}_{bug}.json"
        return (
            source / "predictions" / filename,
            source / filename,
        )

    def matches(self, path: Path, value: Dict[str, Any]) -> bool:
        return (
            path.name.startswith("XFL-")
            and isinstance(value.get("messages"), list)
            and isinstance(value.get("interaction_records"), dict)
            and "buggy_methods" in value
        )

    @staticmethod
    def _model(path: Path) -> str:
        roots = [path.parent, path.parent.parent]
        for root in roots:
            config_path = root / "config.json"
            if not config_path.is_file():
                continue
            config = read_json(config_path)
            section = config.get("dpex", config)
            model = section.get("model") if isinstance(section, dict) else None
            if isinstance(model, str) and model.strip():
                return model.strip()
        return ""

    def adapt(
        self,
        path: Path,
        value: Dict[str, Any],
        project: str,
        bug: str,
    ) -> AdaptedLocatorResult:
        expected = f"XFL-{project}_{bug}.json"
        if path.name != expected:
            raise ValueError(
                f"AutoFL result identity does not match selected bug: {path.name}"
            )
        signatures = _autofl_predictions(value)
        diagnosis = _autofl_diagnosis(value)
        failing_tests = _autofl_failing_tests(value)
        locator: Dict[str, Any] = {
            "name": "AutoFL",
            "source_format": self.name,
        }
        model = self._model(path)
        if model:
            locator["model"] = model
        return AdaptedLocatorResult(
            path=path,
            locator=locator,
            failing_tests=failing_tests,
            ranking=[
                {
                    "function": signature.rsplit("(", 1)[0],
                    "signature": signature,
                    "reason": diagnosis,
                }
                for signature in signatures
            ],
        )


def _normalize_test_name(raw_test: str, source: str) -> str:
    test = raw_test.strip()
    if test.endswith("()"):
        test = test[:-2]
    if "::" not in test:
        if "." not in test:
            raise ValueError(f"invalid {source} failing test: {raw_test}")
        test_class, method = test.rsplit(".", 1)
        test = f"{test_class}::{method}"
    return test


def _soapfl_failing_tests(path: Path) -> tuple[str, ...] | None:
    messages_path = path.with_name("model_messages.jsonl")
    if not messages_path.is_file():
        return None
    tests = []
    try:
        lines = messages_path.read_text(encoding="utf-8", errors="strict").splitlines()
        for line in lines:
            record = json.loads(line)
            request = record.get("request")
            payload = request.get("input") if isinstance(request, dict) else None
            if payload is None and isinstance(request, dict):
                payload = request.get("messages")
            if not isinstance(payload, list):
                continue
            for message in payload:
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                content = message.get("content")
                if not isinstance(content, str):
                    continue
                for raw_test in re.findall(
                    r"(?m)^\s*[\"']?\d+\)\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*::[A-Za-z_$][\w$]*)\s*[\"']?$",
                    content,
                ):
                    test = _normalize_test_name(raw_test, "SoapFL")
                    if test not in tests:
                        tests.append(test)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read SoapFL model messages {messages_path}: {error}") from error
    return tuple(tests) or None


class SoapFLLocatorAdapter:
    name = "soapfl-result"

    def candidate_paths(
        self, source: Path, project: str, bug: str
    ) -> Sequence[Path]:
        if source.is_file():
            return (source,)
        return tuple(sorted(source.glob(f"d4j*-{project}-{bug}/result.json")))

    def matches(self, path: Path, value: Dict[str, Any]) -> bool:
        return (
            path.name == "result.json"
            and path.parent.name.startswith("d4j")
            and isinstance(value.get("buggy_classes"), list)
            and isinstance(value.get("buggy_methods"), list)
            and isinstance(value.get("buggy_codes"), dict)
        )

    def adapt(
        self,
        path: Path,
        value: Dict[str, Any],
        project: str,
        bug: str,
    ) -> AdaptedLocatorResult:
        if not path.parent.name.endswith(f"-{project}-{bug}"):
            raise ValueError(
                f"SoapFL result identity does not match selected bug: {path.parent.name}"
            )
        ranking = []
        seen = set()
        for index, item in enumerate(value["buggy_methods"]):
            if not isinstance(item, dict):
                raise ValueError(f"invalid SoapFL candidate at index {index}")
            raw_signature = str(item.get("method_name") or "").strip()
            signature = raw_signature.replace("::", ".", 1)
            if _AUTOFL_SIGNATURE.fullmatch(signature) is None:
                raise ValueError(f"invalid SoapFL method signature: {raw_signature}")
            if signature in seen:
                continue
            seen.add(signature)
            reason = str(item.get("reason") or "").strip()
            if not reason:
                reason = str(item.get("test_failure_causes") or "").strip()
            ranking.append({
                "function": signature.rsplit("(", 1)[0],
                "signature": signature,
                "reason": reason or "SoapFL final prediction",
                "method_code": str(item.get("method_code") or ""),
            })
        if not ranking:
            raise ValueError("SoapFL result has no method candidates")
        return AdaptedLocatorResult(
            path=path,
            locator={"name": "SoapFL", "source_format": self.name},
            ranking=ranking,
            failing_tests=_soapfl_failing_tests(path),
        )


def _agentless_rows(path: Path) -> list[Dict[str, Any]]:
    try:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8", errors="strict").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read Agentless JSONL {path}: {error}") from error


def _agentless_failing_tests(value: Dict[str, Any]) -> tuple[str, ...] | None:
    trajectories = value.get("related_loc_traj")
    if not isinstance(trajectories, list):
        return None
    for trajectory in trajectories:
        if not isinstance(trajectory, dict):
            continue
        messages = trajectory.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            match = re.search(r"The test `(\[[^\r\n]*\])` failed\.", content)
            if match is None:
                continue
            try:
                raw_tests = ast.literal_eval(match.group(1))
            except (SyntaxError, ValueError) as error:
                raise ValueError("Agentless failing-test list is invalid") from error
            if not isinstance(raw_tests, list) or not raw_tests:
                raise ValueError("Agentless failing-test list is invalid")
            tests = []
            for raw_test in raw_tests:
                if not isinstance(raw_test, str) or not raw_test.strip():
                    raise ValueError("Agentless failing-test list is invalid")
                test = _normalize_test_name(raw_test, "Agentless")
                if test in tests:
                    raise ValueError(f"duplicate Agentless failing test: {raw_test}")
                tests.append(test)
            return tuple(tests)
    return None


def _split_agentless_parameters(value: str) -> list[str]:
    parameters = []
    current = []
    depth = 0
    for character in value:
        if character in "<([":
            depth += 1
        elif character in ">)]" and depth:
            depth -= 1
        if character == "," and depth == 0:
            parameters.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    parameters.append("".join(current).strip())
    result = []
    for parameter in parameters:
        if not parameter:
            continue
        parameter = re.sub(r"^(?:final\s+)+", "", parameter).strip()
        match = re.fullmatch(
            r"(.+\S)\s+([A-Za-z_$][\w$]*)(\[\])?", parameter
        )
        if match is not None:
            parameter = match.group(1) + (match.group(3) or "")
        result.append(parameter)
    return result


class AgentlessLocatorAdapter:
    name = "agentless4java-related-locations"

    def candidate_paths(
        self, source: Path, project: str, bug: str
    ) -> Sequence[Path]:
        if source.is_file():
            return (source,)
        return (source / "related_elements" / "loc_outputs.jsonl",)

    def matches(self, path: Path, value: Dict[str, Any]) -> bool:
        return (
            path.name == "loc_outputs.jsonl"
            and path.parent.name == "related_elements"
            and isinstance(value.get("instance_id"), str)
            and isinstance(value.get("found_related_locs"), dict)
        )

    def adapt(
        self,
        path: Path,
        value: Dict[str, Any],
        project: str,
        bug: str,
    ) -> AdaptedLocatorResult:
        expected = f"{project}@{bug}"
        if value.get("instance_id") != expected:
            raise ValueError(
                f"Agentless result identity does not match selected bug: {value.get('instance_id')}"
            )
        related = value["found_related_locs"]
        file_order = list(value.get("found_files") or [])
        file_order.extend(path for path in related if path not in file_order)
        ranking = []
        seen = set()
        for source_file in file_order:
            locations = related.get(source_file, [])
            if not isinstance(locations, list):
                locations = [locations]
            for location in locations:
                for line in str(location).splitlines():
                    match = re.fullmatch(
                        r"\s*method:\s*"
                        r"((?:[A-Za-z_$][\w$]*\.)*"
                        r"(?:[A-Za-z_$][\w$]*|<init>))"
                        r"\s*(?:\(([^\r\n]*)\))?\s*",
                        line,
                    )
                    if match is None:
                        if line.strip():
                            raise ValueError(f"invalid Agentless method location: {line}")
                        continue
                    function = match.group(1)
                    parameters = (
                        _split_agentless_parameters(match.group(2))
                        if match.group(2) is not None else None
                    )
                    source_class = Path(str(source_file)).stem
                    if "." not in function and function == source_class:
                        function = f"{source_class}.<init>"
                    elif (
                        "." in function
                        and function.rsplit(".", 1)[1]
                        == function.rsplit(".", 1)[0].rsplit(".", 1)[-1]
                    ):
                        class_name = function.rsplit(".", 1)[0]
                        function = f"{class_name}.<init>"
                    identity = (
                        str(source_file), function,
                        tuple(parameters) if parameters is not None else None,
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    candidate = {
                        "function": function,
                        "signature": function + "()",
                        "source_file": str(source_file),
                        "parameter_types_unknown": True,
                        "reason": "",
                    }
                    if parameters is not None:
                        candidate["parameter_types"] = parameters
                    ranking.append(candidate)
        if not ranking:
            raise ValueError("Agentless result has no method candidates")
        return AdaptedLocatorResult(
            path=path,
            locator={"name": "Agentless4Java", "source_format": self.name},
            ranking=ranking,
            failing_tests=_agentless_failing_tests(value),
        )


_PINGFL_METHOD_ID = re.compile(
    r"(?P<function>(?:[A-Za-z_$][\w$]*\.|[1-9]\d*\.)+"
    r"[A-Za-z_$][\w$]*)#(?P<start>[1-9]\d*)-(?P<end>[1-9]\d*)"
)


class PingFLLocatorAdapter:
    name = "pingfl-debug-result"

    def candidate_paths(
        self, source: Path, project: str, bug: str
    ) -> Sequence[Path]:
        if source.is_file():
            return (source,)
        return (
            source / project / f"{project}-{bug}" / "debug_result.json",
        )

    def matches(self, path: Path, value: Dict[str, Any]) -> bool:
        if path.name != "debug_result.json" or not isinstance(value, dict):
            return False
        ranking_path = path.with_name("method_rank_list.json")
        return ranking_path.is_file() and all(
            isinstance(test, str) and isinstance(processes, dict)
            for test, processes in value.items()
        )

    def adapt(
        self,
        path: Path,
        value: Dict[str, Any],
        project: str,
        bug: str,
    ) -> AdaptedLocatorResult:
        if path.parent.name != f"{project}-{bug}":
            raise ValueError(
                f"PingFL result identity does not match selected bug: {path.parent.name}"
            )
        ranking_value = read_json(path.with_name("method_rank_list.json"))
        if not isinstance(ranking_value, list):
            raise ValueError("PingFL method ranking must be an array")
        ranking = []
        seen = set()
        for index, raw_method_id in enumerate(ranking_value):
            if not isinstance(raw_method_id, str):
                raise ValueError(f"invalid PingFL method ID at index {index}")
            match = _PINGFL_METHOD_ID.fullmatch(raw_method_id.strip())
            if match is None:
                raise ValueError(f"invalid PingFL method ID: {raw_method_id}")
            method_id = raw_method_id.strip()
            if method_id in seen:
                continue
            seen.add(method_id)
            start_line = int(match.group("start"))
            end_line = int(match.group("end"))
            if end_line < start_line:
                raise ValueError(f"invalid PingFL method line range: {raw_method_id}")
            ranking.append({
                "function": match.group("function"),
                "signature": match.group("function") + "()",
                "start_line": start_line,
                "end_line": end_line,
                "source_range_identity": True,
                "reason": "PingFL final ranking",
            })
        if not ranking:
            raise ValueError("PingFL result has no method candidates")
        return AdaptedLocatorResult(
            path=path,
            locator={"name": "PingFL", "source_format": self.name},
            ranking=ranking,
            # PingFL's published ranking is aggregated across its debugging
            # sessions.  Let DPEX use every failing execution in the selected
            # trace suite; Defects4J revisions can rename or replace trigger
            # tests without changing the project/bug identity.
            failing_tests=None,
        )


LOCATOR_ADAPTERS: tuple[LocatorResultAdapter, ...] = (
    CanonicalLocatorAdapter(),
    AutoFLLocatorAdapter(),
    SoapFLLocatorAdapter(),
    AgentlessLocatorAdapter(),
    PingFLLocatorAdapter(),
)


def adapt_locator_result(
    source: Path, project: str, bug: str
) -> AdaptedLocatorResult:
    if not source.exists():
        raise FileNotFoundError(f"locator result source not found: {source}")
    candidate_paths = []
    for adapter in LOCATOR_ADAPTERS:
        for path in adapter.candidate_paths(source, project, bug):
            if path.is_file() and path not in candidate_paths:
                candidate_paths.append(path)
    matches = []
    for path in candidate_paths:
        values = _agentless_rows(path) if path.suffix == ".jsonl" else [read_json(path)]
        for value in values:
            if not isinstance(value, dict):
                continue
            for adapter in LOCATOR_ADAPTERS:
                if adapter.matches(path, value):
                    if (
                        isinstance(value.get("instance_id"), str)
                        and value["instance_id"] != f"{project}@{bug}"
                    ):
                        continue
                    matches.append((adapter, path, value))
    if len(matches) != 1:
        found = ", ".join(str(path) for _, path, _ in matches) or "none"
        raise ValueError(
            "locator result source must resolve to exactly one supported result "
            f"for {project}-{bug}; found: {found}"
        )
    adapter, path, value = matches[0]
    return adapter.adapt(path, value, project, bug)
