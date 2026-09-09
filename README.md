# MLLM Fault-Localization Refinement

This repository contains one workflow: collect Defects4J failures, record normalized runtime
evidence, use a multimodal refinement agent to audit an external locator ranking, and evaluate
the refined source ranges. The executable stages are `collect`, `refine`, `evaluate`, and
the narrowly allowlisted `cleanup` maintenance command.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
source d4j_env.sh
mvn -q -f fullchain_tracer/pom.xml package
cp fullchain_tracer/target/fullchain-tracer.jar lib/fullchain-tracer.jar
```

Secrets are environment-only. Copy `config/mllm.example.json` to an ignored `*.local.json`, set
`api_key_env`, and export that variable. Inline API keys are rejected.

## Pipeline

```bash
python -m mllmfl collect --root runs/chart-1 --projects Chart --bugs 1 \
  --config config/mllm.local.json
python -m mllmfl refine --root runs/chart-1 --projects Chart --bugs 1 \
  --locator-results path/to/locator-results \
  --config config/mllm.local.json --dry-run
python -m mllmfl evaluate --root runs/chart-1 --projects Chart --bugs 1 \
  --d4j-home "$D4J_HOME"
# Preview obsolete trace files; add --apply only after reviewing cleanup.csv.
python -m mllmfl cleanup --root runs/chart-1 --projects Chart --bugs 1
```

Run refinement with `--dry-run` before making model requests. `--force` replaces an existing stage
result. `collect` checks out and compiles each bug, then executes each distinct trigger once with
Fullchain to collect output, error stack, and trace together. `trace` is an alias for this same
stage; do not run both commands. Both require `--config` and process all triggers for a bug.
After the persisted suite and trace payloads validate, that bug's checkout is deleted immediately.
Failed collections retain their checkout and diagnostics. Matching completed suites are skipped;
`--force` or a changed capture policy rebuilds the checkout and traces.
Refinement and evaluation temporarily restore missing buggy checkouts without compiling or running
tests, then remove the checkouts they created (including on failure). Source access requires
Defects4J on PATH or `D4J_HOME`/`DEFECTS4J_HOME` to be set. Refinement is serial by default and supports deterministic bug-level concurrency with
`--workers N`. Trace uses lean artifact retention by default; pass `--retain-debug-artifacts` to
trace when diagnosing it. Refinement always retains its conversation, per-request usage, and
inspected PUML/PNG evidence; its `--retain-debug-artifacts` switch additionally records recoverable
render errors. Evaluation preserves intermediate artifacts unless the explicit `--final-only`
switch is supplied.

All runtime data stays under `--root`:

```text
<root>/
├── workspace/<project>_<id>b/
├── artifacts/<project>/bug_<id>/
│   ├── trace_suite.json
│   ├── traces/
│   │   └── T<n>.refinement-trace.json.zst
│   ├── inspection_graphs/
│   ├── refine_conversation.jsonl
│   ├── refine_response_usage.jsonl
│   └── refinement.json
├── logs/<stage>/<project>/bug_<id>/
└── summaries/evaluation.json
```

For the Closure AutoFL batch, `scripts/run_closure_refinement.py --workers 10`
collects each bug in a separate process and feeds the refinement pool. Source
`d4j_env.sh` first. `--status` reads the live batch summary; `--dry-run` collects
and validates inputs without model requests. Each bug's collection log contains
`resources.json` with sampled peak memory and termination reason.

Collection defaults to a 6 GiB per-process address-space hard limit, a sampled
6 GiB process-group RSS plus swap limit, 4 GiB of host available-memory reserve,
and a 3600-second total deadline. Configure these with `--collect-memory-gib`,
`--host-reserve-gib`, and `--collect-timeout`. The group watchdog polls every
0.25 seconds and can overshoot between samples; it is not a cgroup hard quota.
The subprocess inherits the address-space limit and uses a 1 GiB Java heap.
These limits apply to collection, not the refinement worker pool.

Fullchain agent protocol v5 now produces full/execution trace schema v6. Calls
store only their direct parent ID; ancestor relationships are recovered from
invocation parent pointers. Calls, captured values and their order are preserved.
Readers still validate legacy schemas v3–v5. Temporary collection work uses
`refinement-trace.v2.work.json.zst` (work schema v2), so interrupted older work
files are not loaded accidentally. Completed normalized trace suites remain
compatible. Each trigger returns only its call count after writing its work file,
and suite construction releases each loaded execution before loading the next.

## Trace evidence

The Java 8-compatible Fullchain agent records invocation IDs, parent relationships, JVM
descriptors, thread/timing information, test-source attribution, assertion boundaries, and bounded
argument/return snapshots. Value capture and all limits are configured in the JSON `trace` section.
Long strings retain their first and last configured characters; flat arrays, whitelisted
collections, and maps retain their configured leading and trailing items. Nested containers use
their own edge-item limit at each of the two configured levels. Method arguments have a count limit,
but no aggregate character budget.

Value summaries never invoke application `toString()` and never inspect application fields.
Scalars, strings, enums, arrays, and an explicit JDK collection/map whitelist are represented within
the configured limits; other objects are type-only placeholders. Value-enabled traces remain fully
expanded instead of merging repeated calls.

The tracer emits dynamic assertion start/pass/fail events. Complete, normally returning subtrees
wholly owned by successful assertions are discarded from the lean refinement trace; throwing,
incomplete, cross-boundary, fixture, and non-assertion calls remain visible. The compact trace keeps
the folding counts, failure evidence, capture settings, method table, call topology, method lookup
index, sibling order, and exact subtree-call counts. Raw JSONL is streamed during parsing and deleted
after successful validation; a failure is retained as `raw_events.failed.jsonl.zst`.

Each failing test receives an unpadded `T<n>` ID. The normalized trace maps runtime methods to exact
test-scoped invocation IDs such as `T1-C19`, and `trace_suite.json` contains the bug-global method
catalog used internally by refinement. `--retain-debug-artifacts` additionally retains compressed
raw/full execution evidence and detailed assertion folding.

## Locator input

`--locator-results` accepts either:

- one canonical `fault-localization-input` v1 file;
- a directory containing `<project>/bug_<id>/locator_result.json`, the equivalent path below an
  `artifacts/` directory, or `<project>-<id>.json`;
- an AutoFL experiment root, `predictions/` directory, or one `XFL-<project>_<bug>.json` file.

The AutoFL adapter reads the final prediction and preceding diagnosis only. It never consumes
evaluator-derived `buggy_methods.is_found` labels. If the locator selected a subset of failing tests,
only that subset is exposed to the refinement agent. See `config/locator_input.example.json` for the
canonical schema.

Before prompting, every candidate is resolved against the buggy checkout and presented as:

```text
METHOD|getServiceName|src/main/java/p/Service.java:42|...
```

Canonical signatures, full source ranges, scores, and candidate IDs remain in private artifacts.

## Refinement agent

The default `dynamic-graph` agent starts with no image and has three tools:

- `bash` performs bounded read-only source inspection inside the buggy checkout. Git/history,
  Defects4J, fixed-version, patch, parent-traversal, and out-of-workspace paths are rejected.
- `find_method_invocation_id` resolves a source method (`test_id`, declared `name`, and
  `path/File.java:line`) to ordered exact runtime invocation IDs. Successful-assertion folds are
  hidden from lookup.
- `inspect_execution_graph` renders one self-contained sequence diagram centered on an exact
  invocation ID returned by lookup or copied from another graph.

For the static ablation, set `mllm.agent_variant` to `bash-only`. This keeps the defect, failing-test,
locator-ranking, and output contracts unchanged while making the smallest corresponding system-
prompt edits: source inspection replaces dynamic-graph analysis and the graph instructions are
removed. The only exposed tool is `bash`, guarded by a static command allowlist including
`rg`, `grep`, `sed`, `cat`, and `find`; compilation, tests, project-code execution, language
runtimes, command substitution, and writes are rejected. Version 7 results record `agent_variant`
and `prompt_version`, and validation requires every bash-only dynamic-inspection audit field to be
empty.

The focus viewport has independent upstream, downstream, and internal call budgets. Nearby siblings
are selected before moving to higher caller levels; focus internals use breadth-first order. Omitted
regions are represented by `... omit N calls ...` self-arrows. `N` comes from trace-time subtree
counts, so viewport queries do not traverse hidden subtrees. Each selected test trace is opened once,
and invocation dictionaries are expanded only for the bounded local viewport. A selected occurrence
produces exactly one image and has no navigation graph.

Diagram IDs use `T<test>-M<method>-C<invocation_id>-D1`; call labels use exact test-scoped IDs such
as `T1-C19`. Arguments appear on call arrows and normal return values on dashed return arrows. PUML
and PNG files are generated only when an occurrence is inspected. PNG rendering is content-addressed,
atomically replaced, and rejected if it reaches the configured PlantUML size limit. Refinement
retains inspection graphs, the complete replayable conversation, and one usage record per model
request in both normal and debug modes. Debug mode additionally retains recoverable render-error
records.

The locator ranking in the first user prompt and the final model response use the same one-record-
per-line protocol: `METHOD|declaredName|relative/path/File.java:line|reason`. The agent is instructed
to return only these records, but result acceptance scans physical lines and extracts lines beginning
exactly with `METHOD|`; blank lines and unrelated surrounding output are ignored. Multiple `METHOD|`
records on one physical line are rejected. The accepted result has at most `top_k` source-anchored
methods. The agent may reorder or remove locator candidates and may add runtime/source-supported
methods. Every returned source anchor is checked against the declared Java name and resolved to a
canonical source range.

## Model transport

The default configuration uses the OpenAI Responses API with `parallel_tool_calls: false`,
`store: false`, and complete local history replay. The client also supports compatible Chat
Completions providers with preserved `reasoning_content`, tool calls, images, usage, and finish
reason. API keys are read only from the configured environment variable.

If a thinking provider returns empty visible content with `finish_reason=length`, refinement retries
that finalization request once using `final_length_retry_max_tokens`. Other malformed final answers
follow the bounded METHOD-line correction path.

## Artifact contracts

| File | Schema | Producer | Consumer |
|---|---|---|---|
| `T<n>.refinement-trace.json.zst` | `refinement-trace` v1 | trace | refine |
| `trace_suite.json` | `execution-trace-suite` v2 | trace | refine |
| external locator JSON | `fault-localization-input` v1 | external locator | refine |
| `refinement.json` | `fault-localization-refinement` v7 | refine | evaluate |
| `summaries/evaluation.json` | `fault-localization-evaluation` v2 | evaluate | reporting |

Stage outputs are validated before consumption. Existing refinement results are reused only when the
input, configuration, trace-suite fingerprints, and `top_k` match; otherwise rerun with `--force`.
The cleanup command recognizes only `trace.json`, `execution_sliced.json`, and
`execution_compressed.json`; it is a dry-run unless `--apply` is present. `evaluate --final-only`
runs only after evaluation output is validated and removes known trace/debug directories plus the
selected buggy workspace while preserving `refinement.json`, logs, and evaluation summaries.

## Validation

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q mllmfl
.venv/bin/python -m mllmfl --help
.venv/bin/python -m mllmfl refine --root runs/smoke --projects Chart --bugs 1 \
  --locator-results config/locator_input.example.json \
  --config config/mllm.example.json --dry-run
```
