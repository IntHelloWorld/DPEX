from pathlib import Path
from typing import Any, Dict, Sequence

from mllmfl.infrastructure.method_location import (
    java_executables,
    resolve_method_location,
)
from .parsing import format_method_record


AGENT_VARIANT_DYNAMIC_GRAPH = "dynamic-graph"
AGENT_VARIANT_BASH_ONLY = "bash-only"
AGENT_VARIANTS = {
    AGENT_VARIANT_DYNAMIC_GRAPH,
    AGENT_VARIANT_BASH_ONLY,
}
PROMPT_VERSIONS = {
    AGENT_VARIANT_DYNAMIC_GRAPH: "refinement-method-lines-v1",
    AGENT_VARIANT_BASH_ONLY: "refinement-method-lines-bash-only-v2",
}


SYSTEM_PROMPT = """You are a fault-localization refinement agent. Your task is to analyze dynamic
program sequence diagrams to understand runtime behavior, then verify and improve an existing
fault-localization ranking. The input ranking is evidence and a starting point, not a restriction:
you may reorder candidates, remove unsupported candidates, and add newly discovered methods. Return
a new ranking from most to least suspicious.

Use bash to inspect buggy-project source. It accepts one complete Bash command string and a maximum
output character count, including pipelines and conditionals. Its working directory is the buggy
project root. Use inspection commands only and do not modify any files. Do not inspect
repository history, patches, fixed versions, or paths outside the buggy project root.

Use find_method_invocation_id to find the runtime invocations of a source-anchored method in one
failing-test trace. Use inspect_execution_graph to view the dynamic execution graph around an exact
invocation_id returned by that lookup or shown in another execution graph. Use only the failing
tests listed in the prompt; tests excluded by the upstream locator are outside this task's evidence.

An execution graph is a sequence diagram read from top to bottom. Participants are runtime classes;
solid arrows are method calls, and dashed arrows are returns or throws. Each call arrow starts with
an exact test-scoped invocation_id such as T1-C32, and the highlighted call is the selected focus.
A self-directed `... omit N calls ...` arrow marks a hidden execution region; N is the exact
number of dynamic calls represented by that region.

Do not overthink. Call at most one tool in each assistant response.

When finished, return only METHOD lines, without JSON, XML, Markdown, or surrounding text. Use this
exact four-field format for every line:

METHOD|getServiceName|src/main/java/p/Service.java:42|Concise evidence-based reason

Return between 1 and __TOP_K__ distinct METHOD lines in descending suspiciousness. Keep every record,
including its reason, on one physical line. The reason may contain additional `|` characters because
only the first three separators are structural.
The second field is the declared method name (or the declared class name
for a constructor). The third field combines the POSIX source path relative to the buggy project
root, a colon, and the 1-based line number containing that declared name. Copy both from source
inspected with bash. Give a concise, candidate-specific reason grounded in runtime
behavior and relevant source evidence."""


BASH_ONLY_SYSTEM_PROMPT = SYSTEM_PROMPT.replace(
    "analyze dynamic\nprogram sequence diagrams to understand runtime behavior",
    "analyze\nprogram source code to understand behavior",
).replace(
    """Use bash to inspect buggy-project source. It accepts one complete Bash command string and a maximum
output character count, including pipelines and conditionals. Its working directory is the buggy
project root. Use inspection commands only and do not modify any files. Do not inspect
repository history, patches, fixed versions, or paths outside the buggy project root.""",
    """Use bash only to inspect buggy-project source with static file-inspection commands such as
rg, grep, sed, cat, and find. It accepts one complete Bash command string and a maximum output
character count, including pipelines. Its working directory is the buggy project root.
Do not compile, run tests, execute project code, or invoke a language runtime. Do not modify any
files in the project or inspect repository history, patches, fixed versions, or paths outside the buggy project
root.""",
).replace(
    """Use find_method_invocation_id to find the runtime invocations of a source-anchored method in one
failing-test trace. Use inspect_execution_graph to view the dynamic execution graph around an exact
invocation_id returned by that lookup or shown in another execution graph. Use only the failing
tests listed in the prompt; tests excluded by the upstream locator are outside this task's evidence.

An execution graph is a sequence diagram read from top to bottom. Participants are runtime classes;
solid arrows are method calls, and dashed arrows are returns or throws. Each call arrow starts with
an exact test-scoped invocation_id such as T1-C32, and the highlighted call is the selected focus.
A self-directed `... omit N calls ...` arrow marks a hidden execution region; N is the exact
number of dynamic calls represented by that region.""",
    """Use only the failing tests listed in the prompt; tests excluded by the upstream locator are outside
this task's evidence.""",
).replace(
    "grounded in runtime\nbehavior and relevant source evidence.",
    "grounded in relevant source\nevidence.",
)


def refinement_agent_variant(config: Dict[str, Any]) -> str:
    cfg = config.get("mllm", config)
    variant = str(
        cfg.get("agent_variant") or AGENT_VARIANT_DYNAMIC_GRAPH
    ).strip().lower()
    if variant not in AGENT_VARIANTS:
        raise ValueError(
            "agent_variant must be dynamic-graph or bash-only"
        )
    return variant


def build_system_prompt(
    top_k: int,
    agent_variant: str = AGENT_VARIANT_DYNAMIC_GRAPH,
) -> str:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if agent_variant not in AGENT_VARIANTS:
        raise ValueError("agent_variant must be dynamic-graph or bash-only")
    template = (
        BASH_ONLY_SYSTEM_PROMPT
        if agent_variant == AGENT_VARIANT_BASH_ONLY
        else SYSTEM_PROMPT
    )
    return template.replace("__TOP_K__", str(top_k))


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
    localization_input: Dict[str, Any], suite: Dict[str, Any]
) -> list[Dict[str, Any]]:
    requested = localization_input.get("failing_tests")
    if requested is None:
        return [dict(item) for item in suite["tests"]]
    by_test = {str(item["test"]): item for item in suite["tests"]}
    missing = [test for test in requested if test not in by_test]
    if missing:
        raise ValueError(
            "locator failing tests are absent from the trace suite: "
            + ", ".join(missing)
        )
    return [dict(by_test[test]) for test in requested]


def build_prompt(
    project: str,
    bug: str,
    locator: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
    failures: Sequence[Dict[str, str]],
    workspace: Path,
) -> str:
    lines = [
        "[Defect]",
        f"Project: {project}",
        f"Bug: {bug}",
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
