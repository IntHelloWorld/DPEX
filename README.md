# MLLM Fault Localization

This repository provides a reproducible Defects4J fault-localization pipeline. The Python package
`mllmfl` orchestrates collection, Fullchain v3 tracing, test-boundary-sliced sequence-diagram
fragments, interactive multimodal localization, and per-bug vote aggregation. The bytecode tracer
remains an independent Maven module in `fullchain_tracer/`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
source d4j_env.sh
mvn -q -f fullchain_tracer/pom.xml package
```

The pipeline writes exclusively under `--root` using this layout:

```text
<root>/
├── workspace/                         # Defects4J checkouts
├── artifacts/<project>/bug_<id>/
│   └── triggers/trigger_<n>/          # versioned stage artifacts
├── logs/<stage>/<project>/bug_<id>/   # stage summaries and per-trigger diagnostics
└── summaries/<project>/bug_<id>.json # aggregate ranking
```

Use a separate run root for each experiment. Runtime data is never read from source directories.

## Pipeline

Run one Chart bug through the local stages:

```bash
python -m mllmfl collect --root runs/chart-1 --projects Chart --bugs 1
python -m mllmfl trace --root runs/chart-1 --projects Chart --bugs 1
python -m mllmfl uml --root runs/chart-1 --projects Chart --bugs 1
python -m mllmfl summarize --root runs/chart-1 --projects Chart --bugs 1
python -m mllmfl localize --root runs/chart-1 --projects Chart --bugs 1 \
  --config config/mllm.example.json --dry-run
python -m mllmfl aggregate --root runs/chart-1 --projects Chart --bugs 1
python -m mllmfl evaluate --root runs/chart-1 --projects Chart --bugs 1 \
  --d4j-home "$D4J_HOME"
```

Every stage supports `--help`. Stages that operate on triggers accept `--trigger`; `--force` replaces
an existing stage result. `localize` should always be exercised with `--dry-run` first. For a real
request, copy the example configuration to an ignored `*.local.json`, set `api_key_env`, and export
that environment variable. Inline API keys are rejected.

Localization uses the Responses API with `reasoning_effort: medium`. It does not send a
`temperature` parameter, so reasoning effort is the only model-generation control configured by
this pipeline.

PNG rendering defaults to a 32768-pixel PlantUML limit. If a fragment reaches that boundary, `uml`
fails explicitly instead of keeping a truncated image; raise `--plantuml-limit-size`.

## Artifact contracts

Stages communicate only through versioned JSON:

| File | Schema | Producer | Consumer |
|---|---|---|---|
| `collect.json` | `collected-trigger` v1 | collect | trace metadata |
| `trace.json` | `fullchain-trace` v3 | trace | diagnostics |
| `execution.json` | `fullchain-execution` v3 | trace | summarize, fallback diagnostics |
| `execution_sliced.json` | `fullchain-execution` v3 | trace | uml |
| `test_slice.json` | `test-boundary-slice` v2 | trace | slice audit |
| `defect_context.json` | `defect-context` v1 | trace | localize defect context |
| `uml.json` | `execution-uml-index` v2 | uml | localize navigation and audit |
| `candidates.json` | `fault-candidates` v1 | summarize | localize |
| `localization.json` | `fault-localization` v3 | localize | aggregate |
| `summaries/...json` | `fault-localization-aggregate` v1 | aggregate | evaluation |
| `summaries/evaluation.json` | `fault-localization-evaluation` v1 | evaluate | reporting |

Full traces retain invocation IDs, parent chains, descriptors, threads, timing, and return/throw
state and the originating test-source line. `execution.json` retains every recorded application call
after framework-noise filtering. The trace stage additionally locates the failed test statement,
uses the tree-sitter Java AST to perform a conservative local-variable and control-dependency
backward slice over the test method, and writes
`execution_sliced.json`. Calls that cannot be mapped to the test body (including fixture and async
work) are retained conservatively. JVM class initializers (`<clinit>`) are excluded;
their complete invocation subtrees are also excluded so the trace never invents a direct caller for
methods reached only through class initialization.

UML prefers `execution_sliced.json` and falls back to `execution.json` when no slice artifact exists.
It treats the failing test invocation as level 0 and emits one image under `sequence_diagrams/` for
each direct runtime child invocation. Repeated calls to the same method remain separate index entries.
Each image contains only that invocation and its descendants. Within a fragment, adjacent completely
identical sibling subtrees are still merged from the highest level downward; the first subtree is
drawn once and its root message is marked `×N`. The complete calls remain unchanged in trace artifacts.

Localization initially sends no image. Its prompt contains the failing-test identifier, complete
line-numbered sliced test code, error stack, test output (including an explicitly empty section),
and the ordered fragment index; it omits the project, bug ID, and candidate-function list. Missing
sliced source or an unavailable error stack prevents localization from starting. A tool-capable
vision model can call
`view_sequence_diagram` as many times as needed, one image per tool round; the localizer validates the requested ID, appends the
corresponding image and that fragment's ordered unique method signatures to the same Chat Completions
conversation, and records viewed diagram IDs and tool
rounds in `localization.json`. Candidate functions remain local and are used only to validate the
model's final ranking. Aggregate accepts historical localization v1/v2 and current v3 results.
The model's final JSON uses `signature` and `reason`; every signature must exactly match a method
signature returned by a successfully viewed diagram. The persisted v3 ranking also records the
corresponding signature alongside the legacy function identifier used by aggregation.

Real localization runs also write `conversation.jsonl` beside `localization.json`. It records every
system, user, assistant, and tool message in order, including the raw final assistant message. Image
messages are represented only as `{\"type\": \"image_ref\", \"diagram_id\": ...}`; Base64 image
data is never written to the conversation log. The file is a runtime artifact and remains git-ignored.

Candidates are derived exclusively from the normalized execution. Method-summary generation is
temporarily disabled: the `summarize` stage still writes the required `candidates.json`, but each
candidate has an empty `summary`, a `SUMMARY_DISABLED` status, and no source implementation metadata.

Evaluation reads the per-bug aggregate agent rankings and Defects4J `*.src.patch` files. It verifies
each patch against the buggy workspace, reconstructs the fixed source in memory, and maps changed
lines in both versions to methods and constructors with the tree-sitter Java AST. A patch that cannot
be mapped reliably is reported as
`GROUND_TRUTH_ERROR` and excluded from macro metrics; aggregates with no valid agent ranking are
reported as `NO_VALID_RESULT`. The versioned JSON contains per-bug ground-truth methods, relevant
ranks, Top-1/Top-3/Top-5 hits, reciprocal rank, and average precision. Its overall metrics are macro
Top-1/Top-3/Top-5 accuracy, mean reciprocal rank (MRR), and mean average precision (MAP). Average
precision divides by the complete ground-truth method count, so relevant methods missing from the
agent's finite ranking contribute zero. A flat per-bug report is also written to `logs/evaluate.csv`.

## Validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q mllmfl
python -m mllmfl --help
```

For changes involving external tools, also run the pipeline commands above on one bug. Trace changes
should additionally be checked on a Maven-based Defects4J project and on a test that exits by
exception.
