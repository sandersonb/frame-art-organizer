"""Real spawned worker lifecycle and retry safety, with fake TV I/O (no network)."""
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from samsungtvws import exceptions

from frame_art_organizer.frame_client import (
    FrameClient, FrameConfig, FrameConnectionError, FrameTimeout, FrameUploadUncertain,
)


class FakeArt:
    def __init__(self, tv):
        self.tv = tv

    def get_api_version(self):
        self.tv.record("api_version")
        if self.tv.mode == "hang":
            time.sleep(60)
            self.tv.record("late_return")
        return "5.0.1.0"

    def get_artmode(self):
        self.tv.record("artmode")
        if self.tv.mode == "connectfail":
            raise exceptions.ConnectionFailure("secret raw response")
        if self.tv.mode == "responseerror":
            raise exceptions.ResponseError("secret raw response")
        return "on"

    def upload(self, data, **kw):
        self.tv.record("upload", data=data.decode(), kwargs=kw)
        if self.tv.mode == "preflightfail":
            raise exceptions.ConnectionFailure("not submitted")
        self.upload_submitted = True
        if self.tv.mode == "hang-upload":
            time.sleep(60)
        self.tv.record("committed")
        if self.tv.mode == "lostack":
            raise exceptions.ConnectionFailure("lost acknowledgement")
        if self.tv.mode == "error-after-submit":
            raise exceptions.ResponseError("ambiguous error after sending image")
        if self.tv.mode == "invalidack":
            return None
        return "MY_F0999"

    def delete(self, cid):
        self.tv.record("delete")
        if self.tv.mode == "delete-error-absent":
            raise exceptions.ResponseError("already absent")
        return False

    def available(self, category):
        self.tv.record("list")
        return [{"content_id": "MY_F0001"}] if self.tv.mode == "delete-refused" else []

    def close(self):
        self.tv.record("art.close")
        if self.tv.mode == "hang-close":
            time.sleep(60)


class FakeTV:
    def __init__(self, host, token_file, **kwargs):
        self.mode, self.trace = host, token_file
        self.record("created")
        if host == "hang-startup":
            time.sleep(60)

    def record(self, stage, **fields):
        with open(self.trace, "a") as stream:
            stream.write(json.dumps({"stage": stage, "pid": os.getpid(), **fields}) + "\n")

    def art(self): return FakeArt(self)
    def close(self): self.record("tv.close")


@pytest.fixture
def client(tmp_path):
    clients = []

    def create(mode="good"):
        trace = tmp_path / f"{len(clients)}.jsonl"
        fc = FrameClient(FrameConfig(host=mode, token_file=str(trace),
                                    ws_call_timeout=3, connect_retry_delay=0), worker_factory=FakeTV)
        clients.append(fc)
        return fc, trace

    yield create
    for fc in clients:
        fc.close()


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_close_closes_art_and_remote_and_reaps_worker(client):
    fc, trace = client()
    assert fc.api_version() == "5.0.1.0"
    pid = fc._process.pid
    fc.close()
    assert [r["stage"] for r in records(trace)][-2:] == ["art.close", "tv.close"]
    assert fc._process is None and fc._pipe is None
    with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    before = records(trace)
    fc.close()
    assert records(trace) == before


def test_worker_is_serialized_and_reused_only_within_a_client(client):
    fc, _ = client()
    assert fc.api_version() == "5.0.1.0"
    pid = fc._process.pid
    assert fc.artmode() == "on" and fc._process.pid == pid
    fc.close()
    assert fc.api_version() == "5.0.1.0" and fc._process.pid != pid


def test_worker_can_be_started_from_daemon_background_thread(client):
    fc, _ = client()
    results = []
    thread = threading.Thread(target=lambda: results.append(fc.api_version()))
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and results == ["5.0.1.0"]


def test_timeout_kills_real_worker_before_it_can_return_late(client):
    fc, trace = client("hang")
    start = time.monotonic()
    with pytest.raises(FrameTimeout):
        fc._call("api_version", timeout=.6)
    assert time.monotonic() - start < 2
    assert fc._process is None
    assert not any(r["stage"] == "late_return" for r in records(trace))
    assert not any(p.name == "frameart-tv" for p in multiprocessing.active_children())


def test_repeated_timeouts_do_not_accumulate_children(client):
    for _ in range(3):
        fc, _ = client("hang")
        with pytest.raises(FrameTimeout): fc._call("api_version", timeout=.4)
        fc.close()
    assert not any(p.name == "frameart-tv" for p in multiprocessing.active_children())


def test_hung_cleanup_is_bounded_and_reaped(client):
    fc, _ = client("hang-close")
    fc.api_version()
    pid = fc._process.pid
    start = time.monotonic()
    fc.close()
    assert time.monotonic()-start < 2
    with pytest.raises(ProcessLookupError): os.kill(pid, 0)


def test_connection_retries_are_bounded_and_use_fresh_workers(client):
    fc, trace = client("connectfail")
    with pytest.raises(FrameConnectionError): fc.artmode()
    attempts = [r for r in records(trace) if r["stage"] == "artmode"]
    assert len(attempts) == 3 and len({r["pid"] for r in attempts}) == 3


def test_upload_retry_is_allowed_only_before_submission(client):
    fc, trace = client("preflightfail")
    with pytest.raises(FrameConnectionError): fc.upload_jpeg(b"jpg")
    assert len([r for r in records(trace) if r["stage"] == "upload"]) == 3
    assert not any(r["stage"] == "committed" for r in records(trace))


@pytest.mark.parametrize("mode", ["lostack", "error-after-submit", "invalidack"])
def test_uploaded_image_with_missing_reply_is_never_replayed(client, mode):
    fc, trace = client(mode)
    with pytest.raises(FrameUploadUncertain): fc.upload_jpeg(b"jpg")
    stages = [r["stage"] for r in records(trace)]
    assert stages.count("upload") == stages.count("committed") == 1


def test_upload_timeout_is_uncertain_and_worker_is_reaped(client):
    fc, trace = client("hang-upload")
    with pytest.raises(FrameUploadUncertain):
        fc._call("upload_jpeg", (b"jpg",), {}, timeout=.6)
    assert fc._process is None
    assert not any(r["stage"] == "committed" for r in records(trace))


def test_large_upload_to_worker_hung_during_startup_is_bounded(client):
    fc, _ = client("hang-startup")
    start = time.monotonic()
    with pytest.raises(FrameUploadUncertain):
        fc._call("upload_jpeg", (b"x"*4_000_000,), {}, timeout=.6)
    assert time.monotonic()-start < 2 and fc._process is None


def test_worker_exits_if_its_parent_is_abruptly_killed(tmp_path):
    trace = tmp_path / "orphan.jsonl"
    script = f"""
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_frame_client import FakeTV
from frame_art_organizer.frame_client import FrameClient, FrameConfig
fc=FrameClient(FrameConfig(host='hang',token_file={str(trace)!r},ws_call_timeout=60),worker_factory=FakeTV)
fc.api_version()
"""
    parent = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic()+5
        while time.monotonic() < deadline and not any(r["stage"] == "api_version" for r in records(trace)):
            time.sleep(.02)
        rows = records(trace)
        assert any(r["stage"] == "api_version" for r in rows)
        pid = rows[0]["pid"]
        parent.kill()
        parent.wait()
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            # Some Linux PID 1 implementations leave an exited orphan as a zombie.
            stat = Path(f"/proc/{pid}/stat")
            if stat.exists() and stat.read_text().split()[2] == "Z":
                break
            time.sleep(.02)
        else:
            pytest.fail("orphaned TV worker survived its parent")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_response_errors_are_not_retried_and_do_not_expose_payload(client):
    fc, trace = client("responseerror")
    with pytest.raises(exceptions.ResponseError) as exc: fc.artmode()
    assert "secret" not in str(exc.value)
    assert len([r for r in records(trace) if r["stage"] == "artmode"]) == 1


@pytest.mark.parametrize("mode,expected", [("delete-absent", True), ("delete-error-absent", True),
                                          ("delete-refused", False)])
def test_unconfirmed_delete_requires_inventory_confirmation(client, mode, expected):
    fc, trace = client(mode)
    assert fc.delete("MY_F0001") is expected
    assert [r["stage"] for r in records(trace)].count("delete") == 1
    assert [r["stage"] for r in records(trace)].count("list") == 1


def test_upload_preserves_date_and_always_sends_portrait_none(client):
    fc, trace = client()
    assert fc.upload_jpeg(b"jpg", matte="flexible_black", date="2026:10:02 12:00:00") == "MY_F0999"
    kw = next(r["kwargs"] for r in records(trace) if r["stage"] == "upload")
    assert kw == {"matte": "flexible_black", "portrait_matte": "none", "file_type": "JPEG",
                  "date": "2026:10:02 12:00:00"}


def test_upload_normalizes_matte_and_defaults_none(client):
    fc, trace = client()
    fc.upload_jpeg(b"a", matte="  Shadowbox_Sage ")
    fc.upload_jpeg(b"b")
    uploads = [r for r in records(trace) if r["stage"] == "upload"]
    assert [r["kwargs"]["matte"] for r in uploads] == ["shadowbox_sage", "none"]


@pytest.mark.parametrize("bad", ["modern", "fancy_black", "modern_purple", "", "flexible-black"])
def test_unknown_matte_is_refused_without_starting_a_worker(client, bad):
    fc, trace = client()
    with pytest.raises(ValueError, match="not in the TV's matte list"):
        fc.upload_jpeg(b"jpg", matte=bad)
    assert fc._process is None and not trace.exists()
