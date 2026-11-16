"""Bounded read-only TV soak. Run with the installed project's Python on Linux."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import sqlite3
import subprocess
import threading
import time
import warnings
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

from urllib3.exceptions import InsecureRequestWarning

from frame_art_organizer import config, upload_recovery
from frame_art_organizer.device_lock import device_lock
from frame_art_organizer.frame_client import FrameClient
from frame_art_organizer.resource_probe import process_record, snapshot

# No library log messages, URLs, response bodies, tokens or raw exceptions.
warnings.filterwarnings('ignore', category=InsecureRequestWarning)


def service_stopped():
    result = subprocess.run(['systemctl', 'show', 'frame-art-organizer',
                             '--property=ActiveState,MainPID,UnitFileState'],
                            capture_output=True, text=True, timeout=5, check=True)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return fields.get('ActiveState') == 'inactive' and fields.get('MainPID') == '0' and fields.get('UnitFileState') == 'disabled'


class Recorder:
    def __init__(self, path, host, sample_seconds):
        self.stream = path.open('x')
        self.host, self.sample_seconds = host, sample_seconds
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.started = time.monotonic()
        self.counts = Counter()
        self.workers = {}
        self.thread = threading.Thread(target=self.watch, name='soak-sampler', daemon=True)

    def emit(self, event, **fields):
        with self.lock:
            self.stream.write(json.dumps({'utc': datetime.now(timezone.utc).isoformat(),
                                          'elapsed_seconds': round(time.monotonic()-self.started, 3),
                                          'event': event, **fields}, sort_keys=True)+'\n')
            self.stream.flush()

    def sample(self, phase):
        try:
            with self.lock:
                workers = dict(self.workers)
            self.emit('resources', phase=phase, **snapshot(os.getpid(), host=self.host, tracked_workers=workers))
        except Exception as exc:
            self.emit('sample_failed', error=type(exc).__name__)

    def progress(self, stage, **fields):
        if stage == 'worker.started':
            try:
                row, _ = process_record(Path('/proc') / str(fields['pid']))
                with self.lock:
                    self.workers[row['pid']] = row['start_ticks']
            except (OSError, ValueError, KeyError, IndexError):
                self.emit('worker_identity_unavailable')
        if stage in {'worker.started', 'worker.stopped', 'operation.complete', 'operation.failed'}:
            safe = {key: fields[key] for key in ('pid', 'operation', 'elapsed_seconds', 'phase',
                    'phase_elapsed_seconds', 'outcome', 'worker_pid', 'failure_reason') if key in fields}
            self.emit(stage, **safe)

    def watch(self):
        while not self.stop.wait(self.sample_seconds):
            self.sample('periodic')

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join()
        self.sample('final_idle')
        self.emit('finished', counters=dict(self.counts))
        self.stream.close()


def validate(args):
    for name, low, high in (('duration', 60, 3600), ('interval', 15, 120),
                            ('sample_seconds', 2, 30), ('idle_seconds', 30, 300)):
        value = getattr(args, name)
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError('Invalid bound')
    if not 1 <= args.max_cycles <= 120 or not 1 <= args.max_failures <= 3 or not 1 <= args.idle_every <= 30:
        raise ValueError('Invalid count bound')


def cycle(cfg, expected_duid, expected_ids, recorder):
    # Same lifecycle as an active daemon tick: fresh client, shared worker for
    # the two reads, close/reap before returning. No recovery/database writes.
    with FrameClient(cfg) as client:
        client.set_observer(recorder.progress)
        if client.device_info().get('device', {}).get('duid') != expected_duid:
            return 'identity_changed'
        ids = upload_recovery.inventory_ids(client.list_my_photos())
        if ids != expected_ids:
            recorder.emit('inventory_changed', new_count=len(ids-expected_ids), missing_count=len(expected_ids-ids))
            return 'inventory_changed'
        recorder.emit('inventory_verified', count=len(ids))
    return 'success'


def summarize(path):
    """Streaming aggregate report; no automatic leak verdict or TV access."""
    counts = Counter()
    first_idle, last_idle = [], deque(maxlen=10)
    ranges = {}
    settings = None
    finished = False
    def records():
        with path.open() as stream:
            for line in stream:
                yield json.loads(line)
    for row in records():
        event = row['event']
        counts[event] += 1
        if event == 'start':
            settings = row
        if event == 'finished':
            finished = True
        if event == 'cycle_end':
            counts['outcome:'+row['outcome']] += 1
        if event != 'resources':
            continue
        parent = row.get('parent')
        if not parent:
            counts['missing_parent_samples'] += 1
            continue
        if row.get('tcp_partial') or row.get('process_scan_partial'):
            counts['partial_samples'] += 1
        phase = row['phase']
        values = {key: parent.get(key) for key in ('rss_kib', 'fd_count', 'socket_count', 'threads')}
        values['descendants'] = len(row['descendants'])
        values['worker_survivors'] = len(row['tracked_worker_survivors'])
        for key, value in values.items():
            if value is None:
                continue
            name = phase+':'+key
            span = ranges.setdefault(name, {'first': value, 'last': value, 'min': value, 'max': value})
            span['last'] = value
            span['min'], span['max'] = min(span['min'], value), max(span['max'], value)
        if phase == 'after_idle':
            if len(first_idle) < 10:
                first_idle.append(values)
            last_idle.append(values)
    def averages(rows):
        return {key: round(sum(row[key] for row in rows if row.get(key) is not None)/
                           sum(row.get(key) is not None for row in rows), 2)
                for key in ('rss_kib', 'fd_count', 'socket_count', 'threads', 'worker_survivors')
                if any(row.get(key) is not None for row in rows)}
    return {'complete_log': finished, 'settings': settings, 'counts': dict(counts),
            'ranges_by_phase': ranges, 'first_ten_idle_means': averages(first_idle),
            'last_ten_idle_means': averages(last_idle),
            'interpretation': 'Compare equivalent post-idle phases; growth is evidence to investigate, not proof of a leak.'}


def main(args):
    validate(args)
    if not Path('/proc/self/status').is_file() or not service_stopped():
        raise RuntimeError('Requires Linux and stopped disabled daemon')
    cfg = config.load(args.config)
    base = args.config.resolve().parent
    database = config.paths(cfg, base)['database'].resolve()
    conn = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    recorder = None
    try:
        fcfg = config.frame_config(cfg, base)
        fcfg.connect_retries = 0
        fcfg.ws_call_timeout = min(fcfg.ws_call_timeout, 20)
        devs = conn.execute('SELECT id,duid FROM device WHERE host=?', (fcfg.host,)).fetchall()
        if len(devs) != 1 or not devs[0]['duid']:
            raise RuntimeError('Expected one recorded identity')
        dev = devs[0]
        if conn.execute("SELECT count(*) FROM placement WHERE device_id=? AND (state='pending' OR upload_outcome='uncertain')", (dev['id'],)).fetchone()[0]:
            raise RuntimeError('Unresolved uploads')
        if conn.execute("SELECT count(*) FROM refresh_batch WHERE device_id=? AND state='active'", (dev['id'],)).fetchone()[0]:
            raise RuntimeError('Active batch')
        ids = {row[0] for row in conn.execute("SELECT content_id FROM placement WHERE device_id=? AND state='present'", (dev['id'],))}
        if args.check_only:
            sample = snapshot(os.getpid(), host=fcfg.host)
            if sample['parent'] is None or sample['tcp_partial']:
                raise RuntimeError('OS sampler unavailable')
            print(json.dumps({'check': 'passed', 'tracked_count': len(ids), 'tv_requests': 0,
                              'parent': sample['parent']}))
            return
        args.output.mkdir(parents=True, exist_ok=True)
        path = args.output / ('frame-resource-soak-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.jsonl')
        logging.disable(logging.CRITICAL)
        recorder = Recorder(path, fcfg.host, args.sample_seconds)
        print('RESOURCE_LOG', path, flush=True)
        # Refuse concurrent managed writers. Only this lock file is opened for
        # writing; the state database itself stays read-only throughout.
        with device_lock(conn, dev['id']):
            recorder.emit('start', parent_pid=os.getpid(), library=importlib.metadata.version('samsungtvws'),
                          script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                          duration=args.duration, interval=args.interval, max_cycles=args.max_cycles,
                          sample_seconds=args.sample_seconds, idle_every=args.idle_every,
                          idle_seconds=args.idle_seconds, retries=0, uploads=0, deletions=0,
                          note='Sampler thread/log/lock/DB descriptors are intentional; multiprocessing tracker may persist')
            recorder.sample('baseline')
            recorder.thread.start()
            deadline = time.monotonic()+args.duration
            failures = 0
            for index in range(1, args.max_cycles+1):
                if time.monotonic() >= deadline:
                    break
                if not service_stopped():
                    recorder.emit('stopped', reason='service_state_changed')
                    break
                recorder.sample('before_cycle')
                recorder.emit('cycle_begin', index=index)
                try:
                    outcome = cycle(fcfg, dev['duid'], ids, recorder)
                except Exception as exc:
                    outcome = type(exc).__name__
                recorder.counts[outcome] += 1
                recorder.emit('cycle_end', index=index, outcome=outcome)
                recorder.sample('after_client_close')
                if outcome in {'identity_changed', 'inventory_changed'}:
                    break
                failures = 0 if outcome == 'success' else failures+1
                if failures >= args.max_failures:
                    recorder.emit('stopped', reason='consecutive_failure_limit')
                    break
                delay = args.idle_seconds if index % args.idle_every == 0 else args.interval
                if outcome != 'success':
                    delay = max(60, delay)
                recorder.emit('idle_begin', seconds=min(delay, max(0, deadline-time.monotonic())))
                time.sleep(min(delay, max(0, deadline-time.monotonic())))
                recorder.sample('after_idle')
            recorder.emit('terminal_idle', seconds=10)
            time.sleep(10)  # Observe delayed cleanup even after a stop condition.
    finally:
        if recorder:
            recorder.close()
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=config.DEFAULT_CONFIG_PATH,
                        help='Configuration file (default: config.toml in the working directory)')
    parser.add_argument('--output', type=Path, default=Path.home())
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--summarize', type=Path, help='Analyze an existing JSONL log only; no TV access')
    parser.add_argument('--duration', type=float, default=1800)
    parser.add_argument('--interval', type=float, default=30)
    parser.add_argument('--sample-seconds', type=float, default=5)
    parser.add_argument('--max-cycles', type=int, default=60)
    parser.add_argument('--max-failures', type=int, default=3)
    parser.add_argument('--idle-every', type=int, default=10)
    parser.add_argument('--idle-seconds', type=float, default=120)
    try:
        options = parser.parse_args()
        if options.summarize:
            print(json.dumps(summarize(options.summarize), indent=2, sort_keys=True))
        else:
            main(options)
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({'stopped': type(exc).__name__}), flush=True)
        raise SystemExit(2)
