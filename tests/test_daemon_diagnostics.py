"""Passive production diagnostics: bounded, correlated, and secret-free."""
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from frame_art_organizer import daemon_diagnostics as mod, stack_capture
from frame_art_organizer.daemon_diagnostics import DaemonDiagnostics, PrivateRotatingHandler, safe_progress, settings


def make(tmp_path, mode='diagnostic', **kw):
    return DaemonDiagnostics({'diagnostics': {'mode': mode, **kw}}, tmp_path,
                             tmp_path/'missing.db', '192.0.2.1')


def records(tmp_path):
    return [json.loads(line) for line in (tmp_path/'data/diagnostics/daemon.jsonl').read_text().splitlines()]


@pytest.fixture
def passive(monkeypatch):
    calls = []
    def snapshot(*a, **k):
        calls.append(k)
        return {'parent': {'pid': os.getpid()}, 'descendants': [], 'tracked_worker_survivors': []}
    monkeypatch.setattr(mod, 'snapshot', snapshot)
    monkeypatch.setattr(mod, 'database_snapshot', lambda *a: {'available': False})
    return calls


@pytest.mark.parametrize('key,value', [('mode','debug-everything'), ('sample_seconds',0),
    ('sample_seconds',float('nan')), ('slow_operation_seconds',True), ('stall_seconds',float('inf')),
    ('history_limit',201), ('max_bytes',1), ('backups',0), ('log_directory','')])
def test_invalid_config(key, value):
    with pytest.raises(ValueError):
        settings({'diagnostics':{key:value}})


def test_off_has_no_files_threads_or_resource_reads(tmp_path, monkeypatch):
    diag = make(tmp_path, 'off')
    monkeypatch.setattr(mod, 'snapshot', lambda *a, **k: pytest.fail('must stay off'))
    diag.start({})
    with diag.tick(), diag.stage('rendering'):
        diag.progress('operation.begin', operation='list_my_photos')
    diag.incident('test')
    diag.close()
    assert diag.thread is None and not list(tmp_path.iterdir())


def test_safe_progress_drops_secrets_and_untrusted_labels():
    secret = 'https://tv/path?token=SECRET'
    values = safe_progress({'operation':'list_my_photos', 'client_id':'a'*12,
                            'operation_id':'a'*12+'-1','outcome':secret,'error':secret,
                            'event':secret,'request':secret,'phase':secret,
                            'token':secret,'url':secret,'body':secret,'message':secret,
                            'bytes':12,'elapsed_seconds':float('nan')})
    assert values == {'operation':'list_my_photos','client_id':'a'*12,
                      'operation_id':'a'*12+'-1','outcome':'other','error':'other','bytes':12}
    assert safe_progress({'operation':[], 'event':{}, 'phase':[], 'error':{}}) == {'error':'other'}


def test_correlated_trace_and_bounded_history(tmp_path, passive):
    diag = make(tmp_path, history_limit=10)
    diag.start({'call_timeout':20})
    try:
        with diag.tick(), diag.stage('identity'):
            tick_id = diag.tick_id
            for index in range(30):
                diag.progress('websocket.event', event='ms.channel.ready',
                              operation='list_my_photos',client_id='a'*12, operation_id='a'*12+'-1')
            assert len(diag.history) == 10
            diag.progress('operation.complete', operation='list_my_photos', elapsed_seconds=1)
    finally:
        diag.close()
    rows = records(tmp_path)
    phases = [r for r in rows if r['event']=='rpc.progress']
    assert phases and all(r['tick_id']==tick_id for r in phases)
    assert not (tmp_path/'missing.db').exists()
    assert not diag.thread.is_alive()


def test_normal_mode_keeps_trace_history_but_not_trace_file_lines(tmp_path, passive):
    diag = make(tmp_path, 'normal')
    diag.start({})
    try:
        diag.progress('websocket.waiting_ready',operation='list_my_photos')
        assert any(r.get('stage')=='websocket.waiting_ready' for r in diag.history)
    finally:
        diag.close()
    assert not any(r.get('stage')=='websocket.waiting_ready' for r in records(tmp_path))


def test_independent_monitor_reports_overdue_stage_without_scheduler_progress(tmp_path, passive):
    diag = make(tmp_path)
    incident_seen = threading.Event()
    original = diag.record
    def record(event, **fields):
        original(event, **fields)
        if event == 'incident': incident_seen.set()
    diag.record = record
    # Simulate the scheduler being blocked in rendering; monitor needs no lock
    # owned by the renderer and makes no TV calls.
    diag.tick_id = 'a'*12+'-1'
    diag.tick_since = diag.stage_since = time.monotonic()-100
    diag.stage_name = 'rendering'
    diag.start({})
    try:
        assert incident_seen.wait(3)
    finally:
        diag.close()
    rows = records(tmp_path)
    assert any(r['event']=='health' and r['state']['stage']=='rendering' for r in rows)
    incidents = [r for r in rows if r['event']=='incident']
    assert incidents[0]['reason']=='scheduler_overdue'
    assert incidents[0]['stacks']
    # Incident/history nesting cannot grow recursively over days of failures.
    assert not any(r['event']=='incident' for r in diag.history)
    assert len(diag.pending_incidents) <= 8


def test_failure_capture_preserves_context_and_never_exception_text(tmp_path, passive):
    diag = make(tmp_path)
    seen = threading.Event()
    original = diag.record
    def record(event, **fields):
        original(event, **fields)
        if event == 'incident': seen.set()
    diag.record = record
    diag.start({})
    try:
        with diag.tick():
            diag.progress('operation.begin',operation='upload_jpeg',operation_id='a'*12+'-1')
            diag.progress('operation.failed',operation='upload_jpeg',operation_id='a'*12+'-1',
                          outcome='FrameUploadUncertain',phase='upload.awaiting_ack',
                          message='SECRET_RESPONSE', token='SECRET_TOKEN')
        assert seen.wait(3)
    finally:
        diag.close()
    text = (tmp_path/'data/diagnostics/daemon.jsonl').read_text()
    assert 'SECRET_' not in text
    incident = next(r for r in records(tmp_path) if r['event']=='incident')
    assert incident['tick_id'] is not None
    assert incident['operation']['phase']=='upload.awaiting_ack'
    assert incident['operation']['operation_id']=='a'*12+'-1'


def test_worker_reuse_does_not_invent_survivor(tmp_path, monkeypatch):
    diag = make(tmp_path)
    monkeypatch.setattr(mod, 'process_record', lambda *a: ({'pid':123,'start_ticks':10}, set()))
    diag.progress('worker.started', pid=123)
    diag.progress('worker.stopped', pid=123)
    assert not diag.workers and list(diag.retired_workers)==[(123,10)]
    observed = []
    def snapshot(*a, **kw):
        observed.append(kw['tracked_workers'])
        return {'tracked_worker_survivors':[], 'descendants':[]}
    monkeypatch.setattr(mod, 'snapshot', snapshot)
    diag.health()
    assert observed == [{123:10}]
    assert not diag.pending_incidents


def test_private_rotation_is_size_and_count_bounded(tmp_path):
    import logging
    path = tmp_path/'run.jsonl'
    handler = PrivateRotatingHandler(path, maxBytes=1024, backupCount=2)
    logger = logging.Logger('rotation')
    logger.addHandler(handler)
    for _ in range(100): logger.warning('x'*200)
    handler.close()
    files = list(tmp_path.iterdir())
    assert len(files)==3
    assert all(p.stat().st_size<=1024 for p in files)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in files)


def test_stack_sanitization_has_no_locals_paths_or_source():
    stacks = stack_capture.sanitize_stacks([{'thread_id':1,'token':'SECRET_TOKEN','frames':[
        {'module':'frame_art_organizer.daemon','function':'_tick_once','line':12,
         'source':'SECRET_SOURCE','locals':{'token':'SECRET_TOKEN'},'path':'/SECRET_PATH'},
        {'module':'SECRET_MODULE','function':'SECRET_FUNCTION','line':1}]}])
    text = json.dumps(stacks)
    assert 'SECRET_' not in text
    assert stacks[0]['frames'][0] == {'module':'frame_art_organizer.daemon','function':'_tick_once','line':12}
    try:
        raise RuntimeError('SECRET_MESSAGE https://tv?token=SECRET_TOKEN')
    except RuntimeError as exc:
        assert 'SECRET_' not in json.dumps(stack_capture.exception_stacks(exc))


def test_worker_stack_timer_only_fires_during_active_call():
    reports = []
    with stack_capture.operation_stacks(.02, reports.append):
        time.sleep(.06)
    assert len(reports)==1
    count = len(reports)
    with stack_capture.operation_stacks(.05, reports.append):
        pass
    time.sleep(.08)
    assert len(reports)==count


def test_cooldown_does_not_report_idle_as_stall(tmp_path, passive):
    diag = make(tmp_path)
    diag.stage_since = time.monotonic()-10000
    diag.cooldown(time.monotonic()+300)
    diag.health()
    assert not diag.pending_incidents


def test_enabled_daemon_preserves_harvest_behavior_and_logs_stages(tmp_path, monkeypatch, passive):
    from datetime import datetime, timedelta, timezone
    from conftest import item
    from test_daemon_and_cli import FakeTV, library, make_daemon, settled
    from frame_art_organizer import daemon as daemon_mod
    conn, _, _ = library(tmp_path)
    class ObservedTV(FakeTV):
        def set_observer(self, observer, **kw): self.observer = observer
        def device_info(self):
            self.observer('operation.begin', operation='device_info', operation_id='a'*12+'-1')
            result = super().device_info()
            self.observer('operation.complete', operation='device_info', elapsed_seconds=.1)
            return result
    FakeTV.reachable, FakeTV.listing, FakeTV.events = True, [item('MY_F0001')], []
    monkeypatch.setattr(daemon_mod, 'FrameClient', ObservedTV)
    original = make_daemon(tmp_path)
    original.cfg['diagnostics'] = {'mode':'diagnostic'}
    daemon = settled(daemon_mod.Daemon(original.cfg, tmp_path),
                     last_harvest=datetime.now(timezone.utc)-timedelta(minutes=20))
    daemon._diagnostics.start({})
    try:
        daemon._tick_once()
    finally:
        daemon._diagnostics.close()
        conn.close()
    assert FakeTV.events == ['connect','list']
    rows = records(tmp_path)
    stages = {r['stage'] for r in rows if r['event']=='stage.begin'}
    assert {'tick','summary','reachability','planning','identity','recovery','harvest','database_close'} <= stages
    assert len({r['tick_id'] for r in rows if r['event']=='rpc.progress'}) == 1
    assert daemon._diagnostics.active_rpc is None


def test_unexpected_tick_error_does_not_log_its_message(tmp_path, monkeypatch, caplog):
    from test_daemon_and_cli import make_daemon
    daemon = make_daemon(tmp_path)
    daemon._diagnostics = make(tmp_path)
    def fail():
        raise RuntimeError('SECRET_MESSAGE https://tv?token=SECRET_TOKEN')
    monkeypatch.setattr(daemon, '_tick_body', fail)
    with pytest.raises(RuntimeError): daemon._tick_once()
    assert 'SECRET_' not in caplog.text
    encoded = json.dumps(list(daemon._diagnostics.history))
    assert 'SECRET_' not in encoded and 'tick.failed' in encoded


def test_oversize_incident_cannot_bypass_rotation_limit(tmp_path, passive):
    diag = make(tmp_path, max_bytes=65536)
    diag.start({})
    try:
        diag.record('incident', recent_history=[{'event':'test','value':'x'*1000}]*200)
    finally:
        diag.close()
    assert any(r.get('truncated') for r in records(tmp_path))
    assert all(p.stat().st_size <= 65536 for p in (tmp_path/'data/diagnostics').iterdir())


def test_sink_failure_omits_payload_and_does_not_raise(tmp_path, caplog):
    import logging
    handler = PrivateRotatingHandler(tmp_path/'run.jsonl', maxBytes=1024, backupCount=1)
    class BrokenStream:
        def write(self, *a): raise OSError('SECRET_ERROR')
        def flush(self): pass
        def close(self): pass
        def seek(self, *a): pass
        def tell(self): return 0
    handler.stream.close()
    handler.stream = BrokenStream()
    logger = logging.Logger('broken-sink')
    logger.addHandler(handler)
    logger.warning('SECRET_PAYLOAD')
    logger.warning('SECRET_PAYLOAD')
    handler.close()
    assert handler.failure_count == 2
    assert 'SECRET_' not in caplog.text
    assert caplog.text.count('diagnostic log write failed') == 1


def test_incident_queue_and_history_remain_bounded(tmp_path):
    diag = make(tmp_path, history_limit=10)
    for index in range(100):
        diag.tick_id = 'a'*12+f'-{index}'
        diag.record('tick.begin')
        diag.incident('operation_failed')
    assert len(diag.pending_incidents)==8
    assert len(diag.history)==10 and len(diag.incident_keys)==32
    assert diag.dropped_incidents==92


def test_transient_monitor_failure_does_not_disable_future_sampling(tmp_path):
    diag = make(tmp_path)
    calls = []
    def health():
        calls.append(1)
        if len(calls)==1:
            raise RuntimeError('SECRET_ERROR')
        diag.stop_event.set();diag.wake_event.set()
    diag.health = health
    original = diag.stop_event.wait
    diag.stop_event.wait = lambda seconds: original(0)
    diag.monitor()
    assert len(calls)==2
    assert any(r['event']=='monitor.failed' for r in diag.history)
    assert 'SECRET_ERROR' not in json.dumps(list(diag.history))
