"""M3 CLI tooling: schedule-refresh renders first; schedule-show reports migration progress."""
from types import SimpleNamespace

from conftest import add_asset, add_derivative, add_device, add_placement, resident
from test_daemon_and_cli import FakeTV

from frame_art_organizer import cli, scheduler

V2 = {"image": {"default_fit": "auto", "pipeline_version": 2}}


def test_schedule_refresh_renders_pending_photos_before_it_touches_the_tv(conn, monkeypatch, capsys):
    events = []
    FakeTV.events = events
    monkeypatch.setattr(cli.ingest, "render_pending", lambda *a, **k: events.append("render") or 2)
    monkeypatch.setattr(scheduler, "refresh", lambda *a, **k: events.append("refresh") or {
        "desired": 1, "added": 0, "removed": 0, "errors": 0, "harvested": 0, "removals_skipped": False,
        "pending_adds": 0, "mattes_used": {}, "interval": 15, "shuffle": True})
    monkeypatch.setattr(cli, "_library", lambda args: (conn, {"derivatives": None}, V2))
    monkeypatch.setattr(cli, "_client", lambda args: (FakeTV(None), V2))
    monkeypatch.setattr(cli, "_ensure_device", lambda c, f: 1)

    assert cli.cmd_schedule_refresh(SimpleNamespace(limit=None)) == 0

    assert events == ["render", "connect", "refresh"]                # CPU/disk work first, TV connection after
    assert "rendered 2 pending derivative(s) for the current pipeline" in capsys.readouterr().out


def test_schedule_show_reports_how_far_the_migration_has_got(conn, monkeypatch, capsys):
    dev = add_device(conn)
    resident(conn, dev, 1)                                           # on the old pipeline (cover v1)
    resident(conn, dev, 2)
    a3 = add_asset(conn, 3)                                          # already on the new one (auto v2)
    add_placement(conn, dev, add_derivative(conn, a3, fit="auto", pv=2), "MY_F0003")
    monkeypatch.setattr(cli, "_library", lambda args: (conn, {}, V2))

    assert cli.cmd_schedule_show(SimpleNamespace()) == 0

    assert "pipeline      : auto v2 — resident on it: 1/3, 2 stale (a refresh swaps them)" in capsys.readouterr().out


def test_schedule_show_is_quiet_about_staleness_when_fully_migrated(conn, monkeypatch, capsys):
    dev = add_device(conn)
    a = add_asset(conn, 1)
    add_placement(conn, dev, add_derivative(conn, a, fit="auto", pv=2), "MY_F0001")
    monkeypatch.setattr(cli, "_library", lambda args: (conn, {}, V2))
    cli.cmd_schedule_show(SimpleNamespace())
    out = capsys.readouterr().out
    assert "resident on it: 1/1" in out and "stale" not in out
