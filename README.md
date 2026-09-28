# DPEX: Dynamic Program Execution Navigation for Fault Localization

DPEX provides on-demand access to runtime evidence for agentic fault localization.
An agent identifies a source method, looks up its concrete invocations in a failing
test, and inspects a bounded execution context around a selected invocation.
**DPEX-agent** combines these tools with read-only source inspection to produce a
ranked list of faulty methods.

DPEX supports two tasks:

- **Standalone localization:** derive a ranking from failing-test evidence and buggy source.
- **Ranking refinement:** inspect and revise an external locator's ranking using the same tools.

The following expandable sections show the complete initial system and user
prompts for each task with the default `dynamic-text` mode and `dpex.top_k=5`.
System prompts are copied from
[`dpex/stages/refine/context.py`](dpex/stages/refine/context.py). In the user
prompts, `{...}` marks a field populated at runtime. Tool definitions are supplied
separately through the API; subsequent tool results and validation feedback are
added to the conversation during execution.

<details>
<summary><strong>Standalone localization — complete prompts</strong></summary>

**System prompt**

```text
You are a standalone fault-localization agent. Given a buggy
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

Return between 1 and 5 distinct METHOD lines in descending suspiciousness. Keep every record,
including its reason, on one physical line. The reason may contain additional `|` characters because
only the first three separators are structural. The second field is the declared method name (or the
declared class name for a constructor). The third field combines the POSIX source path relative to
the buggy project root, a colon, and the 1-based line number containing that declared name. Copy both
from the supplied buggy source.
```

**User prompt template**

```text
[Task]
Localize the defect in buggy checkout {project}-{bug}.

[Failing Tests]
Each report is a read-only file available through bash.
{test_id} | {test_class}::{test_method} | {failure_report_path}
```

Repeat the failing-test row for every supplied test. Each row contains its stable
test ID, fully qualified test name, and report path (for example,
`failing-tests/T1.txt`); the agent reads report contents through `bash`.

</details>

<details>
<summary><strong>Ranking refinement — complete prompts</strong></summary>

**System prompt**

```text
You are a fault-localization refinement agent. Given a buggy Java checkout, its
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

Return between 1 and 5 distinct METHOD lines in descending suspiciousness. Keep every record,
including its reason, on one physical line. The reason may contain additional `|` characters because
only the first three separators are structural.
The second field is the declared method name (or the declared class name
for a constructor). The third field combines the POSIX source path relative to the buggy project
root, a colon, and the 1-based line number containing that declared name. Copy both from the supplied
buggy source. Give a concise, candidate-specific reason grounded in the available evidence.
```

**User prompt template**

```text
[Task]
Analyze the supplied buggy checkout.

[Failing Tests]
## {test_id} {test_class}::{test_method}
Error stack:
{error_stack}
Test output:
{test_output}

[Locator]
Name: {locator_name}

[Locator Ranking]
METHOD|{declared_method_name}|{relative_source_path}:{declaration_line}|{locator_reason}
```

Repeat the failing-test block for every selected test and the `METHOD` record for
every input candidate, preserving the locator's ranking order. An empty test
output is represented as `(empty)`. Candidate paths and declaration-name lines
are resolved against the buggy source before the prompt is constructed.

</details>

## Trajectory Data

We provide all recorded fault-localization trajectories and results from our
actual experiment runs in [results.tar.gz](results.tar.gz), covering
`dynamic-text` (default) and the four variants: `bash-only`,
`no-assertion-folding`, `no-values`, and `raw-trace`.
It also includes the full AutoFL ranking-refinement run in
`defects4j-all-refinement-gpt-5.4-mini-dynamic-text`.
The archive contains the `artifacts/` directory for each run, including
agent conversations, model usage records, localization outputs, and supporting
artifacts generated by that variant. Recorded retries and unsuccessful attempts
are retained where present.

## Contents

- [Requirements](#requirements)
- [Installation and configuration](#installation-and-configuration)
- [Quick start: one Defects4J bug](#quick-start-one-defects4j-bug)
- [Experiment workflows](#experiment-workflows)
- [Outputs and evaluation](#outputs-and-evaluation)
- [Execution evidence and agent interface](#execution-evidence-and-agent-interface)
- [Troubleshooting](#troubleshooting)
- [Repository structure and validation](#repository-structure-and-validation)
- [License](#license)

## Requirements

| Component | Purpose |
| --- | --- |
| Python 3.12 | Reference environment for the Python pipeline; dependencies are listed in `requirements.txt`. |
| JDK 11 | Reference runtime for Defects4J; the instrumentation agent targets Java 8 bytecode. |
| Maven | Build the instrumentation agent. |
| Defects4J and its dependencies | Check out buggy programs, compile them, and execute failing tests. |
| `zstd` command-line tool | Compress raw traces during collection; the Python package alone is insufficient. |

DPEX is evaluated on **Defects4J v3.0.1, 854 active bugs across 17 projects**.
Install Defects4J and its dependencies according to the instructions included with
that version. Allow space for benchmark dependencies, temporary checkouts, and
compressed traces. Resource usage varies substantially by bug; establish a
single-bug run before selecting batch concurrency.

## Installation and configuration

Run all commands from the repository root.

### 1. Install Python dependencies and build the tracer

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mvn -q -f fullchain_tracer/pom.xml package
cp fullchain_tracer/target/fullchain-tracer.jar lib/fullchain-tracer.jar
```

### 2. Configure the benchmark environment

Set these paths for your machine:

```bash
export DEFECTS4J_HOME=/absolute/path/to/defects4j
export D4J_HOME="$DEFECTS4J_HOME"
export JAVA_HOME=/absolute/path/to/jdk-11
export PATH="$DEFECTS4J_HOME/framework/bin:$JAVA_HOME/bin:$PATH"
export TZ=America/Los_Angeles

java -version
defects4j info -p Chart
zstd --version
python -m dpex --help
```

[`d4j_env.sh`](d4j_env.sh) is a convenience script for the original development
machine. Review its absolute Java, Defects4J, and Perl paths before sourcing it.

### 3. Configure the model

```bash
cp config/dpex.example.json config/dpex.local.json
```

Edit `config/dpex.local.json` to select your provider and model. Local configuration
files matching `*.local.json` are ignored by Git. API keys must be supplied through
the environment variable named by `dpex.api_key_env`; inline keys are rejected.

| Configuration field | Meaning |
| --- | --- |
| `dpex.api_style` | `responses` or `chat_completions`, according to the provider. |
| `dpex.base_url` | Provider API base URL. |
| `dpex.api_key_env` | Name of the environment variable holding the API key. |
| `dpex.vision_model` | Model identifier, including for text-only variants. |
| `dpex.reasoning_effort` | Reasoning setting passed to the provider. |
| `dpex.agent_variant` | Evidence variant; the default is `dynamic-text`. |
| `dpex.top_k` | Maximum number of ranked methods; default: 5. |
| `dpex.max_tokens` | Output budget; set it to a value supported by the selected endpoint. |

## Quick start: one Defects4J bug

This walkthrough runs standalone localization on **Chart-1**. Use a new run root
for a new experiment. Collection executes benchmark code; localization makes
billable model requests after the dry run.

### 1. Collect failing-test evidence

```bash
python -m dpex collect \
  --root runs/artifact-chart-1 --projects Chart --bugs 1 \
  --config config/dpex.local.json
```

Collection checks out and compiles the buggy program, then records test output,
error stacks, and runtime events for each distinct trigger test. On success,
`artifacts/Chart/bug_1/` contains a validated `trace_suite.json` and indexed traces
under `traces/`. With the example configuration, traces are archived as
`T<n>.trace.sqlite3.zst`.

`trace` is an alias for `collect`; run either command once. Successful collection
removes its checkout after validation. Failed collection retains diagnostics.

### 2. Validate inputs without querying the model

```bash
python -m dpex localize \
  --root runs/artifact-chart-1 --projects Chart --bugs 1 \
  --config config/dpex.local.json --dry-run
```

Inspect `logs/localize.csv` for `DRY_RUN`. This checks the prepared inputs without
producing a model ranking. It requires collected evidence and may restore a
missing buggy checkout.

### 3. Run localization

Supply the configured API-key environment variable through your shell or secret
manager, then run:

```bash
python -m dpex localize \
  --root runs/artifact-chart-1 --projects Chart --bugs 1 \
  --config config/dpex.local.json
```

Expected output: `artifacts/Chart/bug_1/localization.json`, containing a validated
ranking of up to five source methods, plus conversation and usage logs. A valid
ranking establishes successful execution; it does not establish a localization hit.

### 4. Evaluate the ranking

```bash
python -m dpex evaluate \
  --root runs/artifact-chart-1 --projects Chart --bugs 1 \
  --d4j-home "$DEFECTS4J_HOME"
```

Inspect `summaries/evaluation.json` and `logs/evaluate.csv`. For a fully evaluable
single-bug run, `evaluated_bug_count` is 1 and `skipped_bug_count` is 0. The actual
ranking and hit metrics can vary across model executions.

## Experiment workflows

### Standalone localization at benchmark scale

Use `--projects ALL --bugs ALL` to select the supported projects and active bug
IDs during collection. Subsequent stages consume available artifacts; `ALL` does
not prove that every intended bug completed successfully.

```bash
python -m dpex collect \
  --root runs/artifact-full --projects ALL --bugs ALL \
  --config config/dpex.local.json
python -m dpex localize \
  --root runs/artifact-full --projects ALL --bugs ALL \
  --config config/dpex.local.json --workers 1 --dry-run
python -m dpex localize \
  --root runs/artifact-full --projects ALL --bugs ALL \
  --config config/dpex.local.json --workers 1
python -m dpex evaluate \
  --root runs/artifact-full --projects ALL --bugs ALL \
  --d4j-home "$DEFECTS4J_HOME"
```

`localize` and `refine` default to one worker. Increase `--workers` according to
available resources and API limits. To reuse compatible evidence in a separate
experiment, pass `--trace-root runs/artifact-full` to either stage and write its
results to a different `--root`. No trace copy is required.

### Refinement of an external ranking

Use a separate root to keep standalone and refinement results distinguishable:

```bash
python -m dpex refine \
  --root runs/artifact-refine-chart-1 --trace-root runs/artifact-chart-1 \
  --projects Chart --bugs 1 \
  --locator-results /absolute/path/to/locator-results \
  --config config/dpex.local.json --dry-run
```

After a successful dry run, repeat without `--dry-run`, then evaluate the
refinement root using the same `evaluate` command. The final artifact is
`refinement.json`.

`--locator-results` accepts a canonical JSON file, a directory of
`<project>/bug_<id>/locator_result.json` files (optionally under `artifacts/`),
`<project>-<id>.json` files, or supported AutoFL experiment/prediction outputs.
See [`config/locator_input.example.json`](config/locator_input.example.json).
The AutoFL adapter consumes predictions and diagnosis text, excluding evaluator
labels. Standalone `localize` does not accept an external ranking.

### Evidence variants

Set `dpex.agent_variant` in a separate configuration for each experiment.

| Variant | Evidence available to the agent |
| --- | --- |
| `dynamic-text` | Source inspection, invocation lookup, and local execution events as text; default DPEX-agent workflow. |
| `bash-only` | Source and failing-test reports through the guarded Bash tool. |
| `no-values` | Text navigation with captured argument and normal return values hidden. |
| `raw-trace` | Contiguous event slices around the focus call, instead of structured context windows. |
| `no-assertion-folding` | Text navigation over traces retaining successful-assertion subtrees. |


## Outputs and evaluation

```text
<root>/
├── workspace/                         # Temporary buggy checkouts
├── artifacts/<project>/bug_<id>/
│   ├── trace_suite.json
│   ├── trace_suite_summary.json
│   ├── traces/T<n>.trace.sqlite3.zst
│   ├── localization.json             # Standalone workflow
│   ├── localize_conversation.jsonl
│   ├── localize_response_usage.jsonl
│   ├── refinement.json               # Refinement workflow
│   ├── refine_conversation.jsonl
│   └── refine_response_usage.jsonl
├── logs/                             # Stage CSVs and per-bug diagnostics
└── summaries/evaluation.json
```

Only files relevant to the selected workflow are created. Shared failing-test and
ground-truth caches default to `<run-parent>/.dpex-cache/`; override them with
`--failure-cache-root` and `--evaluation-cache-root`, respectively. To reuse a
cache created before the package rename, explicitly point these options to its
existing directory. Historical protocol identifiers and fingerprint constants
remain unchanged so existing artifacts retain their identity.

Evaluation maps Defects4J source patches to buggy-side Java method ranges and
computes Top-1/3/5 accuracy, MRR, and MAP. Ground truth is used for evaluation,
not candidate generation. The summary's `metrics` average **only evaluable bugs**.
Inspect `evaluated_bug_count`, `skipped_bug_count`, and per-bug statuses, including
`MISSING_RESULT`, `INVALID_RESULT`, `NO_VALID_RESULT`, and `GROUND_TRUTH_ERROR`.


## Execution evidence and agent interface

DPEX exposes three tools:

| Tool | Purpose |
| --- | --- |
| `bash` | Bounded read-only inspection of buggy Java source and failing-test reports. |
| `find_method_invocation_id` | Resolve a source method and test to exact runtime invocation IDs. |
| `inspect_execution_graph` | Inspect a local execution context around a selected invocation. |

Rankings use one record per line:

```text
METHOD|declaredName|relative/path/File.java:line|reason
```

Each source anchor must identify a Java declaration-name line and is resolved to a
canonical method range before acceptance. Results retain model settings,
fingerprints, tool audit fields, and token usage.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Defects4J or Java is unavailable | Verify environment paths and the benchmark's own installation checks. |
| Trace compression fails | Verify the `zstd` executable, free disk space, and per-trigger collection logs. |
| Dry run reports missing evidence | Collect the selected bug first, or supply a compatible `--trace-root`. |
| API authentication or request rejection | Check `api_key_env`, API style, model identifier, and endpoint-supported budgets. |
| An ablation rejects a trace | Inspect capture policy, folding counts, and degraded-store metadata; prepare compatible traces. |
| Evaluation skips a result | Read the per-bug status and error in `evaluation.json`; separate ranking validity from ground-truth mapping. |
| A rerun reuses old output | Check fingerprints and configuration; choose a new root or deliberately use `--force`. |

## Repository structure and validation

| Path | Contents |
| --- | --- |
| [`dpex/domain/`](dpex/domain/) | Trace/ranking schemas and pure domain logic. |
| [`dpex/infrastructure/`](dpex/infrastructure/) | Defects4J, storage, source parsing, and process adapters. |
| [`dpex/stages/`](dpex/stages/) | Collection, localization, refinement, evaluation, and cleanup. |
| [`fullchain_tracer/`](fullchain_tracer/) | Maven project for the instrumentation agent. |
| [`lib/`](lib/) | Java tool JARs. |
| [`config/`](config/) | Example model and locator-input configurations. |
| [`tests/`](tests/) | Automated tests and fixtures. |

To validate the implementation independently of a full model run:

```bash
python -m unittest discover -s tests -v
python -m compileall -q dpex
python -m dpex --help
```

The Chart-1 walkthrough additionally checks the collection-to-evaluation workflow.
A dry run alone does not test model transport or localization quality.

## License

The repository is distributed under the [MIT License](LICENSE). External
benchmarks, libraries, and model services retain their respective terms.
