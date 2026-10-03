"""M2 user-facing pieces: plan-report, schedule-refresh --limit, push guard, daemon render order,
web startup validation. No TV: fakes throughout."""
from types import SimpleNamespace

import pytest
from conftest import add_asset, add_derivative, add_device, item, resident
from PIL import Image
from test_daemon_and_cli import FakeTV, make_daemon, tv  # noqa: F401  (tv is a fixture)

from frame_art_organizer import cli, config, daemon as daemon_mod, scheduler, store


# --- fao plan-report -------------------------------------------------------------------------
@pytest.fixture
def library(conn):
    add_asset(conn, 1, name="dslr.jpg", width=4928, height=3264)            # 3:2  -> FILL
    add_asset(conn, 2, name="phone.jpg", width=1600, height=1200)           # 4:3  -> FIT
    add_asset(conn, 3, name="tiny.jpg", width=800, height=600)              # FIT, low-res
    portrait = add_asset(conn, 4, name="portrait.jpg", width=2000, height=3000)
    store.set_asset_matte(conn, portrait, "shadowbox_black", "odd"); conn.commit()   # chosen on the TV
    add_asset(conn, 5, name="gone.jpg", width=1920, height=1080, status="deleted")
    add_asset(conn, 6, name="broken.jpg", width=None, height=None)
    return conn


def _rows(conn, tol=0.16):
    return {r["name"]: r for r in cli._plan_rows(conn, config.image_settings({}), tol)}


def test_plan_rows_describe_what_the_v2_pipeline_would_do(library):
    rows = _rows(library)
    assert set(rows) == {"dslr.jpg", "phone.jpg", "tiny.jpg", "portrait.jpg"}   # deleted + unsized excluded
    assert (rows["dslr.jpg"]["kind"], rows["dslr.jpg"]["size"], rows["dslr.jpg"]["matte"]) == ("fill", (3840, 2160), "none")
    assert (rows["phone.jpg"]["kind"], rows["phone.jpg"]["size"], rows["phone.jpg"]["matte"]) == ("fit", (1600, 1200), "flexible_black")
    assert rows["tiny.jpg"]["low_res"] and not rows["phone.jpg"]["low_res"]
    assert (rows["portrait.jpg"]["matte"], rows["portrait.jpg"]["tv_choice"]) == ("shadowbox_black", True)


def test_the_tolerance_override_changes_the_split(library):
    assert _rows(library, 0.16)["dslr.jpg"]["kind"] == "fill"
    assert _rows(library, 0.15)["dslr.jpg"]["kind"] == "fit"                    # a literal 15 % mattes every 3:2


def test_plan_report_prints_a_reviewable_table_and_summary(library, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_library", lambda args: (library, {}, {}))
    assert cli.cmd_plan_report(SimpleNamespace(crop_tolerance=None)) == 0
    out = capsys.readouterr().out
    assert "4 active photo(s)  crop_tolerance=0.16" in out and "fit_matte=flexible_black" in out
    assert "FILL 1 · FIT 3 · low-res 1 · with a TV-chosen matte 1" in out
    assert "largest crop: 15.1%" in out and "TV-chosen matte" in out and "low-res" in out   # 4928x3264 = 1.5098:1


def test_plan_report_on_an_empty_library(conn, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_library", lambda args: (conn, {}, {}))
    assert cli.cmd_plan_report(SimpleNamespace(crop_tolerance=None)) == 0
    assert "no active photos" in capsys.readouterr().out


# --- fao schedule-refresh --limit ------------------------------------------------------------
def test_schedule_refresh_passes_the_limit_and_reports_pending_swaps(conn, monkeypatch, capsys):
    seen = {}

    def fake_refresh(fc, conn_, **kw):
        seen.update(kw)
        return {"desired": 3, "added": 1, "removed": 1, "errors": 0, "harvested": 0,
                "removals_skipped": False, "pending_adds": 2, "mattes_used": {"flexible_black": 1},
                "interval": 15, "shuffle": True}

    monkeypatch.setattr(scheduler, "refresh", fake_refresh)
    monkeypatch.setattr(cli, "_library", lambda args: (conn, {}, {}))
    monkeypatch.setattr(cli, "_client", lambda args: (FakeTV(None), {}))
    monkeypatch.setattr(cli, "_ensure_device", lambda c, f: 1)

    args = cli.build_parser().parse_args(["schedule-refresh", "--limit", "1"])
    assert args.limit == 1 and cli.cmd_schedule_refresh(args) == 0

    assert seen["limit"] == 1 and seen["matte_cfg"].fit_matte == "flexible_black"
    out = capsys.readouterr().out
    assert "2 more swap(s) pending" in out and "mattes on new uploads: {'flexible_black': 1}" in out


def test_schedule_refresh_without_a_limit_is_unlimited():
    assert cli.build_parser().parse_args(["schedule-refresh"]).limit is None


# --- fao push: the matte guard ---------------------------------------------------------------
class PushClient:
    def __init__(self):
        self.uploads = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def upload_jpeg(self, data, matte="none"):
        self.uploads.append(matte)
        return "MY_F0777"


@pytest.fixture
def push(tmp_path, monkeypatch):
    client = PushClient()
    monkeypatch.setattr(cli, "_client", lambda args: (client, {}))

    def run(w, h, **kw):
        path = tmp_path / f"{w}x{h}.jpg"
        Image.new("RGB", (w, h), (90, 100, 110)).save(path, "JPEG")
        args = SimpleNamespace(file=str(path), fit=kw.get("fit"), matte=kw.get("matte"),
                               force_matte=kw.get("force_matte", False), show=False)
        return cli.cmd_push(args), client.uploads

    return run


def test_push_refuses_a_crash_prone_matte_on_a_non_16_9_photo(push, capsys):
    code, uploads = push(600, 900, fit="auto", matte="modern_polar")      # the 2026-10-01 combination
    assert code == cli.EXIT_REFUSED and uploads == []                      # nothing reached the TV
    assert "can crash when it displays it" in capsys.readouterr().err


def test_push_allows_the_verified_types_and_the_override(push):
    assert push(600, 900, fit="auto", matte="shadowbox_black") == (0, ["shadowbox_black"])


def test_push_force_matte_overrides_the_guard(push):
    code, uploads = push(600, 900, fit="auto", matte="modern_polar", force_matte=True)
    assert (code, uploads) == (0, ["modern_polar"])


def test_push_defaults_come_from_matte_for(push):
    assert push(600, 900, fit="auto")[1] == ["flexible_black"]            # odd shape -> the fit matte
    assert push(1920, 1080, fit="auto")[1][-1] == "none"                  # 16:9 -> the default


def test_legacy_push_is_unchanged_and_unrestricted(push):
    # cover/contain always produce 3840x2160 (16:9), so any matte is as valid as it ever was.
    assert push(600, 900, fit="cover", matte="modern_polar") == (0, ["modern_polar"])
    assert push(600, 900, fit="cover")[1][-1] == "none"


# --- daemon: render before refresh -----------------------------------------------------------
def test_the_daemon_renders_pending_photos_before_it_connects_for_a_refresh(tmp_path, tv, monkeypatch):
    order = []
    monkeypatch.setattr(daemon_mod.ingest, "render_pending", lambda *a, **k: order.append("render") or 2)
    monkeypatch.setattr(scheduler, "refresh", lambda *a, **k: order.append("refresh") or {"desired": 1})
    add_device(daemon_mod.db.open_db(tmp_path / "state.db"))

    make_daemon(tmp_path)._tick_once()                                     # fresh daemon: a refresh is due

    assert order == ["render", "refresh"]
    assert tv.events == ["connect"]                                         # rendering didn't hold the TV connection


def test_a_render_failure_never_blocks_the_refresh(tmp_path, tv, monkeypatch):
    order = []

    def boom(*a, **k):
        raise OSError("disk error")

    monkeypatch.setattr(daemon_mod.ingest, "render_pending", boom)
    monkeypatch.setattr(scheduler, "refresh", lambda *a, **k: order.append("refresh") or {"desired": 1})
    make_daemon(tmp_path)._tick_once()
    assert order == ["refresh"]


def test_a_harvest_only_tick_does_not_render(tmp_path, tv, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from test_daemon_and_cli import settled
    monkeypatch.setattr(daemon_mod.ingest, "render_pending", lambda *a, **k: pytest.fail("must not render"))
    add_device(daemon_mod.db.open_db(tmp_path / "state.db"))
    d = settled(make_daemon(tmp_path), last_harvest=datetime.now(timezone.utc) - timedelta(minutes=20))
    d._tick_once()                                                          # harvest due, refresh not


# --- web: a bad [image] value stops the server at startup -------------------------------------
def _write_config(path, image_block=""):
    (path).write_text(
        '[frame]\nhost = "192.0.2.10"\n[paths]\ninbox = "in"\noriginals = "o"\n'
        'derivatives = "d"\ndatabase = "state.db"\n' + image_block, encoding="utf-8")
    return str(path)


def test_the_web_app_starts_with_a_valid_config(tmp_path):
    pytest.importorskip("fastapi")
    from frame_art_organizer import web
    assert web.create_app(_write_config(tmp_path / "config.toml"), run_daemon=False) is not None


def test_the_web_app_refuses_to_start_with_a_dangerous_fit_matte(tmp_path):
    pytest.importorskip("fastapi")
    from frame_art_organizer import web
    cfg = _write_config(tmp_path / "config.toml", '[image]\nfit_matte_type = "modern"\n')
    with pytest.raises(config.ConfigError, match="fit_matte_type"):
        web.create_app(cfg, run_daemon=False)
