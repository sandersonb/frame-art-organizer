"""Daemon harvest cadence and the `fao harvest` command — no TV, a fake client."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from conftest import add_device, item, resident

from frame_art_organizer import cli, daemon as daemon_mod, db, scheduler, store
from frame_art_organizer.frame_client import FrameTimeout


class FakeTV:
    """Class-level state so tests can steer it; instances are made by the daemon itself."""
    reachable, listing, list_exc, events = True, [], None, []

    def __init__(self, cfg):
        self.cfg = SimpleNamespace(host="192.0.2.10")

    def is_reachable(self):
        return FakeTV.reachable

    def __enter__(self):
        FakeTV.events.append("connect")
        return self

    def __exit__(self, *exc):
        return False

    def device_info(self):
        return {"device": {"name": "Test Frame", "duid": "uuid:test-device"}}

    def api_version(self):
        return "5.0.1.0"

    def list_my_photos(self):
        FakeTV.events.append("list")
        if FakeTV.list_exc:
            raise FakeTV.list_exc
        return list(FakeTV.listing)

    def set_slideshow(self, **kw):
        FakeTV.events.append("slideshow")


@pytest.fixture
def tv(monkeypatch):
    FakeTV.reachable, FakeTV.listing, FakeTV.list_exc, FakeTV.events = True, [], None, []
    monkeypatch.setattr(daemon_mod, "FrameClient", FakeTV)
    return FakeTV


def make_daemon(tmp_path, harvest_minutes=15):
    cfg = {"frame": {"host": "192.0.2.10"},
           "paths": {"inbox": "in", "originals": "o", "derivatives": "d", "database": "state.db"},
           "daemon": {"harvest_minutes": harvest_minutes}, "schedule": {"refresh_hours": 24}}
    return daemon_mod.Daemon(cfg, tmp_path)


def settled(d, *, last_harvest):
    """A daemon that has just refreshed (so a refresh is NOT due) and was already awake."""
    now = datetime.now(timezone.utc)
    d._applied_period, d._last_refresh, d._was_reachable = "all-day", now, True
    d._last_harvest = last_harvest
    return d


def library(tmp_path, matte="none"):
    conn = db.open_db(tmp_path / "state.db")
    dev = add_device(conn)
    a, _, p = resident(conn, dev, 1, matte=matte)
    return conn, dev, a


# --- cadence ------------------------------------------------------------------------------
def test_harvest_due_timing(tmp_path):
    d = make_daemon(tmp_path)
    t0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    assert d._harvest_due(t0)                                       # never harvested yet
    d._last_harvest = t0
    assert not d._harvest_due(t0 + timedelta(minutes=14, seconds=59))
    assert d._harvest_due(t0 + timedelta(minutes=15))


def test_harvest_minutes_zero_turns_the_tick_off(tmp_path):
    assert not make_daemon(tmp_path, harvest_minutes=0)._harvest_due(datetime.now(timezone.utc))


def test_an_idle_tick_makes_no_connection(tmp_path, tv):
    d = settled(make_daemon(tmp_path), last_harvest=datetime.now(timezone.utc))
    d._tick_once()
    assert tv.events == []


def test_asleep_tv_is_left_alone(tmp_path, tv):
    tv.reachable = False
    make_daemon(tmp_path)._tick_once()
    assert tv.events == []


# --- behavior -----------------------------------------------------------------------------
def test_a_harvest_only_tick_records_the_edit_and_does_not_refresh(tmp_path, tv, monkeypatch):
    conn, dev, a = library(tmp_path)
    monkeypatch.setattr(scheduler, "refresh", lambda *x, **k: pytest.fail("refresh must not run"))
    tv.listing = [item("MY_F0001", "shadowbox_sage")]
    d = settled(make_daemon(tmp_path), last_harvest=datetime.now(timezone.utc) - timedelta(minutes=20))

    d._tick_once()

    assert conn.execute("SELECT matte FROM asset_policy WHERE asset_id = ?", (a,)).fetchone()[0] == "shadowbox_sage"
    assert tv.events == ["connect", "list"]
    assert not d._harvest_due(datetime.now(timezone.utc))           # and it won't hammer the TV


def test_a_due_refresh_counts_as_the_harvest(tmp_path, tv, monkeypatch):
    library(tmp_path)
    monkeypatch.setattr(scheduler, "refresh", lambda *x, **k: {"desired": 1})   # refresh harvests itself
    d = make_daemon(tmp_path)                                       # fresh daemon -> refresh is due

    d._tick_once()

    assert "list" not in tv.events                                  # no second, redundant listing
    assert d._last_harvest is not None and not d._harvest_due(datetime.now(timezone.utc))


def test_a_failing_harvest_backs_off_instead_of_retrying_every_tick(tmp_path, tv):
    library(tmp_path)
    tv.list_exc = FrameTimeout("art call exceeded 20s")
    d = settled(make_daemon(tmp_path), last_harvest=datetime.now(timezone.utc) - timedelta(minutes=20))

    d._tick_once()                                                  # must not raise

    assert not d._harvest_due(datetime.now(timezone.utc))           # next attempt is a full interval away


# --- fao harvest --------------------------------------------------------------------------
@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    conn, dev, a = library(tmp_path)
    fake = FakeTV(None)
    monkeypatch.setattr(cli, "_library", lambda args: (conn, {}, {}))
    monkeypatch.setattr(cli, "_client", lambda args: (fake, {}))
    monkeypatch.setattr(cli, "_ensure_device", lambda c, f: dev)
    FakeTV.listing, FakeTV.list_exc, FakeTV.events = [item("MY_F0001", "modern_seafoam")], None, []
    return conn, a


def test_fao_harvest_dry_run_reports_without_writing(cli_env, capsys):
    conn, a = cli_env
    assert cli.cmd_harvest(SimpleNamespace(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "would record 1 matte edit" in out and "none -> modern_seafoam" in out and "nothing was written" in out
    assert conn.execute("SELECT COUNT(*) FROM asset_policy").fetchone()[0] == 0


def test_fao_harvest_records_and_a_second_run_is_quiet(cli_env, capsys):
    conn, a = cli_env
    cli.cmd_harvest(SimpleNamespace(dry_run=False))
    assert "recorded 1 matte edit" in capsys.readouterr().out
    assert conn.execute("SELECT matte FROM asset_policy WHERE asset_id = ?", (a,)).fetchone()[0] == "modern_seafoam"
    cli.cmd_harvest(SimpleNamespace(dry_run=False))
    assert "no matte edits found" in capsys.readouterr().out


def test_the_cli_parser_knows_the_harvest_command():
    args = cli.build_parser().parse_args(["harvest", "--dry-run"])
    assert args.func is cli.cmd_harvest and args.dry_run is True
