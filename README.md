# MLLM Fault-Localization Refinement

This repository contains one workflow: collect Defects4J failures, record lossless runtime
executions, use a multimodal refinement agent to audit an external locator ranking, and evaluate
the refined source ranges. The executable stages are `collect`, `trace`, `refine`, and `evaluate`.

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
python -m mllmfl collect --root runs/chart-1 --projects Chart --bugs 1
python -m mllmfl trace --root runs/chart-1 --projects Chart --bugs 1
python -m mllmfl refine --root runs/chart-1 --projects Chart --bugs 1 \
  --locator-results path/to/locator-results \
  --config config/mllm.local.json --dry-run
python -m mllmfl evaluate --root runs/chart-1 --projects Chart --bugs 1 \
  --d4j-home "$D4J_HOME"
```

Run refinement with `--dry-run` before making model requests. `--force` replaces an existing stage
result. Trace accepts `--trigger`; a complete bug-level trace run produces the suite required by
refinement. Refinement is serial by default and supports deterministic bug-level concurrency with
`--workers N`.

All runtime data stays under `--root`:

```text
<root>/
├── workspace/<project>_<id>b/
├── artifacts/<project>/bug_<id>/
│   ├── triggers/trigger_<n>/
│   │   ├── raw_events.jsonl
│   │   ├── execution.json
│   │   ├── execution_assertion_pruned.json
│   │   ├── assertion_folding.json
│   │   ├── trace_index.json
│   │   └── defect_context.json
│   ├── trace_suite.json
│   ├── inspection_graphs/
│   └── refinement.json
├── logs/<stage>/<project>/bug_<id>/
└── summaries/evaluation.json
```

## Trace evidence

The Java 8-compatible Fullchain agent records invocation IDs, parent relationships, JVM
descriptors, thread/timing information, test-source attribution, assertion boundaries, and bounded
argument/return snapshots. Value capture is enabled by default and can be disabled with
`--no-capture-values`. The limits are configurable through `--value-max-chars`,
`--value-max-items`, `--value-max-depth`, and `--value-max-arguments-chars`.

Value summaries never invoke application `toString()` and never inspect application fields.
Scalars, strings, enums, arrays, and an explicit JDK collection/map whitelist are represented within
the configured limits; other objects are type-only placeholders. Value-enabled traces remain fully
expanded instead of merging repeated calls.

`execution.json` is lossless after framework filtering. The tracer emits dynamic assertion
start/pass/fail events. Complete, normally returning subtrees wholly owned by successful assertions
are hidden only in `execution_assertion_pruned.json`; throwing, incomplete, cross-boundary, fixture,
and non-assertion calls remain visible. `assertion_folding.json` records every hidden invocation so
the transformation is auditable and reversible.

Each failing test receives an unpadded `T<n>` ID. `trace_index.json` maps runtime methods to exact
test-scoped invocation IDs such as `T1-C19`, and `trace_suite.json` contains the bug-global method
catalog used internally by refinement.

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

```json
[{"method":{"name":"getServiceName","line":"src/main/java/p/Service.java:42"},"reason":"..."}]
```

Canonical signatures, full source ranges, scores, and candidate IDs remain in private artifacts.

## Refinement agent

The agent starts with no image and has three tools:

- `bash` performs bounded read-only source inspection inside the buggy checkout. Git/history,
  Defects4J, fixed-version, patch, parent-traversal, and out-of-workspace paths are rejected.
- `find_method_invocation_id` resolves a source method (`test_id`, declared `name`, and
  `path/File.java:line`) to ordered exact runtime invocation IDs. Successful-assertion folds are
  hidden from lookup.
- `inspect_execution_graph` renders one self-contained sequence diagram centered on an exact
  invocation ID returned by lookup or copied from another graph.

The focus viewport has independent upstream, downstream, and internal call budgets. Nearby siblings
are selected before moving to higher caller levels; focus internals use breadth-first order. Omitted
regions are represented by self-arrows of the form `... omit N calls ...`, where `N` includes every
dynamic call in the omitted complete subtrees. A selected occurrence produces exactly one image and
has no navigation graph.

Diagram IDs use `T<test>-M<method>-C<invocation_id>-D1`; call labels use exact test-scoped IDs such
as `T1-C19`. Arguments appear on call arrows and normal return values on dashed return arrows. PUML
and PNG files are generated only when an occurrence is inspected. PNG rendering is content-addressed,
atomically replaced, and rejected if it reaches the configured PlantUML size limit.

The final model response is a JSON array of at most `top_k` source-anchored methods. The agent may
reorder or remove locator candidates and may add runtime/source-supported methods. Every returned
source anchor is checked against the declared Java name and resolved to a canonical source range.

## Model transport

The default configuration uses the OpenAI Responses API with `parallel_tool_calls: false`,
`store: false`, and complete local history replay. The client also supports compatible Chat
Completions providers with preserved `reasoning_content`, tool calls, images, usage, and finish
reason. API keys are read only from the configured environment variable.

If a thinking provider returns empty visible content with `finish_reason=length`, refinement retries
that finalization request once using `final_length_retry_max_tokens`. Other malformed final answers
follow the bounded JSON-correction path.

## Artifact contracts

| File | Schema | Producer | Consumer |
|---|---|---|---|
| `collect.json` | `collected-trigger` v2 | collect | trace |
| `raw_events.jsonl` | Fullchain Agent protocol v4 | trace | trace parser |
| `execution.json` | `fullchain-execution` v4 | trace | graph inspection |
| `execution_assertion_pruned.json` | `fullchain-execution` v4 | trace | default lookup evidence |
| `assertion_folding.json` | `assertion-trace-folding` v1 | trace | trace audit |
| `trace_index.json` | `execution-trace-index` v2 | trace | invocation lookup |
| `trace_suite.json` | `execution-trace-suite` v1 | trace | refine |
| `defect_context.json` | `defect-context` v1 | trace | refine |
| external locator JSON | `fault-localization-input` v1 | external locator | refine |
| `refinement.json` | `fault-localization-refinement` v5 | refine | evaluate |
| `summaries/evaluation.json` | `fault-localization-evaluation` v2 | evaluate | reporting |

Stage outputs are validated before consumption. Existing refinement results are reused only when the
input, configuration, trace-suite fingerprints, and `top_k` match; otherwise rerun with `--force`.

## Validation

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q mllmfl
.venv/bin/python -m mllmfl --help
.venv/bin/python -m mllmfl refine --root runs/smoke --projects Chart --bugs 1 \
  --locator-results config/locator_input.example.json \
  --config config/mllm.example.json --dry-run
```
