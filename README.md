# MLLM Fault Localization

This repository provides a reproducible Defects4J fault-localization pipeline. The Python package
`mllmfl` orchestrates collection, Fullchain v3 tracing, test-boundary-sliced sequence-diagram
fragments and one bug-level interactive multimodal localization session. The bytecode tracer
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
│   ├── triggers/trigger_<n>/          # isolated trace/UML/candidate artifacts
│   ├── uml_suite.json                 # all failing tests and bug-global Mxxx catalog
│   └── localization.json              # one bug-level agent ranking
├── logs/<stage>/<project>/bug_<id>/   # stage summaries and per-trigger diagnostics
└── summaries/evaluation.json          # evaluation report
```

Use a separate run root for each experiment. Runtime data is never read from source directories.

## Pipeline

Run one Chart bug through the local stages:

```bash
python -m mllmfl collect --root runs/chart-1 --projects Chart --bugs 1
python -m mllmfl trace --root runs/chart-1 --projects Chart --bugs 1
python -m mllmfl uml --root runs/chart-1 --projects Chart --bugs 1 \
  --config config/mllm.example.json
python -m mllmfl summarize --root runs/chart-1 --projects Chart --bugs 1
python -m mllmfl localize --root runs/chart-1 --projects Chart --bugs 1 \
  --config config/mllm.example.json --dry-run
python -m mllmfl evaluate --root runs/chart-1 --projects Chart --bugs 1 \
  --d4j-home "$D4J_HOME"
```

Every stage supports `--help`. Trace and summarize accept `--trigger`; UML and localization process
all failing tests for a bug together. `--force` replaces an existing stage result. `localize` should
always be exercised with `--dry-run` first. For a real
request, copy the example configuration to an ignored `*.local.json`, set `api_key_env`, and export
that environment variable (`OPENAI_API_KEY` by default). Inline API keys are rejected.

Collection processes every failing test in Defects4J export order. Each test receives an isolated
trigger directory and a stable `Txxx` ID. Trace, slicing, UML rendering, and candidate extraction
remain test-isolated; localization consumes all completed tests in one bug-level agent session.

Localization uses OpenAI's Responses API with `gpt-5.5`. Requests use
`reasoning: {"effort": "medium"}`, set
`parallel_tool_calls: false`, and do not send `temperature`, `top_p`, or penalty parameters.
Responses use `store: false`; each continuation replays the complete ordered local history,
including encrypted reasoning output, function calls, tool results, and viewed images. A stable
`prompt_cache_key` is reused during one localization task and rotates after compatible upstream
route failures.

PNG rendering defaults to a 32768-pixel PlantUML limit. If a fragment reaches that boundary, `uml`
fails explicitly instead of keeping a truncated image; raise `--plantuml-limit-size`.

## Artifact contracts

Stages communicate only through versioned JSON:

| File | Schema | Producer | Consumer |
|---|---|---|---|
| `collect.json` | `collected-trigger` v2 | collect | trace metadata |
| `trace.json` | `fullchain-trace` v3 | trace | diagnostics |
| `execution.json` | `fullchain-execution` v3 | trace | summarize, fallback diagnostics |
| `execution_sliced.json` | `fullchain-execution` v3 | trace | uml |
| `test_slice.json` | `test-boundary-slice` v2 | trace | slice audit |
| `defect_context.json` | `defect-context` v1 | trace | localize defect context |
| `uml.json` | `execution-uml-graph` v3 | uml | test-local navigation and audit |
| `uml_suite.json` | `execution-uml-suite` v1 | uml | bug-level localization context |
| `candidates.json` | `fault-candidates` v1 | summarize | localize |
| `localization.json` | `fault-localization` v5 | localize | evaluation |
| `summaries/evaluation.json` | `fault-localization-evaluation` v2 | evaluate | reporting |

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
It builds one uniformly navigable graph of sequence subgraphs. The entry subgraph is centered on the
failing test invocation. If that invocation is absent, a layout-only execution root treats every real
top-level invocation as a direct child without adding a candidate method or changing the trace. Each
subgraph is breadth-first loaded up to 24 visible units and 8 participants by default. A visible unit
is either a call or a consecutive sibling bundle; the focal invocation is context and does not consume
a unit. `CALL_INTERNAL` folds use directional `TO`/`FROM` links. Oversized sibling lists are split
into symmetric `SIBLING_PEER` views: every view repeats the same focal call arrow, exposes every other
peer ID, and uses prefix/suffix notes bound to self-messages on the focal activation bar to map hidden
ranges to `VIEW <diagram-id>`. Consecutive repeated sequences of recursively identical complete
sibling subtrees are compressed bottom-up: a one-call pattern is marked `×N`, while a multi-call
pattern is rendered once inside a `loop repeated sequence ×N` block. The original occurrence IDs
remain recorded in `execution_compressed.json`, and the complete calls remain unchanged in the trace
artifacts. Configure
the limits with `max_visible_units_per_image`, `max_participants_per_image`,
`--max-visible-units`, and `--max-participants` (`--max-calls` remains a CLI alias).
The graph index records these synthetic-root boundary arrows separately as
`layout_root_call_count`; `trace_call_count` remains the unchanged call count from the execution
artifact.

UML and localization are image-only. Call occurrences use `Cxxx`; every distinct runtime method has
one deterministic bug-global `Mxxx` identifier reused across every failing-test image. Diagram IDs
use `Txxx-Dxxx`, so titles, filenames, and `TO`/`FROM`/`VIEW` navigation identify the owning failing
test. `uml_suite.json` maps all entry IDs and contains the private bug-global method catalog used to
resolve model output back to exact functions, signatures, and descriptors. The agent returns the
method ID and short signature shown in an image, for example `M001` and `add(TickUnit)`. Local
resolution accepts an exact ID-and-signature pair. If those fields conflict, it uses a unique
short-signature match among viewed candidate methods; ambiguous or unmatched entries are discarded.

Compare the selected execution's call counts before compression and after compression for one bug:

```bash
.venv/bin/python scripts/count_uml_compression.py \
  --root runs/chart-all --project Chart --bug 4
```

Use `--trigger 1` to inspect one failing test, or `--json` for machine-readable output. The script
reads `execution_compressed.json`; it does not count repeated focus boundaries or navigation notes
introduced later by diagram pagination.

Localization starts one session per bug. The initial user message contains only every failing-test
identifier, its `Txxx` ID, and its entry `Txxx-Dxxx` ID; it contains no image,
test code, error stack, test output, candidate list, project ID, bug ID, or filesystem path. Every
listed entry is initially accessible through `view_sequence_diagram`. Opening an entry returns that
test's line-numbered sliced/full code, error stack, test output, and image. Opening a child linked
from a viewed image returns only a minimal acknowledgement and the image. The prompt permits at most
one diagram request in each assistant response. If the provider still returns multiple tool calls,
the localizer retains and executes only the first; the remaining calls are discarded without
generating tool errors.

The model reads `Txxx-Dxxx` navigation and bug-global `Mxxx` method IDs directly from images and
returns `method_id`, the image-visible short `method_signature`, and `reason`. Resolution is limited
to methods in successfully viewed diagrams that map to the union of local candidates. It prefers
matching both fields; conflicting IDs may be corrected only by a unique short-signature match.
Ambiguous or unresolved entries are discarded. The persisted v5 ranking contains between one and
the configured `top_k` maximum number of evidence-supported methods; the model is not required to
pad the result to that maximum. After
validating model output, the localizer resolves each method against the buggy Java AST and stores
its workspace-relative source file plus declaration start/end lines. Methods without one
unambiguous buggy-source range are discarded. The result records viewed test IDs, viewed diagram
IDs, and tool rounds in the bug-level `localization.json`. Evaluation compares source file and
start/end lines rather than transient UML method IDs. The legacy aggregate command remains only for
historical per-trigger localization artifacts and is not part of the new pipeline.
Before each diagram-tool call, the system prompt asks the model for a JSON progress object describing
its current evidence and why the selected linked subgraph is useful. Tool-navigation requests keep
JSON Output disabled so the provider can return structured tool calls. A response without a diagram
tool call is treated as the final answer and must itself be exactly one JSON object; Markdown fences
and surrounding prose are rejected. If that response is invalid or violates the ranking output
contract, localization preserves the complete conversation and asks the model to correct it while
keeping the same tool-capable request contract. It makes up to `mllm.invalid_final_json_retries`
additional attempts (default `2`) before reporting the validation failure.

Real localization runs also write `conversation.jsonl` beside `localization.json`. It records the
system instruction plus every accepted Responses turn in order, including assistant reasoning
summaries, tool calls, and tool results. When a provider response contains multiple tool
calls, only its retained first call is persisted. Image messages are represented only as
`{\"type\": \"image_ref\", \"diagram_id\": ...}`; Base64 image data is never written to the
conversation log; each referenced local PNG is loaded only when an HTTP request is built.
`response_usage.jsonl` records each completion ID and its provider usage object. Both files are
runtime artifacts and remain git-ignored.

Candidates are derived exclusively from the normalized execution. Method-summary generation is
temporarily disabled: the `summarize` stage still writes the required `candidates.json`, but each
candidate has an empty `summary`, a `SUMMARY_DISABLED` status, and no source implementation metadata.

Evaluation reads the bug-level v5 agent ranking (or a historical aggregate) and Defects4J
`*.src.patch` files. It verifies
each patch against the buggy workspace, reconstructs the fixed source in memory, and maps changed
lines in both versions to methods and constructors with the tree-sitter Java AST. A patch that cannot
be mapped reliably is reported as
`GROUND_TRUTH_ERROR` and excluded from macro metrics; results with no valid agent ranking are
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
