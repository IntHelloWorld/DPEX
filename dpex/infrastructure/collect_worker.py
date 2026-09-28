"""Isolated collection entry point; address-space limits precede pipeline imports."""
import argparse
import os
from pathlib import Path
import resource


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--bug', required=True)
    parser.add_argument('--timeout', type=int, required=True)
    parser.add_argument('--address-space-bytes', type=int, required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    if args.address_space_bytes <= 0 or args.timeout <= 0:
        parser.error('limits must be positive')
    resource.setrlimit(resource.RLIMIT_AS, (args.address_space_bytes, args.address_space_bytes))
    from dpex.infrastructure.io import write_compact_json
    from dpex.infrastructure.layout import RunLayout
    from dpex.stages import collect

    rows = collect.run(
        RunLayout(args.root), [args.project], {args.bug},
        Path(os.environ['DEFECTS4J_HOME']) if os.environ.get('DEFECTS4J_HOME') else None,
        Path(os.environ['JAVA_HOME']) if os.environ.get('JAVA_HOME') else None,
        args.timeout, agent_jar=Path(__file__).resolve().parents[2] / 'lib/fullchain-tracer.jar',
        config_path=args.config,
    )
    write_compact_json(args.result, {
        'schema': 'isolated-collection-result', 'schema_version': 1,
        'rows': rows, 'peak_self_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
    })
    return 0 if len(rows) == 1 and rows[0]['status'] in {'OK', 'SKIPPED'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
