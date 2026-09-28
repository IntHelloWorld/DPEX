#!/usr/bin/env python3
"""Collect Closure evidence and feed a monitored, resumable refinement pool.

Run after `source d4j_env.sh`. Use --check for input-only validation,
--dry-run to collect and validate without model calls, or --status to monitor.
Completed refinements are validated and reused by the existing stage.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dpex.infrastructure.layout import RunLayout
from dpex.infrastructure.io import read_json, write_csv
from dpex.infrastructure.limited_process import run_limited
from dpex.domain.schemas import validate_trace_suite
from dpex.stages.refine.adapters import _autofl_predictions
from dpex.stages.refine.stage import _refine_bug, refinement_viewport_configuration


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def load_inputs(source, selected=None):
    manifest = json.loads((source / 'sample_manifest.json').read_text())
    if manifest.get('schema') != 'autofl_sample_manifest' or manifest.get('schema_version') != 1:
        raise ValueError('unsupported AutoFL manifest')
    names = manifest.get('bugs')
    if not isinstance(names, list) or not names or len(names) != len(set(names)):
        raise ValueError('empty or duplicate manifest bugs')
    if any(not isinstance(name, str) or not name.startswith('Closure_') or
           not name.removeprefix('Closure_').isdigit() for name in names):
        raise ValueError('manifest must contain Closure bug IDs')
    bugs = sorted([name.removeprefix('Closure_') for name in names], key=int)
    if selected is not None:
        if not selected or not selected.issubset(bugs):
            raise ValueError('--bugs contains IDs absent from the manifest')
        bugs = [bug for bug in bugs if bug in selected]
    for bug in bugs:
        prediction = source / 'predictions' / f'XFL-Closure_{bug}.json'
        _autofl_predictions(json.loads(prediction.read_text()))
    return bugs


def configured_workers(config_path):
    document = json.loads(config_path.read_text())
    batch = document.get('batch', {})
    if not isinstance(batch, dict):
        raise ValueError('config batch must be a JSON object')
    workers = batch.get('workers', 30)
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError('config batch.workers must be a positive integer')
    return workers


def collect_isolated(layout, bug, config_path, args):
    log_dir = layout.stage_log_dir('collect', 'Closure', bug)
    log_dir.mkdir(parents=True, exist_ok=True)
    result_path = log_dir / 'worker_result.json'
    result_path.unlink(missing_ok=True)
    command = [sys.executable, '-m', 'dpex.infrastructure.collect_worker',
               '--root', str(layout.root), '--config', str(config_path),
               '--project', 'Closure', '--bug', bug, '--timeout', str(args.timeout),
               '--address-space-bytes', str(args.collect_memory_gib * 1024 ** 3),
               '--result', str(result_path)]
    env = os.environ.copy()
    # Bound Java reservations as well as its heap under the inherited RLIMIT_AS.
    env['JAVA_TOOL_OPTIONS'] = (env.get('JAVA_TOOL_OPTIONS', '') +
                               ' -Xmx1g -XX:ReservedCodeCacheSize=128m -XX:CompressedClassSpaceSize=256m')
    metrics = run_limited(command, cwd=REPO, env=env, log_dir=log_dir,
                          timeout=args.collect_timeout,
                          memory_bytes=args.collect_memory_gib * 1024 ** 3,
                          reserve_bytes=args.host_reserve_gib * 1024 ** 3)
    if metrics['status'] == 'OK':
        result = json.loads(result_path.read_text())
        if result.get('schema') != 'isolated-collection-result' or result.get('schema_version') != 1:
            raise ValueError('invalid isolated collection result')
        rows = result.get('rows')
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or rows[0].get('project') != 'Closure' or rows[0].get('bug') != bug
                or rows[0].get('status') not in {'OK', 'SKIPPED'}):
            raise ValueError('isolated collection result does not match requested bug')
        return rows
    return [{'project': 'Closure', 'bug': bug, 'status': 'ERROR',
             'trigger_count': 0, 'checkout_removed': False, 'resource_status': metrics['status']}]


def main():
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument(
        '--config', type=Path,
        default=REPO / 'config/dpex.deepseek-flash.local.json',
    )
    config_args, _ = config_parser.parse_known_args()
    default_workers = configured_workers(config_args.config)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=REPO / 'runs/autofl-refine-deepseek-closure-all-standard-20260908')
    parser.add_argument('--locator-results', type=Path, default=REPO / 'reproduce/AutoFL-D4J/results/closure-all-standard-20260908')
    parser.add_argument('--trace-root', type=Path,
                        help='existing run root whose trace suites are read directly')
    parser.add_argument('--config', type=Path, default=config_args.config)
    parser.add_argument('--workers', type=int, default=default_workers)
    parser.add_argument('--timeout', type=int, default=1200, help='timeout per external command/API request')
    parser.add_argument('--collect-timeout', type=int, default=3600, help='wall-clock limit per collected bug')
    parser.add_argument('--collect-memory-gib', type=int, default=6,
                        help='per-process address-space hard cap and sampled collection group RSS+swap cap')
    parser.add_argument('--host-reserve-gib', type=int, default=4)
    parser.add_argument('--bugs', help='optional comma-separated manifest bug IDs')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    layout = RunLayout(args.root.resolve())
    status_path = layout.summaries / 'batch_status.json'
    if args.status:
        state = json.loads(status_path.read_text())
        try:
            os.kill(state['pid'], 0)
            state['process_alive'] = True
        except ProcessLookupError:
            state['process_alive'] = False
        print(json.dumps({key: value for key, value in state.items() if key != 'bugs'}, indent=2))
        for bug, row in state['bugs'].items():
            if row['status'] not in {'PENDING', 'OK', 'SKIPPED', 'DRY_RUN'}:
                print(f"Closure-{bug}: {row}")
        return 0
    if min(args.workers, args.timeout, args.collect_timeout, args.collect_memory_gib, args.host_reserve_gib) < 1:
        parser.error('workers and resource limits must be positive')
    source = args.locator_results.resolve()
    config = json.loads(args.config.read_text())
    cfg = config.get('dpex', config)
    viewport = refinement_viewport_configuration(config)
    if cfg.get('vision_model') != 'deepseek-flash':
        raise ValueError('this batch requires deepseek-flash')
    if 'api_key' in cfg or not cfg.get('api_key_env'):
        raise ValueError('credentials must use api_key_env')
    bugs = load_inputs(source, set(args.bugs.split(',')) if args.bugs else None)
    trace_layout = RunLayout(args.trace_root.resolve()) if args.trace_root else None
    if trace_layout is not None:
        for bug in bugs:
            suite = validate_trace_suite(read_json(
                trace_layout.artifacts / 'Closure' / f'bug_{bug}' / 'trace_suite.json'
            ))
            for spec in suite['tests']:
                trace = trace_layout.artifacts / 'Closure' / f'bug_{bug}' / str(spec['trace'])
                if not trace.is_file() and not trace.with_suffix(trace.suffix + '.zst').is_file():
                    raise ValueError(f'missing external trace artifact for Closure-{bug}')
    if args.check:
        print(json.dumps({'validated_inputs': len(bugs), 'model': cfg['vision_model'],
                          'workers': args.workers,
                          'collect_workers': 0 if trace_layout else 1,
                          'trace_root': str(trace_layout.root) if trace_layout else None,
                          'viewport': viewport,
                          'api_key_present': bool(os.environ.get(cfg['api_key_env']))}))
        return 0
    if not args.dry_run and not os.environ.get(cfg['api_key_env']):
        raise ValueError(f"missing environment variable {cfg['api_key_env']}")
    layout.ensure()
    lock_file = (layout.logs / 'batch.lock').open('w')
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config_path = layout.summaries / 'batch_config.json'
    if config_path.exists() and json.loads(config_path.read_text()) != config:
        raise ValueError('batch config changed; use a new root')
    atomic_json(config_path, config)
    state = {'schema': 'closure-refinement-batch', 'schema_version': 1,
             'pid': os.getpid(), 'started_at': now(), 'phase': 'RUNNING',
             'root': str(layout.root), 'locator_results': str(source),
             'trace_root': str(trace_layout.root) if trace_layout else None,
             'model': cfg['vision_model'], 'reasoning_effort': cfg.get('reasoning_effort'),
             'max_tokens': cfg.get('max_tokens'), 'workers': args.workers,
             'viewport': {
                 'max_upstream_calls': viewport[0],
                 'max_downstream_calls': viewport[1],
                 'max_internal_calls': viewport[2],
             },
             'collect_workers': 0 if trace_layout else 1,
             'dry_run': args.dry_run, 'total': len(bugs),
             'collect_memory_gib': args.collect_memory_gib, 'collect_timeout_s': args.collect_timeout,
             'host_reserve_gib': args.host_reserve_gib,
             'bugs': {bug: {'status': 'PENDING'} for bug in bugs}}
    guard = threading.Lock()
    stop = threading.Event()
    start = time.monotonic()

    def save():
        with guard:
            state['updated_at'] = now()
            state['elapsed_s'] = round(time.monotonic() - start, 1)
            state['counts'] = dict(Counter(row['status'] for row in state['bugs'].values()))
            atomic_json(status_path, state)

    def update(bug, status, **extra):
        with guard:
            state['bugs'][bug].update(status=status, updated_at=now(), **extra)
            event = {'time': now(), 'bug': bug, 'status': status, **extra}
            with (layout.logs / 'batch_events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event) + '\n')
            print(f"{event['time']} Closure-{bug}: {status}", flush=True)
        save()

    def heartbeat():
        while not stop.wait(5):
            save()

    def refine(bug):
        try:
            update(bug, 'DRY_RUNNING')
            arguments = (layout, 'Closure', bug, source, config, args.timeout, None)
            row = _refine_bug(*arguments, True, False, *viewport, False, trace_layout)
            if row['status'] == 'DRY_RUN' and not args.dry_run:
                update(bug, 'REFINING')
                row = _refine_bug(*arguments, False, False, *viewport, False, trace_layout)
            update(bug, row['status'], result=row)
        except Exception as error:
            update(bug, 'ERROR', error=str(error))

    save()
    monitor = threading.Thread(target=heartbeat, daemon=True)
    monitor.start()
    collected = []
    try:
        with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix='closure-refine') as pool:
            pending = set()
            for bug in bugs:
                # Bound the queue and retained trace volume while API work runs.
                while len(pending) >= args.workers * 2:
                    _, pending = wait(pending, return_when=FIRST_COMPLETED)
                if trace_layout is not None:
                    suite = validate_trace_suite(read_json(
                        trace_layout.artifacts / 'Closure' / f'bug_{bug}' / 'trace_suite.json'
                    ))
                    rows = [{'project': 'Closure', 'bug': bug, 'status': 'EXTERNAL',
                             'trigger_count': len(suite['tests']), 'checkout_removed': True,
                             'resource_status': 'NOT_RUN'}]
                else:
                    update(bug, 'COLLECTING')
                    try:
                        rows = collect_isolated(layout, bug, config_path, args)
                    except Exception as error:
                        rows = [{'project': 'Closure', 'bug': bug, 'status': 'ERROR',
                                 'trigger_count': 0, 'checkout_removed': False,
                                 'resource_status': 'WORKER_ERROR'}]
                        error_path = layout.stage_log_dir('collect', 'Closure', bug) / 'worker_error.log'
                        error_path.parent.mkdir(parents=True, exist_ok=True)
                        error_path.write_text(str(error) + '\n', encoding='utf-8')
                collected.extend(rows)
                write_csv(layout.logs / 'collect.csv', collected,
                          ['project', 'bug', 'status', 'trigger_count', 'checkout_removed', 'resource_status'])
                if len(rows) != 1 or rows[0]['status'] not in {'OK', 'SKIPPED', 'EXTERNAL'}:
                    update(bug, 'COLLECT_ERROR', result=rows)
                    continue
                update(bug, 'QUEUED', collect_status=rows[0]['status'])
                pending.add(pool.submit(refine, bug))
        with guard:
            state['phase'] = 'COMPLETED_WITH_ERRORS' if any(
                row['status'] in {'ERROR', 'COLLECT_ERROR'} for row in state['bugs'].values()
            ) else 'COMPLETED'
    except BaseException:
        with guard:
            state['phase'] = 'INTERRUPTED'
        raise
    finally:
        stop.set()
        monitor.join()
        save()
        write_csv(layout.logs / 'refine.csv', [
            {'project': 'Closure', 'bug': bug, 'status': row['status'],
             'top1': row.get('result', {}).get('top1', '') if isinstance(row.get('result'), dict) else ''}
            for bug, row in state['bugs'].items()
        ], ['project', 'bug', 'status', 'top1'])
        lock_file.close()
    return 0 if state['phase'] == 'COMPLETED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
