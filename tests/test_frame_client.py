"""FrameClient hardening: close both sockets, drop on timeout, retry transient connect failures,
validate the matte, portrait_matte always 'none'. No TV: fake connections that record closes."""
import threading

import pytest
from samsungtvws import exceptions as ws_exc

from frame_art_organizer import frame_client
from frame_art_organizer.frame_client import FrameClient, FrameConfig, FrameTimeout


class FakeArt:
    instances = []

    def __init__(self):
        self.closed = 0
        self.uploads = []
        FakeArt.instances.append(self)

    def close(self):
        self.closed += 1

    def upload(self, data, **kw):
        self.uploads.append((data, kw))
        return "MY_F0999"

    def get_artmode(self):
        return "on"


class FakeTVWS:
    def __init__(self, **kw):
        self.closed = 0

    def art(self):
        return FakeArt()

    def close(self):
        self.closed += 1


@pytest.fixture
def fc(monkeypatch):
    FakeArt.instances = []
    monkeypatch.setattr(frame_client, "SamsungTVWS", FakeTVWS)
    c = FrameClient(FrameConfig(host="192.0.2.10", ws_call_timeout=0.2, connect_retry_delay=0))
    monkeypatch.setattr(c, "ensure_awake", lambda: None)             # skip the TCP probe
    yield c
    c.close()


# --- closing ---------------------------------------------------------------------------------
def test_close_closes_the_art_socket_too_not_just_the_remote_one(fc):
    art, tv = fc.art, fc.tv
    fc.close()
    assert art.closed == 1 and tv.closed == 1                         # the old code only closed `tv`
    assert fc._art is None and fc._tv is None
    fc.close()                                                        # idempotent
    assert art.closed == 1


def test_a_timeout_drops_the_connection_and_releases_the_stuck_worker(fc):
    art = fc.art
    release = threading.Event()
    started = threading.Event()

    def stuck():
        started.set()
        release.wait(5)

    with pytest.raises(FrameTimeout):
        fc._bounded(stuck, timeout=0.05)

    assert started.is_set() and art.closed == 1 and fc._art is None   # socket closed, not leaked
    release.set()


def test_after_a_drop_the_next_call_uses_a_fresh_connection(fc):
    first = fc.art
    fc._drop_connection()
    assert fc.art is not first and len(FakeArt.instances) == 2


# --- transient connect failures --------------------------------------------------------------
def _failure():
    return ws_exc.ConnectionFailure({"event": "ms.channel.clientDisconnect"})


def test_a_transient_connection_failure_is_retried_on_a_fresh_connection(fc):
    seen = []

    def call():
        art = fc.art                                                  # reaches the connection lazily
        seen.append(art)
        if len(seen) < 3:
            raise _failure()
        return art.get_artmode()

    assert fc._bounded(call) == "on"
    assert len(seen) == 3 and len({id(a) for a in seen}) == 3         # a new connection each time
    assert seen[0].closed == 1 and seen[1].closed == 1                # failed ones were closed


def test_retries_are_bounded(fc):
    calls = []

    def call():
        calls.append(1)
        raise _failure()

    with pytest.raises(ws_exc.ConnectionFailure):
        fc._bounded(call)
    assert len(calls) == 1 + fc.cfg.connect_retries == 3


def test_other_errors_are_not_retried(fc):
    calls = []

    def call():
        calls.append(1)
        raise ws_exc.ResponseError("`change_matte` request failed with error number -7")

    with pytest.raises(ws_exc.ResponseError):
        fc._bounded(call)
    assert len(calls) == 1


def test_a_timeout_is_not_retried(fc):
    calls = []

    def call():
        calls.append(1)
        threading.Event().wait(1)

    with pytest.raises(FrameTimeout):
        fc._bounded(call, timeout=0.05)
    assert len(calls) == 1


# --- upload: matte validation and portrait_matte ----------------------------------------------
def test_upload_always_sends_portrait_matte_none(fc):
    assert fc.upload_jpeg(b"jpg", matte="flexible_black", date="2026:10:02 12:00:00") == "MY_F0999"
    (data, kw), = fc.art.uploads
    assert kw == {"matte": "flexible_black", "portrait_matte": "none", "file_type": "JPEG",
                  "date": "2026:10:02 12:00:00"}


def test_upload_normalizes_the_matte_and_defaults_to_none(fc):
    fc.upload_jpeg(b"a", matte="  Shadowbox_Sage ")
    fc.upload_jpeg(b"b")
    assert [kw["matte"] for _, kw in fc.art.uploads] == ["shadowbox_sage", "none"]
    assert all(kw["portrait_matte"] == "none" for _, kw in fc.art.uploads)


@pytest.mark.parametrize("bad", ["modern", "fancy_black", "modern_purple", "", "flexible-black"])
def test_an_unknown_matte_is_refused_before_it_reaches_the_tv(fc, bad):
    with pytest.raises(ValueError, match="not in the TV's matte list"):
        fc.upload_jpeg(b"jpg", matte=bad)
    assert FakeArt.instances == []                                    # never even connected
