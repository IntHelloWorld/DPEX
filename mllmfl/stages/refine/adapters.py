import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Protocol, Sequence

from mllmfl.infrastructure.io import read_json


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
            section = config.get("mllm", config)
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


LOCATOR_ADAPTERS: tuple[LocatorResultAdapter, ...] = (
    CanonicalLocatorAdapter(),
    AutoFLLocatorAdapter(),
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
        value = read_json(path)
        if not isinstance(value, dict):
            continue
        for adapter in LOCATOR_ADAPTERS:
            if adapter.matches(path, value):
                matches.append((adapter, path, value))
    if len(matches) != 1:
        found = ", ".join(str(path) for _, path, _ in matches) or "none"
        raise ValueError(
            "locator result source must resolve to exactly one supported result "
            f"for {project}-{bug}; found: {found}"
        )
    adapter, path, value = matches[0]
    return adapter.adapt(path, value, project, bug)
