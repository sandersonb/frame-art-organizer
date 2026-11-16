"""Opt-in passive daemon diagnostics with bounded history and rotating JSONL."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .frame_client import OPERATIONS
from .observability import database_snapshot, resources, utcnow
from .resource_probe import process_record, snapshot
from .stack_capture import capture_stacks, sanitize_stacks

STAGES = {'tick', 'summary', 'reachability', 'database_open', 'planning', 'rendering',
          'identity', 'recovery', 'refresh', 'slideshow', 'harvest', 'database_close', 'idle', 'cooldown'}
PROGRESS = {'operation.begin', 'operation.dispatched', 'operation.complete', 'operation.failed',
            'operation.timeout', 'operation.error', 'worker.started', 'worker.stopped', 'worker.reap.begin',
            'worker.stack', 'tcp.probe.begin', 'tcp.probe.complete', 'tcp.probe.unreachable',
            'rest.device_info.begin', 'rest.device_info.complete', 'websocket.upgrade.begin',
            'websocket.upgrade.complete', 'websocket.waiting_connect', 'websocket.waiting_ready',
            'websocket.event', 'art.request.begin', 'art.request.sent', 'art.response.waiting',
            'art.request.complete', 'art.response.error', 'pairing.token_saved', 'connection.retry',
            'connection.close.begin', 'connection.close.complete', 'client.close.begin', 'client.close.complete',
            'upload.submitted', 'upload.awaiting_transfer_ready', 'upload.transfer_connect.begin',
            'upload.transfer_connect.complete', 'upload.bytes_sent', 'upload.awaiting_ack',
            'upload.acknowledged', 'upload.interrupted'}
EVENTS = {'ms.channel.connect', 'ms.channel.ready', 'ms.channel.clientConnect', 'ms.channel.clientDisconnect',
          'ms.channel.unauthorized', 'ms.channel.timeOut', 'ms.error', 'ed.edenTV.update',
          'ms.voiceApp.hide', 'd2d_service_message', 'ms.remote.touchEnable', 'ms.remote.touchDisable', 'other'}
ERRORS = {'FrameAsleep', 'FrameTimeout', 'FrameConnectionError', 'FrameUploadUncertain', 'DeviceBusy',
          'UploadRecoveryPending', 'UploadResolutionRequired', 'ResponseError', 'ConnectionFailure',
          'UnauthorizedError', 'ApiUnreachable', 'WebSocketTimeoutException', 'WebSocketConnectionClosedException',
          'TimeoutError', 'ConnectionError', 'OSError', 'BrokenPipeError', 'EOFError', 'ValueError',
          'RuntimeError', 'OperationalError', 'IntegrityError', 'KeyboardInterrupt', 'none', 'unknown',
          'deadline', 'invalid_ack', 'worker_exit', 'channel_error'}
ART_REQUESTS = {'api_version', 'get_api_version', 'get_artmode_status', 'get_current_artwork',
                'get_content_list', 'get_slideshow_status', 'set_slideshow_status', 'send_image',
                'delete_image_list', 'select_image', 'other'}
INCIDENT_REASONS = {'slow_operation', 'slow_cleanup', 'scheduler_overdue', 'worker_survived_reap',
                    'operation_failed', 'tick_failed', 'render_failed', 'manual_review',
                    'refresh_incomplete', 'upload_uncertain'}


def safe_progress(fields):
    out = {}
    for key, value in fields.items():
        if key in {'pid', 'worker_pid', 'attempt', 'attempts', 'bytes', 'client_count', 'host_count',
                   'connect_client_count', 'connect_host_count', 'budget_seconds', 'elapsed_seconds',
                   'phase_elapsed_seconds', 'seconds', 'code'}:
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                out[key] = value
        elif key in {'matched', 'upload_submission_observed', 'upload_submitted'} and isinstance(value, bool):
            out[key] = value
        elif key in {'client_id', 'observation_id'} and isinstance(value, str) and re.fullmatch(r'[a-f0-9]{12}', value):
            out[key] = value
        elif key == 'operation_id' and isinstance(value, str) and re.fullmatch(r'[a-f0-9]{12}-[0-9]{1,12}', value):
            out[key] = value
        elif key == 'operation' and isinstance(value, str) and value in OPERATIONS:
            out[key] = value
        elif key == 'request' and isinstance(value, str) and value in ART_REQUESTS:
            out[key] = value
        elif key == 'event' and isinstance(value, str) and value in EVENTS:
            out['websocket_event'] = value
        elif key in {'phase', 'last_event'} and isinstance(value, str) and value in PROGRESS | EVENTS:
            out[key] = value
        elif key in {'outcome', 'error', 'cause', 'remote_error', 'remote_cause', 'failure_reason'}:
            out[key] = value if isinstance(value, str) and value in ERRORS else 'other'
        # No URLs, tokens, photo names, server bodies, arguments or arbitrary strings.
    return out


def settings(cfg):
    raw = cfg.get('diagnostics', {})
    if not isinstance(raw, dict):
        raise ValueError('[diagnostics] must be a table')
    out = {'mode': raw.get('mode', 'off'), 'sample_seconds': raw.get('sample_seconds', 60),
           'stall_seconds': raw.get('stall_seconds', 90), 'slow_operation_seconds': raw.get('slow_operation_seconds', 10),
           'max_bytes': raw.get('max_bytes', 5*1024*1024), 'backups': raw.get('backups', 4),
           'history_limit': raw.get('history_limit', 100), 'log_directory': raw.get('log_directory', 'data/diagnostics')}
    if not isinstance(out['mode'], str) or out['mode'] not in {'off', 'normal', 'diagnostic'}:
        raise ValueError('[diagnostics] mode must be off, normal or diagnostic')
    for key, low, high in [('sample_seconds', 5, 300), ('stall_seconds', 10, 3600),
                           ('slow_operation_seconds', 1, 60)]:
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'[diagnostics] invalid {key}')
    for key, low, high in [('max_bytes', 65536, 50*1024*1024), ('backups', 1, 10), ('history_limit', 10, 200)]:
        if type(out[key]) is not int or not low <= out[key] <= high:
            raise ValueError(f'[diagnostics] invalid {key}')
    if not isinstance(out['log_directory'], str) or not out['log_directory']:
        raise ValueError('[diagnostics] invalid log_directory')
    return out


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        return os.fdopen(fd, 'a', encoding='utf-8')

    def handleError(self, record):
        # logging's default error handler can dump the entire record to stderr.
        # A failed diagnostic sink must not throw or recursively log its payload.
        self.failure_count = getattr(self, 'failure_count', 0)+1
        if self.failure_count == 1:
            logging.getLogger(__name__).warning('diagnostic log write failed; records may be missing (payload omitted)')


class DaemonDiagnostics:
    def __init__(self, cfg, base, database, host):
        self.cfg = settings(cfg)
        self.enabled = self.cfg['mode'] != 'off'
        self.base, self.database, self.host = Path(base), database, host
        self.session = uuid.uuid4().hex[:12]
        self.lock = threading.RLock()
        self.history = deque(maxlen=self.cfg['history_limit'])
        self.workers = {}  # Only live observed identities; completed workers are OS-checked at stop.
        self.retired_workers = deque(maxlen=64)
        self.tick_id = None
        self.tick_serial = 0
        self.stage_name, self.stage_since = 'idle', time.monotonic()
        self.tick_since = self.last_tick = self.last_success = None
        self.active_rpc = None
        self.active_cleanup = None
        self.retry_at = 0
        self.incident_keys = deque(maxlen=32)
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.pending_incidents = deque(maxlen=8)
        self.sample_requested = False
        self.thread = None
        self.logger = None
        self.scheduler_ident = None
        self.dropped_records = 0
        self.dropped_incidents = 0

    def record(self, event, *, trace=False, **fields):
        if not self.enabled:
            return
        with self.lock:
            row = {'utc': utcnow(), 'event': event, 'diagnostic_id': self.session,
                   'tick_id': self.tick_id, **fields}
            if event not in {'health', 'incident', 'worker.stacks'}:
                self.history.append(row)
            if self.logger is not None and (not trace or self.cfg['mode'] == 'diagnostic'):
                try:
                    encoded = json.dumps(row, sort_keys=True)
                    limit = min(65536, self.cfg['max_bytes']//2)
                    if len(encoded.encode()) > limit:
                        row = {k: v for k, v in row.items() if k != 'recent_history'} | {'truncated': True}
                        encoded = json.dumps(row, sort_keys=True)
                    if len(encoded.encode()) > limit:
                        row = {k: v for k, v in row.items() if k in
                               {'utc', 'event', 'diagnostic_id', 'tick_id', 'reason'}} | {'truncated': True}
                        encoded = json.dumps(row, sort_keys=True)
                    self.logger.log(logging.DEBUG if trace else logging.INFO, encoded)
                except Exception:
                    self.dropped_records += 1

    def start(self, effective):
        if not self.enabled:
            return
        self.scheduler_ident = threading.get_ident()
        try:
            directory = self.base / self.cfg['log_directory']
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / 'daemon.jsonl'
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
            self.logger = logging.Logger('frameart-diagnostic-'+self.session, level=logging.DEBUG)
            handler = PrivateRotatingHandler(path, maxBytes=self.cfg['max_bytes'], backupCount=self.cfg['backups'])
            handler.setFormatter(logging.Formatter('%(message)s'))
            self.logger.addHandler(handler)
            # Only allowlisted diagnostic records reach this sink; no third-party logs.
            versions = {}
            for package in ('samsungtvws', 'websocket-client', 'Pillow'):
                try:
                    versions[package] = importlib.metadata.version(package)
                except importlib.metadata.PackageNotFoundError:
                    versions[package] = 'unavailable'
            digest = hashlib.sha256()
            for source in sorted(Path(__file__).parent.glob('*.py')):
                digest.update(source.name.encode());digest.update(source.read_bytes())
            self.record('diagnostics.start', pid=os.getpid(), python=sys.version.split()[0],
                        versions=versions, code_sha256=digest.hexdigest(), settings=self.cfg | {'log_directory': 'configured'},
                        effective=effective, note='Parent resources include web UI; OS samples are non-atomic; no additional TV probes')
            self.thread = threading.Thread(target=self.monitor, name='frameart-diagnostics', daemon=True)
            self.thread.start()
        except Exception as exc:
            self.record('diagnostics.unavailable', error=type(exc).__name__ if type(exc).__name__ in ERRORS else 'other')
            logging.getLogger(__name__).warning('daemon diagnostics unavailable (%s)', type(exc).__name__)
            self.close()
            self.enabled = False

    def close(self):
        self.stop_event.set();self.wake_event.set()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                return  # Monitor closes its own sink when it eventually exits.
        self._close_sink()

    def _close_sink(self):
        with self.lock:
            if self.logger is not None and self.logger.handlers:
                self.record('diagnostics.stop')
                for handler in self.logger.handlers[:]:
                    handler.close();self.logger.removeHandler(handler)

    @contextmanager
    def tick(self):
        if not self.enabled:
            yield
            return
        with self.lock:
            self.tick_serial += 1
            self.tick_id = f'{self.session}-{self.tick_serial}'
            self.tick_since = time.monotonic()
        self.record('tick.begin')
        try:
            with self.stage('tick'):
                yield
        finally:
            with self.lock:
                self.last_tick = time.monotonic()
                self.record('tick.end', duration_seconds=round(self.last_tick-self.tick_since, 3))
                self.tick_since = None
                self.tick_id = None

    @contextmanager
    def stage(self, name):
        if not self.enabled:
            yield
            return
        if name not in STAGES:
            raise ValueError('Unknown diagnostic stage')
        with self.lock:
            previous = self.stage_name, self.stage_since
            self.stage_name, self.stage_since = name, time.monotonic()
            begun = self.stage_since
        self.record('stage.begin', stage=name, trace=True)
        try:
            yield
        finally:
            self.record('stage.end', stage=name, duration_seconds=round(time.monotonic()-begun, 3), trace=True)
            with self.lock:
                self.stage_name, self.stage_since = previous

    def cooldown(self, retry_at):
        with self.lock:
            self.retry_at = retry_at

    def incident(self, reason):
        if not self.enabled:
            return
        reason = reason if reason in INCIDENT_REASONS else 'other'
        with self.lock:
            key = (reason, self.tick_id, (self.active_rpc or {}).get('operation_id'), self.stage_since)
            if key in self.incident_keys:
                return
            self.incident_keys.append(key)
            if len(self.pending_incidents) == self.pending_incidents.maxlen:
                self.dropped_incidents += 1
            self.pending_incidents.append({'reason': reason, 'tick_id': self.tick_id,
                                           'incident_utc': utcnow(),
                                           'operation': dict(self.active_rpc or {}), 'stage': self.stage_name,
                                           'cleanup': dict(self.active_cleanup or {}),
                                           'recent_history': list(self.history)})
        self.wake_event.set()

    def progress(self, stage, **fields):
        if not self.enabled or stage not in PROGRESS:
            return
        safe = safe_progress(fields)
        with self.lock:
            if stage == 'operation.begin':
                self.active_rpc = dict(safe, started_mono=time.monotonic())
            elif self.active_rpc is not None:
                self.active_rpc.update({k: v for k, v in safe.items() if k != 'elapsed_seconds'})
                if stage in PROGRESS - {'websocket.event', 'worker.stack', 'operation.failed', 'operation.complete'}:
                    self.active_rpc['phase'] = stage
            if stage in {'client.close.begin', 'worker.reap.begin'}:
                self.active_cleanup = dict(safe, phase=stage, started_mono=time.monotonic())
            if stage in {'client.close.complete', 'worker.stopped'}:
                self.active_cleanup = None
            if stage == 'worker.started' and 'pid' in safe:
                try:
                    row, _ = process_record(Path('/proc') / str(safe['pid']))
                    self.workers[row['pid']] = row['start_ticks']
                except (OSError, ValueError, KeyError, IndexError):
                    self.record('worker.identity_unavailable')
            self.record('rpc.progress', stage=stage, details=safe, trace=stage not in {'operation.complete', 'operation.failed'})
            if stage == 'worker.stack':
                # Stacks are produced by our worker, never by server responses.
                self.record('worker.stacks', stacks=sanitize_stacks(fields.get('stacks')), details=safe)
            if stage == 'worker.stopped' and 'pid' in safe:
                ticks = self.workers.pop(safe['pid'], None)
                if ticks is not None:
                    self.retired_workers.append((safe['pid'], ticks))
                # Independent descendant sampling catches remaining/reparented processes.
                self.sample_requested = True
                self.wake_event.set()
            if stage == 'operation.failed':
                self.incident('operation_failed')
            if stage in {'operation.complete', 'operation.failed'}:
                if stage == 'operation.complete':
                    self.last_success = time.monotonic()
                self.active_rpc = None

    def health(self):
        with self.lock:
            now = time.monotonic()
            active = dict(self.active_rpc or {})
            if active:
                active['age_seconds'] = round(now-active.pop('started_mono'), 3)
            state = {'stage': self.stage_name, 'stage_age_seconds': round(now-self.stage_since, 3),
                     'tick_age_seconds': round(now-self.tick_since, 3) if self.tick_since is not None else None,
                     'last_tick_age_seconds': round(now-self.last_tick, 3) if self.last_tick is not None else None,
                     'last_success_age_seconds': round(now-self.last_success, 3) if self.last_success is not None else None,
                     'cooldown_remaining_seconds': round(max(0, self.retry_at-now), 3), 'active_operation': active,
                     'scheduler_thread_present': self.scheduler_ident in sys._current_frames()}
            state['dropped_records'] = self.dropped_records + sum(
                getattr(h, 'failure_count', 0) for h in self.logger.handlers) if self.logger else self.dropped_records
            state['dropped_incidents'] = self.dropped_incidents
            cleanup = dict(self.active_cleanup or {})
            if cleanup:
                cleanup['age_seconds'] = round(now-cleanup.pop('started_mono'), 3)
            state['active_cleanup'] = cleanup
            retired = dict(self.retired_workers)
            workers = retired | self.workers
        try:
            os_state = snapshot(os.getpid(), host=self.host, tracked_workers=workers)
            os_state['reaped_worker_survivors'] = [row for row in os_state['tracked_worker_survivors']
                                                   if retired.get(row['pid']) == row['start_ticks']]
        except (OSError, ValueError):
            os_state = {'partial': True, 'parent': resources()}
        self.record('health', state=state, resources=os_state)
        if os_state.get('reaped_worker_survivors'):
            self.incident('worker_survived_reap')
        if state['tick_age_seconds'] is not None and state['stage_age_seconds'] >= self.cfg['stall_seconds']:
            self.incident('scheduler_overdue')
        if active and active['age_seconds'] >= self.cfg['slow_operation_seconds']:
            self.incident('slow_operation')
        if cleanup and cleanup['age_seconds'] >= self.cfg['slow_operation_seconds']:
            self.incident('slow_cleanup')

    def monitor(self):
        try:
            self._monitor_loop()
        finally:
            self._close_sink()

    def _monitor_loop(self):
        next_health = 0
        while not self.stop_event.is_set():
            try:
                now = time.monotonic()
                with self.lock:
                    requested = self.sample_requested
                    self.sample_requested = False
                if now >= next_health or requested:
                    self.health()
                    next_health = now+self.cfg['sample_seconds']
                with self.lock:
                    pending = list(self.pending_incidents)
                    self.pending_incidents.clear()
                    active = self.active_rpc
                    slow = active is not None and now-active['started_mono'] >= self.cfg['slow_operation_seconds']
                    cleanup = self.active_cleanup
                    slow_cleanup = cleanup is not None and now-cleanup['started_mono'] >= self.cfg['slow_operation_seconds']
                    overdue = self.tick_since is not None and now-self.stage_since >= self.cfg['stall_seconds']
                if slow:
                    self.incident('slow_operation')
                if overdue:
                    self.incident('scheduler_overdue')
                if slow_cleanup:
                    self.incident('slow_cleanup')
                for incident in pending:
                    self.record('incident', **incident, stacks=capture_stacks(),
                                database=database_snapshot(self.database, self.host, utcnow()))
                    self.health()
                self.wake_event.wait(min(1, max(.01, next_health-time.monotonic())))
                self.wake_event.clear()
            except Exception as exc:
                self.record('monitor.failed', error=type(exc).__name__ if type(exc).__name__ in ERRORS else 'other')
                # Retry only diagnostics, never the TV operation. Avoid a tight
                # loop if /proc or the diagnostic sink is persistently broken.
                self.stop_event.wait(self.cfg['sample_seconds'])
