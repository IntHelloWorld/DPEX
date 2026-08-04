# MLLM Fault Localization

This repository provides a reproducible Defects4J fault-localization pipeline. The Python package
`mllmfl` orchestrates collection, Fullchain v2 tracing, fault-focused sequence diagrams, source
summaries, multimodal localization, and per-bug vote aggregation. The bytecode tracer remains an
independent Maven module in `fullchain_tracer/`.

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
```

Every stage supports `--help`. Stages that operate on triggers accept `--trigger`; `--force` replaces
an existing stage result. `localize` should always be exercised with `--dry-run` first. For a real
request, copy the example configuration to an ignored `*.local.json`, set `api_key_env`, and export
that environment variable. Inline API keys are rejected.

PNG rendering defaults to a 16384-pixel PlantUML limit. If a larger diagram reaches that boundary,
`uml` fails explicitly instead of keeping a truncated image; raise `--plantuml-limit-size` or reduce
the trace window.

## Artifact contracts

Stages communicate only through versioned JSON:

| File | Schema | Producer | Consumer |
|---|---|---|---|
| `collect.json` | `collected-trigger` v1 | collect | trace metadata |
| `trace.json` | `fullchain-trace` v2 | trace | diagnostics |
| `window.json` | `fullchain-window` v2 | trace | uml, summarize |
| `uml.json` | `fault-focused-uml` v1 | uml | localization audit |
| `candidates.json` | `fault-candidates` v1 | summarize | localize |
| `localization.json` | `fault-localization` v1 | localize | aggregate |
| `summaries/...json` | `fault-localization-aggregate` v1 | aggregate | evaluation |

Full traces retain invocation IDs, parent chains, descriptors, threads, timing, and return/throw
state. `window.json` is a bounded projection anchored on the failure stack, then the failing test,
then the trace tail. Missing ancestors are restored as context and only consecutive sibling calls
may be compressed.

Candidates are derived exclusively from the normalized window. The summarizer reads only the buggy
checkout; it does not use patches, fixed versions, ground truth, or model predictions.

## Validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q mllmfl
python -m mllmfl --help
```

For changes involving external tools, also run the six commands above on one bug. Trace changes
should additionally be checked on a Maven-based Defects4J project and on a test that exits by
exception.
