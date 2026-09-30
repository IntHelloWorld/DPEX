from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Sequence

from dpex.infrastructure.method_location import (
    java_executables,
    resolve_method_location,
)
from .parsing import format_method_record


AGENT_VARIANT_DYNAMIC_GRAPH = "dynamic-graph"
AGENT_VARIANT_DYNAMIC_TEXT = "dynamic-text"
AGENT_VARIANT_BASH_ONLY = "bash-only"
AGENT_VARIANT_NO_VALUES = "no-values"
AGENT_VARIANT_RAW_TRACE = "raw-trace"
AGENT_VARIANT_NO_ASSERTION_FOLDING = "no-assertion-folding"
AGENT_VARIANTS = {
    AGENT_VARIANT_DYNAMIC_GRAPH,
    AGENT_VARIANT_DYNAMIC_TEXT,
    AGENT_VARIANT_BASH_ONLY,
    AGENT_VARIANT_NO_VALUES,
    AGENT_VARIANT_RAW_TRACE,
    AGENT_VARIANT_NO_ASSERTION_FOLDING,
}
PROMPT_VERSIONS = {
    AGENT_VARIANT_DYNAMIC_GRAPH: "refinement-method-lines-dynamic-v2",
    AGENT_VARIANT_DYNAMIC_TEXT: "refinement-method-lines-dynamic-v2",
    AGENT_VARIANT_BASH_ONLY: "refinement-method-lines-bash-only-v3",
    AGENT_VARIANT_NO_VALUES: "refinement-method-lines-no-values-v1",
    AGENT_VARIANT_RAW_TRACE: "refinement-method-lines-raw-trace-v1",
    AGENT_VARIANT_NO_ASSERTION_FOLDING:
        "refinement-method-lines-no-assertion-folding-v1",
}
LOCALIZATION_PROMPT_VERSIONS = {
    AGENT_VARIANT_DYNAMIC_GRAPH: "localization-method-lines-dynamic-v1",
    AGENT_VARIANT_DYNAMIC_TEXT: "localization-method-lines-dynamic-v1",
    AGENT_VARIANT_BASH_ONLY: "localization-method-lines-bash-only-v1",
    AGENT_VARIANT_NO_VALUES: "localization-method-lines-no-values-v1",
    AGENT_VARIANT_RAW_TRACE: "localization-method-lines-raw-trace-v1",
    AGENT_VARIANT_NO_ASSERTION_FOLDING:
        "localization-method-lines-no-assertion-folding-v1",
}


@dataclass(frozen=True)
class AgentVariantPolicy:
    value_visibility: str
    window_mode: str
    assertion_folding: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


def agent_variant_policy(agent_variant: str) -> AgentVariantPolicy:
    if agent_variant not in AGENT_VARIANTS:
        raise ValueError("invalid agent variant")
    if agent_variant == AGENT_VARIANT_BASH_ONLY:
        return AgentVariantPolicy("unavailable", "none", "unavailable")
    return AgentVariantPolicy(
        "hidden" if agent_variant == AGENT_VARIANT_NO_VALUES else "shown",
        "continuous-events"
        if agent_variant == AGENT_VARIANT_RAW_TRACE else "structured-invocations",
        "disabled"
        if agent_variant == AGENT_VARIANT_NO_ASSERTION_FOLDING else "enabled",
    )


DYNAMIC_SYSTEM_PROMPT = """You are a fault-localization refinement agent. Given a buggy Java checkout, its
observed failing tests, and an existing fault-localization ranking, identify and rank the methods
where a corrective patch is most likely needed.

Treat the input ranking as starting evidence, not a restriction. You may reorder candidates, remove
unsupported candidates, and add newly discovered methods. Rank likely defect locations rather than
methods that merely expose an incorrect value or throw an exception.

Use only evidence supplied in the current task. Do not recall, reconstruct,
or rely on remembered developer fixes, patches, commits, issue reports, release history, benchmark
answers, or source code from other versions, even when identifiers or tests look familiar. Treat
such prior knowledge as unavailable. Independently derive and verify every ranking decision against
the current buggy checkout and failure evidence.

You are expected to use the available dynamic-evidence tools to verify and improve the ranking
before finalizing. Use find_method_invocation_id to locate relevant executions of source-anchored
methods, then use inspect_execution_graph to examine their bounded runtime context. Do not rely only
on static source review and reasoning when runtime calls, returns, throws, arguments, values, or
surrounding control flow can distinguish a likely defect from a downstream failure manifestation.
Treat dynamic evidence as diagnostic evidence rather than proof that a viewed method is faulty.

Use bash for bounded read-only inspection of the supplied buggy Java source when source details are
needed. Do not compile, run tests, execute project code, modify files, inspect repository history,
patches, fixed versions, or paths outside the supplied checkout. Use only the failing tests listed
in the task; tests excluded by the upstream locator are outside this task's evidence. Call at most
one tool in each assistant response.

When finished, return only METHOD lines, without JSON, XML, Markdown, or surrounding text. Use this
exact four-field format for every line:

METHOD|getServiceName|src/main/java/p/Service.java:42|Concise evidence-based reason

Return between 1 and __TOP_K__ distinct METHOD lines in descending suspiciousness. Keep every record,
including its reason, on one physical line. The reason may contain additional `|` characters because
only the first three separators are structural.
The second field is the declared method name (or the declared class name
for a constructor). The third field combines the POSIX source path relative to the buggy project
root, a colon, and the 1-based line number containing that declared name. Copy both from the supplied
buggy source. Give a concise, candidate-specific reason grounded in the available evidence."""


BASH_ONLY_SYSTEM_PROMPT = """You are a fault-localization refinement agent. Given a buggy Java
checkout, its observed failing tests, and an existing fault-localization ranking, identify and rank
the methods where a corrective patch is most likely needed.

Treat the input ranking as starting evidence, not a restriction. You may reorder candidates, remove
unsupported candidates, and add newly discovered methods. Rank likely defect locations rather than
methods that merely expose an incorrect value or throw an exception.

Use only evidence supplied in the current task. Do not recall, reconstruct, or rely on remembered
developer fixes, patches, commits, issue reports, release history, benchmark answers, or source code
from other versions, even when identifiers or tests look familiar. Treat such prior knowledge as
unavailable. Independently derive and verify every ranking decision against the current buggy
checkout and failure evidence.

Use bash for bounded read-only inspection of the supplied buggy Java source. Inspect source with
static file-reading commands only. Do not compile, run tests, execute project code, modify files,
inspect repository history, patches, fixed versions, or paths outside the supplied checkout. Use
only the failing tests listed in the task; tests excluded by the upstream locator are outside this
task's evidence. Call at most one tool in each assistant response.

When finished, return only METHOD lines, without JSON, XML, Markdown, or surrounding text. Use this
exact four-field format for every line:

METHOD|getServiceName|src/main/java/p/Service.java:42|Concise evidence-based reason

Return between 1 and __TOP_K__ distinct METHOD lines in descending suspiciousness. Keep every record,
including its reason, on one physical line. The reason may contain additional `|` characters because
only the first three separators are structural.
The second field is the declared method name (or the declared class name
for a constructor). The third field combines the POSIX source path relative to the buggy project
root, a colon, and the 1-based line number containing that declared name. Copy both from the supplied
buggy source. Give a concise, candidate-specific reason grounded in the available evidence."""


LOCALIZATION_DYNAMIC_SYSTEM_PROMPT = """You are a standalone fault-localization agent. Given a buggy
Java checkout, its observed failing tests, and pre-collected runtime traces, identify and rank the
methods where a corrective patch is most likely needed.

Use only evidence supplied in the current task. Do not recall, reconstruct, or rely on remembered
developer fixes, patches, commits, issue reports, release history, benchmark answers, or source code
from other versions. Independently derive every ranking decision from the current buggy checkout
and failing-test evidence.

You are expected to use the available dynamic-evidence tools to verify and strengthen your
diagnosis. Use find_method_invocation_id to locate relevant executions of
source-anchored methods, then use inspect_execution_graph to examine their bounded runtime context.
Do not rely only on failing-test reports, static source review, and reasoning when runtime calls,
returns, throws, arguments, values, or surrounding control flow can distinguish a likely defect from
a downstream failure manifestation. Treat dynamic evidence as diagnostic evidence rather than proof
that a viewed method is faulty.

Use bash for bounded read-only inspection of the failing-test reports and supplied buggy Java source
when source details are needed.

Do not compile, run tests, execute project code, modify files, inspect repository history, patches,
fixed versions, or paths outside the supplied workspace. Call at most one tool in each assistant
response.

When finished, return only METHOD lines, without JSON, XML, Markdown, or surrounding text. Use this
exact four-field format for every line:

METHOD|getServiceName|src/main/java/p/Service.java:42|Concise evidence-based reason

Return between 1 and __TOP_K__ distinct METHOD lines in descending suspiciousness. Keep every record,
including its reason, on one physical line. The reason may contain additional `|` characters because
only the first three separators are structural. The second field is the declared method name (or the
declared class name for a constructor). The third field combines the POSIX source path relative to
the buggy project root, a colon, and the 1-based line number containing that declared name. Copy both
from the supplied buggy source."""


LOCALIZATION_BASH_ONLY_SYSTEM_PROMPT = """You are a standalone fault-localization agent. Given a buggy Java
checkout and its observed failing tests, identify and rank the methods where a corrective patch is
most likely needed.

Use only evidence supplied in the current task. Do not recall, reconstruct, or rely on remembered
developer fixes, patches, commits, issue reports, release history, benchmark answers, or source code
from other versions. Independently derive every ranking decision from the current buggy checkout
and failing-test evidence.

Use bash for bounded read-only inspection. The failing-test report paths listed in the task and the
buggy Java source tree are available in the tool workspace. Read whichever failure reports are useful,
then inspect relevant production and test source. Do not compile, run tests, execute project code,
modify files, inspect repository history, patches, fixed versions, or paths outside the supplied
workspace. Call at most one tool in each assistant response.

When finished, return only METHOD lines, without JSON, XML, Markdown, or surrounding text. Use this
exact four-field format for every line:

METHOD|getServiceName|src/main/java/p/Service.java:42|Concise evidence-based reason

Return between 1 and __TOP_K__ distinct METHOD lines in descending suspiciousness. Keep every record,
including its reason, on one physical line. The reason may contain additional `|` characters because
only the first three separators are structural. The second field is the declared method name (or the
declared class name for a constructor). The third field combines the POSIX source path relative to
the buggy project root, a colon, and the 1-based line number containing that declared name. Copy both
from the supplied buggy source."""


# Public compatibility name for the shared dynamic agent prompt.
SYSTEM_PROMPT = DYNAMIC_SYSTEM_PROMPT


def refinement_agent_variant(config: Dict[str, Any]) -> str:
    cfg = config.get("dpex", config)
    variant = str(
        cfg.get("agent_variant") or AGENT_VARIANT_DYNAMIC_TEXT
    ).strip().lower()
    if variant not in AGENT_VARIANTS:
        raise ValueError("agent_variant must be one of: " + ", ".join(sorted(AGENT_VARIANTS)))
    return variant


def build_system_prompt(
    top_k: int,
    agent_variant: str = AGENT_VARIANT_DYNAMIC_TEXT,
) -> str:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if agent_variant not in AGENT_VARIANTS:
        raise ValueError("invalid refinement agent variant")
    template = (
        BASH_ONLY_SYSTEM_PROMPT
        if agent_variant == AGENT_VARIANT_BASH_ONLY
        else DYNAMIC_SYSTEM_PROMPT
    )
    prompt = template.replace("__TOP_K__", str(top_k))
    policy = agent_variant_policy(agent_variant)
    if policy.value_visibility == "hidden":
        prompt += ("\n\nIn this ablation, runtime argument values and normal return values are "
                   "deliberately hidden. A hidden marker is not a runtime null or missing capture.")
    elif policy.window_mode == "continuous-events":
        prompt += ("\n\nIn this ablation, execution inspection returns a bounded contiguous event "
                   "slice centered on the selected call. Calls and their exits may lie in different slices.")
    elif policy.assertion_folding == "disabled":
        prompt += ("\n\nIn this ablation, successful-assertion calls remain in the execution space; "
                   "treat them as context rather than presumed defect locations.")
    return prompt


def build_localization_system_prompt(
    top_k: int, agent_variant: str = AGENT_VARIANT_DYNAMIC_TEXT,
) -> str:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if agent_variant not in AGENT_VARIANTS:
        raise ValueError("invalid standalone localization agent variant")
    template = (
        LOCALIZATION_BASH_ONLY_SYSTEM_PROMPT
        if agent_variant == AGENT_VARIANT_BASH_ONLY
        else LOCALIZATION_DYNAMIC_SYSTEM_PROMPT
    )
    prompt = template.replace("__TOP_K__", str(top_k))
    policy = agent_variant_policy(agent_variant)
    if policy.value_visibility == "hidden":
        prompt += ("\n\nIn this ablation, runtime argument values and normal return values are "
                   "deliberately hidden. A hidden marker is not a runtime null or missing capture.")
    elif policy.window_mode == "continuous-events":
        prompt += ("\n\nIn this ablation, execution inspection returns a bounded contiguous event "
                   "slice centered on the selected call. Calls and their exits may lie in different slices.")
    elif policy.assertion_folding == "disabled":
        prompt += ("\n\nIn this ablation, successful-assertion calls remain in the execution space; "
                   "treat them as context rather than presumed defect locations.")
    return prompt


def build_localization_prompt(
    project: str, bug: str, failures: Sequence[Dict[str, str]],
) -> str:
    lines = [
        "[Task]",
        f"Localize the defect in buggy checkout {project}-{bug}.",
        "",
        "[Failing Tests]",
        "Each report is a read-only file available through bash.",
    ]
    for item in failures:
        lines.append(
            f"{item['test_id']} | {item['test']} | {item['failure_file']}"
        )
    return "\n".join(lines)


def runtime_method_ids(
    candidates: Sequence[Dict[str, Any]],
    catalog: Sequence[Dict[str, Any]],
    workspace: Path,
) -> Dict[str, str]:
    signatures: Dict[str, list[str]] = {}
    functions: Dict[str, list[Dict[str, Any]]] = {}
    for item in catalog:
        method_id = str(item["method_id"])
        signature = str(item["signature"])
        signatures.setdefault(signature, []).append(method_id)
        functions.setdefault(str(item["function"]), []).append(item)
    result = {}
    for candidate in candidates:
        candidate_location = (
            str(candidate["source_file"]),
            int(candidate["start_line"]),
            int(candidate["end_line"]),
        )
        matches = signatures.get(str(candidate["signature"]), [])
        if len(matches) == 1:
            result[str(candidate["candidate_id"])] = matches[0]
            continue
        narrowed = functions.get(str(candidate["function"]), [])
        location_matches = []
        for item in narrowed:
            try:
                location = resolve_method_location(
                    workspace,
                    str(item["function"]),
                    descriptor=str(item.get("descriptor") or ""),
                    signature=str(item["signature"]),
                )
            except (OSError, UnicodeError, ValueError):
                continue
            if (
                location.source_file,
                location.start_line,
                location.end_line,
            ) == candidate_location:
                location_matches.append(str(item["method_id"]))
        if len(location_matches) == 1:
            result[str(candidate["candidate_id"])] = location_matches[0]
    return result


def selected_trace_tests(
    localization_input: Dict[str, Any], suite: Dict[str, Any],
    *, allow_partial: bool = False, fallback_to_all: bool = False,
) -> list[Dict[str, Any]]:
    requested = localization_input.get("failing_tests")
    if requested is None:
        return [dict(item) for item in suite["tests"]]
    by_test = {str(item["test"]): item for item in suite["tests"]}
    missing = [test for test in requested if test not in by_test]
    if missing and not allow_partial:
        raise ValueError(
            "locator failing tests are absent from the trace suite: "
            + ", ".join(missing)
        )
    selected = [dict(by_test[test]) for test in requested if test in by_test]
    if not selected:
        if fallback_to_all:
            return [dict(item) for item in suite["tests"]]
        raise ValueError("locator failing tests have no overlap with the trace suite")
    return selected


def build_prompt(
    project: str,
    bug: str,
    locator: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
    failures: Sequence[Dict[str, str]],
    workspace: Path,
) -> str:
    lines = [
        "[Task]",
        "Analyze the supplied buggy checkout.",
        "",
        "[Failing Tests]",
    ]
    for item in failures:
        lines.extend([
            f"## {item['test_id']} {item['test']}",
            "Error stack:",
            item["error_stack"],
            "Test output:",
            item["test_output"] or "(empty)",
            "",
        ])
    locator_ranking = []
    for item in candidates:
        source_file = str(item["source_file"])
        source_path = workspace / Path(*Path(source_file).parts)
        matches = [
            executable
            for executable in java_executables(
                source_path.read_text(encoding="utf-8", errors="replace")
            )
            if executable.function == str(item["function"])
            and executable.start_line == int(item["start_line"])
            and executable.end_line == int(item["end_line"])
        ]
        if len(matches) != 1:
            raise ValueError(
                "cannot build source-anchored locator method: "
                f"{item['function']}"
            )
        executable = matches[0]
        canonical_name = executable.function.rsplit(".", 1)[-1]
        declared_name = (
            executable.function.rsplit(".", 2)[-2].rsplit("$", 1)[-1]
            if canonical_name == "<init>" else canonical_name
        )
        locator_ranking.append(format_method_record(
            declared_name,
            f"{source_file}:{executable.declaration_line}",
            str(item.get("reason") or ""),
        ))
    lines.extend([
        "[Locator]",
        f"Name: {locator['name']}",
        "",
        "[Locator Ranking]",
        *locator_ranking,
    ])
    return "\n".join(lines)
