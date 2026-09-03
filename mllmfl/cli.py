import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from mllmfl.infrastructure.layout import RunLayout
from mllmfl.stages import collect, evaluate, refine, trace

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
        description="MLLM fault-localization and refinement pipeline",
    )
    subparsers = parser.add_subparsers(dest="stage", required=True)

    collect_parser = subparsers.add_parser(
        "collect", help="checkout, compile, and collect failing tests"
    )
    _common(collect_parser, include_trigger=False)
    collect_parser.add_argument("--d4j-home", default=os.environ.get("D4J_HOME"))
    collect_parser.add_argument("--java-home", default=os.environ.get("JAVA_HOME"))
    trace_parser = subparsers.add_parser(
        "trace", help="record Fullchain v4 executions and refinement indexes"
    )
    _common(trace_parser)
    trace_parser.add_argument("--d4j-home", default=os.environ.get("D4J_HOME"))
    trace_parser.add_argument("--java-home", default=os.environ.get("JAVA_HOME"))
    trace_parser.add_argument(
        "--agent-jar",
        default=str(PROJECT_ROOT / "lib" / "fullchain-tracer.jar"),
    )
    capture_group = trace_parser.add_mutually_exclusive_group()
    capture_group.add_argument(
        "--capture-values", dest="capture_values", action="store_true",
        help="capture bounded entry arguments and normal return values (default)",
    )
    capture_group.add_argument(
        "--no-capture-values", dest="capture_values", action="store_false",
        help="disable argument and return-value capture",
    )
    trace_parser.set_defaults(capture_values=True)
    trace_parser.add_argument("--value-max-chars", type=_positive_int, default=120)
    trace_parser.add_argument("--value-max-items", type=_positive_int, default=8)
    trace_parser.add_argument("--value-max-depth", type=int, default=2)
    trace_parser.add_argument(
        "--value-max-arguments-chars", type=_positive_int, default=480
    )

    refine_parser = subparsers.add_parser(
        "refine", help="audit and optimize an external fault-localization ranking"
    )
    _common(refine_parser, include_trigger=False)
    refine_parser.add_argument(
        "--locator-results",
        required=True,
        help="canonical locator result or supported adapter input such as AutoFL XFL",
    )
    refine_parser.add_argument("--config", required=True)
    refine_parser.add_argument("--top-k", type=_positive_int)
    refine_parser.add_argument("--dry-run", action="store_true")
    refine_parser.add_argument("--max-upstream-calls", type=_positive_int, default=6)
    refine_parser.add_argument("--max-downstream-calls", type=_positive_int, default=6)
    refine_parser.add_argument("--max-internal-calls", type=_positive_int, default=10)
    refine_parser.add_argument(
        "--workers", "--max-workers", dest="workers", type=_positive_int,
        default=1,
        help="maximum number of bugs refined concurrently (default: 1)",
    )

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
    if args.stage == "trace" and args.value_max_depth < 0:
        parser.error("--value-max-depth must be non-negative")
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
            args.capture_values,
            args.value_max_chars,
            args.value_max_items,
            args.value_max_depth,
            args.value_max_arguments_chars,
        )
    elif args.stage == "refine":
        rows = refine.run(
            layout,
            projects,
            bugs,
            None,
            Path(args.locator_results).expanduser().resolve(),
            Path(args.config).expanduser().resolve(),
            args.timeout,
            args.top_k,
            args.dry_run,
            args.force,
            args.workers,
            args.max_upstream_calls,
            args.max_downstream_calls,
            args.max_internal_calls,
        )
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
