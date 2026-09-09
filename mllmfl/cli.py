import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from mllmfl.infrastructure.layout import RunLayout
from mllmfl.stages import cleanup, collect, evaluate, refine, trace

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

    trace_parser = subparsers.add_parser(
        "collect", aliases=["trace"], help="checkout, collect failures and traces in one test execution, then remove checkout"
    )
    _common(trace_parser, include_trigger=False)
    trace_parser.add_argument("--d4j-home", default=os.environ.get("D4J_HOME"))
    trace_parser.add_argument("--java-home", default=os.environ.get("JAVA_HOME"))
    trace_parser.add_argument(
        "--agent-jar",
        default=str(PROJECT_ROOT / "lib" / "fullchain-tracer.jar"),
    )
    trace_parser.add_argument(
        "--config",
        required=True,
        help="JSON configuration containing the trace value-capture policy",
    )
    trace_parser.add_argument(
        "--retain-debug-artifacts",
        action="store_true",
        help="retain raw trace, full execution, and detailed folding diagnostics",
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
    refine_parser.add_argument(
        "--retain-debug-artifacts",
        action="store_true",
        help=(
            "retain recoverable render-error diagnostics; conversations, "
            "per-request usage, and inspection images are always retained"
        ),
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
    evaluate_parser.add_argument(
        "--final-only",
        action="store_true",
        help="after successful evaluation, retain final results and remove intermediates",
    )
    cleanup_parser = subparsers.add_parser(
        "cleanup", help="preview or remove exact legacy trace artifacts"
    )
    _common(cleanup_parser, include_trigger=False, include_force=False)
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="remove allowlisted files; without this flag cleanup is a dry-run",
    )
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

    if args.stage in {"collect", "trace"}:
        rows = collect.run(
            layout, projects, bugs,
            Path(args.d4j_home).expanduser() if args.d4j_home else None,
            Path(args.java_home).expanduser() if args.java_home else None,
            args.timeout, args.force,
            agent_jar=Path(args.agent_jar).expanduser().resolve(),
            config_path=Path(args.config).expanduser().resolve(),
            retain_debug_artifacts=args.retain_debug_artifacts,
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
            args.retain_debug_artifacts,
        )
    elif args.stage == "evaluate":
        if not args.d4j_home:
            parser.error("evaluate requires --d4j-home or D4J_HOME")
        rows = evaluate.run(
            layout,
            projects,
            bugs,
            Path(args.d4j_home).expanduser().resolve(),
            args.final_only,
        )
    else:
        rows = cleanup.run(layout, projects, bugs, apply=args.apply)
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
