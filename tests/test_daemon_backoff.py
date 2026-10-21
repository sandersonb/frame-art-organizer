"""Cooldown applies before every stage, including reachability and identity calls."""
from datetime import datetime, timezone

import pytest
from conftest import add_asset, add_derivative
from test_daemon_and_cli import library, make_daemon, settled, tv  # noqa: F401

from frame_art_organizer import daemon as daemon_mod, store
from frame_art_organizer.frame_client import FrameTimeout


def test_identity_failure_blocks_even_tcp_probes_until_cooldown(tmp_path, tv, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: clock[0])
    d = make_daemon(tmp_path)
    monkeypatch.setattr(d, "_ensure_device", lambda *a: (_ for _ in ()).throw(FrameTimeout("identity")))
    d._tick_once()
    assert d._retry_at == 1300 and d._failure_count == 1
    monkeypatch.setattr(daemon_mod, "FrameClient", lambda *a: pytest.fail("no probes during cooldown"))
    clock[0] = 1299
    d._tick_once()


def test_backoff_increases_and_is_capped(tmp_path, tv, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: clock[0])
    d = make_daemon(tmp_path)
    monkeypatch.setattr(d, "_ensure_device", lambda *a: (_ for _ in ()).throw(FrameTimeout("identity")))
    for delay in [300, 600, 1200, 1800, 1800]:
        d._tick_once()
        assert d._retry_at-clock[0] == delay
        clock[0] = d._retry_at


def test_pending_upload_blocks_identity_harvest_and_refresh(tmp_path, tv, monkeypatch):
    conn, dev, _ = library(tmp_path)
    derivative = add_derivative(conn, add_asset(conn, 2))
    store.create_pending_placement(conn, dev, derivative, "none")
    d = make_daemon(tmp_path)
    monkeypatch.setattr(d, "_ensure_device", lambda *a: pytest.fail("must not open Art session"))
    d._tick_once()
    assert tv.events == [] and d._failure_count == 1
    conn.close()


def test_failed_wake_is_remembered_then_success_resets_backoff(tmp_path, tv, monkeypatch):
    conn, _, _ = library(tmp_path)
    conn.close()
    clock = [1000.0]
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: clock[0])
    d = settled(make_daemon(tmp_path), last_harvest=datetime.now(timezone.utc))
    d._was_reachable = False
    normal = d._ensure_device
    monkeypatch.setattr(d, "_ensure_device", lambda *a: (_ for _ in ()).throw(FrameTimeout("identity")))
    d._tick_once()
    assert d._wake_pending
    monkeypatch.setattr(d, "_ensure_device", normal)
    clock[0] = d._retry_at
    d._tick_once()
    assert "slideshow" in tv.events
    assert not d._wake_pending and d._retry_at == 0 and d._failure_count == 0


def test_incomplete_refresh_does_not_advance_success_timestamp(tmp_path, tv, monkeypatch):
    d = make_daemon(tmp_path)
    monkeypatch.setattr(daemon_mod.scheduler, "refresh", lambda *a, **k: {"errors": 1})
    d._tick_once()
    assert d._last_refresh is None and d._failure_count == 1
