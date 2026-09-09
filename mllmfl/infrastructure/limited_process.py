"""Linux process-group watchdog. RSS/swap limits are sampled, not kernel quotas."""
import os
from pathlib import Path
import signal
import subprocess
import time

from .io import write_compact_json


def memory_sample(group_id):
    rss = swap = 0
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) != group_id:
                continue
            status = dict(line.split(':', 1) for line in (directory / 'status').read_text().splitlines())
            rss += int(status.get('VmRSS', '0 kB').split()[0]) * 1024
            swap += int(status.get('VmSwap', '0 kB').split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            continue  # A process can exit between /proc reads.
    available = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                     if line.startswith('MemAvailable:'))
    return rss, swap, available


def run_limited(command, *, cwd, env, log_dir, timeout, memory_bytes, reserve_bytes):
    if timeout <= 0 or memory_bytes <= 0 or reserve_bytes < 0:
        raise ValueError('invalid process resource limits')
    log_dir.mkdir(parents=True, exist_ok=True)
    metrics = {'schema': 'limited-process', 'schema_version': 1,
               'status': 'RUNNING', 'memory_limit_bytes': memory_bytes,
               'host_reserve_bytes': reserve_bytes, 'peak_group_rss_swap_bytes': 0,
               'timeout_s': timeout, 'enforcement': 'sampled-process-group-rss-plus-swap'}
    started = time.monotonic()
    with (log_dir / 'worker.log').open('wb') as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        metrics['pid'] = process.pid
        try:
            while process.poll() is None:
                rss, swap, available = memory_sample(process.pid)
                metrics.update(rss_bytes=rss, swap_bytes=swap, host_available_bytes=available,
                               elapsed_s=round(time.monotonic() - started, 2))
                metrics['peak_group_rss_swap_bytes'] = max(metrics['peak_group_rss_swap_bytes'], rss + swap)
                if rss + swap > memory_bytes:
                    metrics['status'] = 'MEMORY_LIMIT'
                elif available < reserve_bytes:
                    metrics['status'] = 'HOST_MEMORY_LOW'
                elif time.monotonic() - started > timeout:
                    metrics['status'] = 'TIMEOUT'
                write_compact_json(log_dir / 'resources.json', metrics)
                if metrics['status'] != 'RUNNING':
                    break
                time.sleep(0.25)
        finally:
            # Also reap children left behind by a timed-out external command.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
            if metrics['status'] == 'RUNNING':
                metrics['status'] = 'OK' if process.returncode == 0 else 'ERROR'
            metrics.update(returncode=process.returncode, elapsed_s=round(time.monotonic() - started, 2))
            write_compact_json(log_dir / 'resources.json', metrics)
    return metrics
