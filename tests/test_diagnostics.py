"""Read-only diagnostic stages, token safety and process isolation; no live TV."""
import subprocess
import sys

import pytest
from samsungtvws import SamsungTVWS

from frame_art_organizer import cli, diagnostics
from frame_art_organizer.frame_client import FrameClient


def test_info_diagnostic_flags_and_dispatch(monkeypatch):
    args = cli.build_parser().parse_args(["--config", "example.toml", "info", "--diagnose",
                                          "--diagnostic-timeout", "12"])
    seen = {}

    def run(path, **kwargs):
        seen.update(path=path, **kwargs)
        return 124

    monkeypatch.setattr(diagnostics, "run_info", run)
    monkeypatch.setattr(cli, "_client", lambda args: pytest.fail("parent must not connect"))
    assert cli.cmd_info(args) == 124
    assert seen == {"path": "example.toml", "timeout": 12}


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_deadline_must_be_positive_and_finite(value):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["info", "--diagnostic-timeout=" + value])


def test_normal_info_still_uses_existing_client(monkeypatch, capsys):
    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def device_info(self): return {"device": {"name": "Frame"}}
        def api_version(self): return "5.0.1.0"
        def artmode(self): return "on"
        def list_my_photos(self): return [{}] * 40

    monkeypatch.setattr(cli, "_client", lambda args: (FakeClient(), {}))
    assert cli.cmd_info(cli.build_parser().parse_args(["info"])) == 0
    captured = capsys.readouterr()
    assert "art api     : 5.0.1.0" in captured.out
    assert "my photos   : 40" in captured.out
    assert captured.err == ""


def test_supervisor_kills_and_reaps_on_timeout(monkeypatch):
    calls = []

    class Process:
        pid = 123
        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired("probe", timeout)
            return -9

    monkeypatch.setattr(diagnostics.subprocess, "Popen", lambda command, **kw: Process())
    monkeypatch.setattr(diagnostics.os, "killpg", lambda pid, sig: calls.append(("kill",)))
    events = []
    assert diagnostics._supervise(["unused"], 1, lambda stage, **kw: events.append(stage)) == 124
    assert calls == [("wait", 1), ("kill",), ("wait", None)]
    assert events == ["probe.deadline"]


def test_supervisor_real_stalled_child_exits_without_network():
    events = []
    assert diagnostics._supervise([sys.executable, "-c", "import time; time.sleep(30)"],
                                  .1, lambda stage, **kw: events.append(stage)) == 124
    assert events == ["probe.deadline"]


def test_real_probe_captures_workers_stuck_in_interpreter_shutdown(probe_env, capfd):
    path, _ = probe_env
    # Recreate the important case: the command has returned but a non-daemon
    # executor worker prevents interpreter exit. No network operation is performed.
    script = """
import concurrent.futures, sys, threading
from frame_art_organizer import cli, diagnostics
def fake_info(fc):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    executor.submit(threading.Event().wait)
    executor.shutdown(wait=False)
    return 0
cli._print_info = fake_info
sys.exit(diagnostics._probe(sys.argv[1], .15))
"""
    events = []
    result = diagnostics._supervise([sys.executable, "-u", "-c", script, path],
                                   1.5, lambda stage, **kw: events.append(stage))
    assert result == 124 and events == ["probe.deadline"]
    captured = capfd.readouterr()
    assert "status=success" in captured.err
    assert "Timeout" in captured.err
    assert "_python_exit" in captured.err
    assert 'in wait' in captured.err


def test_supervisor_reaps_on_interrupt(monkeypatch):
    calls = []

    class Process:
        pid = 123
        def wait(self, timeout=None):
            if timeout is not None: raise KeyboardInterrupt
            calls.append("reaped")

    monkeypatch.setattr(diagnostics.subprocess, "Popen", lambda command, **kw: Process())
    monkeypatch.setattr(diagnostics.os, "killpg", lambda pid, sig: calls.append("killed"))
    with pytest.raises(KeyboardInterrupt):
        diagnostics._supervise([], 1, lambda *a, **k: None)
    assert calls == ["killed", "reaped"]


@pytest.fixture
def probe_env(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[frame]\nhost="192.0.2.10"\n')
    # _probe normally runs in a separate process; restore its process-global state
    # and prevent faulthandler timers from leaking into the rest of the test suite.
    monkeypatch.setattr(diagnostics.logging, "disable", lambda level: None)
    stacks = []
    monkeypatch.setattr(diagnostics.faulthandler, "dump_traceback_later",
                        lambda seconds, **kw: stacks.append((seconds, kw["repeat"])))
    monkeypatch.setattr(diagnostics.faulthandler, "dump_traceback", lambda **kw: None)
    monkeypatch.setattr(FrameClient, "is_reachable", lambda self: True)
    monkeypatch.setattr(SamsungTVWS, "rest_device_info",
                        lambda self: {"device": {"name": "Frame"}})
    return str(path), stacks


def test_probe_forwards_observer_and_worker_stack_capture(probe_env, monkeypatch, capsys):
    path, stacks = probe_env
    seen = {}

    class Client:
        def __init__(self, cfg, observer, stack_seconds):
            seen.update(stack_seconds=stack_seconds, retries=cfg.connect_retries)
            self.report = observer
        def __enter__(self): return self
        def __exit__(self, *args): self.report("client.close.complete")
        def device_info(self):
            self.report("rest.device_info.complete")
            return {"device": {"name": "Frame"}}
        def api_version(self):
            self.report("websocket.event", event="ms.channel.ready")
            return "5.0.1.0"
        def artmode(self): return "on"
        def list_my_photos(self): return []

    monkeypatch.setattr(diagnostics, "FrameClient", Client)
    assert diagnostics._probe(path, 10) == 0
    captured = capsys.readouterr()
    for stage in ["rest.device_info.complete", "event=ms.channel.ready",
                  "client.close.complete", "status=success"]:
        assert stage in captured.err
    assert stacks == [(10, True)]
    assert seen == {"stack_seconds": 10, "retries": 0}


def test_probe_sanitizes_exception_messages_and_does_not_retry(probe_env, monkeypatch, capsys):
    from frame_art_organizer.frame_client import FrameConnectionError
    path, _ = probe_env
    calls = []

    def fail(fc):
        calls.append(1)
        raise FrameConnectionError("wss://tv/?token=SECRET_TOKEN")

    monkeypatch.setattr(cli, "_print_info", fail)
    assert diagnostics._probe(path, 10) == 2
    captured = capsys.readouterr()
    assert "SECRET_TOKEN" not in captured.out + captured.err
    assert "status=connection-or-authorization-failure" in captured.err
    assert calls == [1]


def test_probe_marks_timeout_differently_from_sleep(probe_env, monkeypatch, capsys):
    from frame_art_organizer.frame_client import FrameTimeout

    path, _ = probe_env
    monkeypatch.setattr(cli, "_print_info", lambda fc: (_ for _ in ()).throw(FrameTimeout("secret")))
    assert diagnostics._probe(path, 10) == 2
    assert "status=art-or-rest-timeout" in capsys.readouterr().err


def test_missing_ready_is_distinguishable_from_an_unanswered_request(probe_env, monkeypatch, capsys):
    from frame_art_organizer.frame_client import FrameTimeout
    path, _ = probe_env

    class NoReady:
        def __init__(self, cfg, observer, **kw): self.report = observer
        def __enter__(self): return self
        def __exit__(self, *a): self.report("client.close.complete")
        def device_info(self): return {"device": {}}
        def api_version(self):
            self.report("websocket.upgrade.complete")
            self.report("websocket.event", event="ms.channel.connect")
            raise FrameTimeout("secret server response")

    monkeypatch.setattr(diagnostics, "FrameClient", NoReady)
    assert diagnostics._probe(path, 10) == 2
    err = capsys.readouterr().err
    assert "websocket.upgrade.complete" in err and "event=ms.channel.connect" in err
    assert "event=ms.channel.ready" not in err and "art.request.sent" not in err
    assert "secret server response" not in err
