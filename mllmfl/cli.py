import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from mllmfl.infrastructure.layout import RunLayout
from mllmfl.stages import aggregate, collect, evaluate, localize, summarize, trace, uml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECTS = [
    "Chart",
    "Cli",
    "Closure",
    "Codec",
    "Collections",
    "Compress",
    "Csv",
    "Gson",
    "JacksonCore",
    "JacksonDatabind",
    "JacksonXml",
    "Jsoup",
    "JxPath",
    "Lang",
    "Math",
    "Mockito",
    "Time",
]


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bugs(value: str | None) -> set[str] | None:
    if not value or value.upper() == "ALL":
        return None
    result = set(_csv(value))
    if not all(item.isdigit() for item in result):
        raise ValueError("--bugs must contain numeric IDs or ALL")
    return result


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _common(
    parser: argparse.ArgumentParser,
    include_trigger: bool = True,
    include_force: bool = True,
) -> None:
    parser.add_argument(
        "--root",
        default="./runs/default",
        help="run root containing workspace/artifacts/logs/summaries",
    )
    parser.add_argument(
        "--projects",
        default="ALL",
        help="comma-separated Defects4J project names or ALL",
    )
    parser.add_argument("--bugs", "--bug", default="ALL", help="comma-separated bug IDs or ALL")
    if include_trigger:
        parser.add_argument("--trigger", help="one trigger index")
    parser.add_argument("--timeout", type=int, default=1200)
    if include_force:
        parser.add_argument("--force", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mllmfl",
        description="MLLM fault-localization pipeline",
    )
    subparsers = parser.add_subparsers(dest="stage", required=True)

    collect_parser = subparsers.add_parser(
        "collect", help="checkout, compile, and collect failing tests"
    )
    _common(collect_parser, include_trigger=False)
    collect_parser.add_argument("--d4j-home", default=os.environ.get("D4J_HOME"))
    collect_parser.add_argument("--java-home", default=os.environ.get("JAVA_HOME"))
    trace_parser = subparsers.add_parser(
        "trace", help="record Fullchain v3 executions and test-boundary slices"
    )
    _common(trace_parser)
    trace_parser.add_argument("--d4j-home", default=os.environ.get("D4J_HOME"))
    trace_parser.add_argument("--java-home", default=os.environ.get("JAVA_HOME"))
    trace_parser.add_argument(
        "--agent-jar",
        default=str(PROJECT_ROOT / "lib" / "fullchain-tracer.jar"),
    )

    uml_parser = subparsers.add_parser(
        "uml", help="render adaptively folded runtime sequence subgraphs"
    )
    _common(uml_parser, include_trigger=False)
    uml_parser.add_argument("--plantuml-command", default="plantuml")
    uml_parser.add_argument(
        "--config", default=str(PROJECT_ROOT / "config" / "mllm.example.json"),
        help="configuration file containing UML image limits",
    )
    uml_parser.add_argument(
        "--max-visible-units", "--max-calls",
        dest="max_visible_units", type=_positive_int,
        help="maximum visible calls plus sibling bundles per subgraph",
    )
    uml_parser.add_argument("--max-participants", type=_positive_int)
    uml_parser.add_argument("--plantuml-batch-size", type=_positive_int)
    uml_parser.add_argument(
        "--plantuml-jar", default=str(PROJECT_ROOT / "lib" / "plantuml.jar")
    )
    uml_parser.add_argument(
        "--plantuml-limit-size",
        type=int,
        default=32768,
        help="maximum fragment PNG width/height before rendering fails",
    )

    summary_parser = subparsers.add_parser(
        "summarize", help="extract execution candidates (method summaries disabled)"
    )
    _common(summary_parser)
    summary_parser.add_argument("--candidate-cap", type=_positive_int, default=100)
    summary_parser.add_argument(
        "--max-summary-chars", type=int, default=240,
        help="reserved while method-summary generation is disabled",
    )
    summary_parser.add_argument(
        "--max-called-methods", type=int, default=8,
        help="reserved while method-summary generation is disabled",
    )

    localize_parser = subparsers.add_parser("localize", help="rank candidates with an MLLM")
    _common(localize_parser, include_trigger=False)
    localize_parser.add_argument("--config", required=True)
    localize_parser.add_argument("--top-k", type=_positive_int)
    localize_parser.add_argument("--dry-run", action="store_true")

    aggregate_parser = subparsers.add_parser(
        "aggregate", help="aggregate historical trigger-level rankings per bug"
    )
    _common(aggregate_parser, include_trigger=False, include_force=False)
    aggregate_parser.add_argument("--top-k", type=_positive_int, default=5)

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="evaluate bug-level rankings against Defects4J patches"
    )
    _common(evaluate_parser, include_trigger=False, include_force=False)
    evaluate_parser.add_argument("--d4j-home", default=os.environ.get("D4J_HOME"))
    return parser


def _projects(value: str) -> Sequence[str]:
    selected = _csv(value)
    return DEFAULT_PROJECTS if not selected or "ALL" in selected else selected


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        bugs = _bugs(args.bugs)
    except ValueError as error:
        parser.error(str(error))
    layout = RunLayout(Path(args.root).expanduser().resolve())
    layout.ensure()
    projects = _projects(args.projects)

    if args.stage == "collect":
        rows = collect.run(
            layout,
            projects,
            bugs,
            Path(args.d4j_home).expanduser() if args.d4j_home else None,
            Path(args.java_home).expanduser() if args.java_home else None,
            args.timeout,
            args.force,
        )
    elif args.stage == "trace":
        rows = trace.run(
            layout,
            projects,
            bugs,
            args.trigger,
            Path(args.agent_jar).expanduser().resolve(),
            Path(args.d4j_home).expanduser() if args.d4j_home else None,
            Path(args.java_home).expanduser() if args.java_home else None,
            args.timeout,
            args.force,
        )
    elif args.stage == "uml":
        uml_config = json.loads(Path(args.config).expanduser().read_text(encoding="utf-8"))
        uml_cfg = uml_config.get("uml") or {}
        max_visible_units = args.max_visible_units or int(
            uml_cfg.get(
                "max_visible_units_per_image",
                uml_cfg.get("max_calls_per_image", 24),
            )
        )
        max_participants = (
            args.max_participants or int(uml_cfg.get("max_participants_per_image", 8))
        )
        plantuml_batch_size = (
            args.plantuml_batch_size or int(uml_cfg.get("plantuml_batch_size", 100))
        )
        rows = uml.run(
            layout,
            projects,
            bugs,
            None,
            args.plantuml_command,
            Path(args.plantuml_jar).expanduser().resolve()
            if args.plantuml_jar
            else None,
            args.timeout,
            args.force,
            args.plantuml_limit_size,
            max_visible_units,
            max_participants,
            plantuml_batch_size,
        )
    elif args.stage == "summarize":
        rows = summarize.run(
            layout,
            projects,
            bugs,
            args.trigger,
            args.candidate_cap,
            args.max_summary_chars,
            args.max_called_methods,
            args.force,
        )
    elif args.stage == "localize":
        rows = localize.run(
            layout,
            projects,
            bugs,
            None,
            Path(args.config).expanduser().resolve(),
            args.timeout,
            args.top_k,
            args.dry_run,
            args.force,
        )
    elif args.stage == "aggregate":
        rows = aggregate.run(layout, projects, bugs, args.top_k)
    else:
        if not args.d4j_home:
            parser.error("evaluate requires --d4j-home or D4J_HOME")
        rows = evaluate.run(
            layout,
            projects,
            bugs,
            Path(args.d4j_home).expanduser().resolve(),
        )
    counts = {}
    for row in rows:
        status = str(row.get("status", "OK"))
        counts[status] = counts.get(status, 0) + 1
    print(
        json.dumps(
            {"stage": args.stage, "processed": len(rows), "statuses": counts},
            ensure_ascii=False,
        )
    )
