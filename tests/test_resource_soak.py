import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from frame_art_organizer import resource_probe


spec = importlib.util.spec_from_file_location('resource_soak', Path(__file__).parents[1]/'scripts/frame_resource_soak.py')
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)


def process(proc, pid, ppid, ticks, sockets=()):
    path = proc / str(pid)
    (path/'fd').mkdir(parents=True)
    fields = ['S', str(ppid)]+['0']*17+[str(ticks)]
    (path/'stat').write_text(f'{pid} (name with ) spaces) '+ ' '.join(fields))
    (path/'status').write_text('VmRSS:\t100 kB\nThreads:\t1\n')
    (path/'cmdline').write_bytes(b'python\0')
    for index, inode in enumerate(sockets):
        (path/'fd'/str(index)).symlink_to(f'socket:[{inode}]')
    return path


def test_os_tree_sockets_and_reparented_worker(tmp_path):
    root = process(tmp_path, 100, 1, 10, ['77'])
    process(tmp_path, 101, 100, 20)
    process(tmp_path, 102, 101, 30)
    process(tmp_path, 103, 1, 40)  # Reparented worker, found independently.
    process(tmp_path, 104, 1, 99)  # Reused PID does not match tracked birth time.
    (root/'net').mkdir()
    (root/'net'/'tcp').write_text('header\n0: 0100007F:1234 0A0200C0:1F42 08 0 0 0 0 0 77\n'
                                '1: 0100007F:1235 0A0200C0:1F42 06 0 0 0 0 0 0\n')
    (root/'net'/'tcp6').write_text('header\n')
    (tmp_path/'meminfo').write_text('MemAvailable: 2048 kB\n')
    result = resource_probe.snapshot(100, host='192.0.2.10', proc=tmp_path,
                                     tracked_workers={103:40, 104:41})
    assert [r['pid'] for r in result['descendants']] == [101,102]
    assert [r['pid'] for r in result['tracked_worker_survivors']] == [103]
    assert result['parent']['socket_count'] == 1
    assert result['family_tcp_states'] == {'08':1}
    assert result['tv_tcp_states_systemwide'] == {'08':1,'06':1}
    assert result['system']['mem_available_kib'] == 2048
    assert '192.0.2.10' not in json.dumps(result)


def test_missing_proc_data_is_explicit(tmp_path):
    process(tmp_path, 1, 0, 1)
    result = resource_probe.snapshot(1, host='192.0.2.1', proc=tmp_path)
    assert result['tcp_partial'] and result['system']['memory_partial']


def test_ipv6_word_order():
    assert str(resource_probe.endpoint_address('00000000000000000000000001000000:1F42')) == '::1'


def test_cli_help_describes_a_portable_config_default():
    script = Path(__file__).parents[1] / 'scripts/frame_resource_soak.py'
    result = subprocess.run([sys.executable, str(script), '--help'], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'config.toml in the working directory' in ' '.join(result.stdout.split())


def test_cycle_only_reads_and_closes(monkeypatch):
    calls = []
    class Client:
        def __init__(self, cfg): pass
        def __enter__(self): calls.append('enter');return self
        def __exit__(self, *args): calls.append('close')
        def set_observer(self, fn): pass
        def device_info(self): calls.append('identity');return {'device':{'duid':'expected'}}
        def list_my_photos(self): calls.append('inventory');return [{'content_id':'MY_F0001'}]
    monkeypatch.setattr(soak, 'FrameClient', Client)
    rec = SimpleNamespace(progress=lambda *a, **k: None, emit=lambda *a, **k: None)
    assert soak.cycle(None, 'expected', {'MY_F0001'}, rec) == 'success'
    assert calls == ['enter','identity','inventory','close']
    calls.clear()
    assert soak.cycle(None, 'different', set(), rec) == 'identity_changed'
    assert calls == ['enter','identity','close']
    assert soak.cycle(None, 'expected', set(), rec) == 'inventory_changed'


@pytest.mark.parametrize('name,value', [('duration', float('nan')), ('duration', 3601),
    ('interval', 0), ('sample_seconds',1), ('max_cycles',121), ('max_failures',4)])
def test_bounds(name, value):
    args = SimpleNamespace(duration=1800, interval=30, sample_seconds=5, idle_seconds=120,
                           max_cycles=60, max_failures=3, idle_every=10)
    setattr(args, name, value)
    with pytest.raises(ValueError): soak.validate(args)


def test_recorder_can_close_before_thread_start(tmp_path):
    recorder = soak.Recorder(tmp_path/'run.jsonl', '192.0.2.1', 5)
    recorder.sample = lambda phase: None
    recorder.close()
    assert json.loads((tmp_path/'run.jsonl').read_text())['event'] == 'finished'


def test_check_only_has_no_tv_requests(conn, tmp_path, monkeypatch, capsys):
    from conftest import add_device
    add_device(conn)
    cfg = tmp_path/'config.toml'
    cfg.write_text('[frame]\nhost="192.0.2.10"\n[paths]\ndatabase="state.db"\n')
    monkeypatch.setattr(soak, 'service_stopped', lambda: True)
    monkeypatch.setattr(soak, 'snapshot', lambda *a, **k: {'parent':{'pid':1},'tcp_partial':False})
    # macOS developer machines do not have /proc: fake only this readiness check.
    original = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda p: True if str(p)=='/proc/self/status' else original(p))
    monkeypatch.setattr(soak, 'FrameClient', lambda *a, **k: pytest.fail('No TV access'))
    args = SimpleNamespace(duration=1800, interval=30, sample_seconds=5, idle_seconds=120,
                           max_cycles=60, max_failures=3, idle_every=10, config=cfg, check_only=True)
    before = list(conn.iterdump())
    soak.main(args)
    assert '"tv_requests": 0' in capsys.readouterr().out
    assert list(conn.iterdump()) == before


def test_service_guard_fails_closed(monkeypatch):
    monkeypatch.setattr(soak.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        stdout='ActiveState=active\nMainPID=123\nUnitFileState=disabled\n'))
    assert not soak.service_stopped()


def test_progress_drops_private_fields(tmp_path, monkeypatch):
    recorder = soak.Recorder(tmp_path/'run.jsonl', '192.0.2.1', 5)
    recorder.progress('operation.failed', operation='list_my_photos', outcome='FrameTimeout',
                      token='SECRET', message='PRIVATE_RESPONSE', url='PRIVATE_URL')
    recorder.sample = lambda phase: None
    recorder.close()
    content = (tmp_path/'run.jsonl').read_text()
    assert not any(value in content for value in ('SECRET', 'PRIVATE_RESPONSE', 'PRIVATE_URL'))


def test_streaming_summary_preserves_idle_growth(tmp_path):
    path = tmp_path/'run.jsonl'
    rows = [{'event':'start'}, {'event':'cycle_end','outcome':'success'}]
    for n in range(20):
        rows.append({'event':'resources','phase':'after_idle',
                     'parent':{'rss_kib':100+n,'fd_count':4,'threads':2,'socket_count':0},
                     'descendants':[], 'tracked_worker_survivors':[]})
    rows.append({'event':'finished'})
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    result = soak.summarize(path)
    assert result['complete_log'] and result['counts']['outcome:success'] == 1
    assert result['first_ten_idle_means']['rss_kib'] == 104.5
    assert result['last_ten_idle_means']['rss_kib'] == 114.5


def test_soak_bounds_repeated_failures_and_never_writes_db(conn, tmp_path, monkeypatch):
    from collections import Counter
    from conftest import add_device
    add_device(conn)
    cfg = tmp_path/'config.toml'
    cfg.write_text('[frame]\nhost="192.0.2.10"\n[paths]\ndatabase="state.db"\n')
    args = SimpleNamespace(duration=600, interval=15, sample_seconds=5, idle_seconds=30,
                           max_cycles=10, max_failures=3, idle_every=2, config=cfg,
                           check_only=False, output=tmp_path/'logs')
    now, sleeps, calls = [0], [], []
    monkeypatch.setattr(soak.time, 'monotonic', lambda: now[0])
    def sleep(seconds):
        sleeps.append(seconds);now[0] += seconds
    monkeypatch.setattr(soak.time, 'sleep', sleep)
    monkeypatch.setattr(soak, 'service_stopped', lambda: True)
    monkeypatch.setattr(soak.logging, 'disable', lambda *a: None)
    original = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda p: True if str(p)=='/proc/self/status' else original(p))
    class Recorder:
        def __init__(self, *a): self.counts=Counter();self.thread=SimpleNamespace(start=lambda:None)
        def emit(self, *a, **k): pass
        def sample(self, *a): pass
        def close(self): pass
    monkeypatch.setattr(soak, 'Recorder', Recorder)
    def cycle(fcfg, *a):
        assert fcfg.connect_retries == 0 and fcfg.ws_call_timeout <= 20
        calls.append(1)
        if len(calls) > 1: raise RuntimeError('PRIVATE')
        return 'success'
    monkeypatch.setattr(soak, 'cycle', cycle)
    before = list(conn.iterdump())
    soak.main(args)
    assert len(calls) == 4 and sleeps == [15,60,60,10]
    assert list(conn.iterdump()) == before
