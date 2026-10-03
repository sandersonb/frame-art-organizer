"""Purging superseded derivatives (SPEC.md §12.8, M4). Real files on disk, real DB."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from frame_art_organizer import cli, config, images, ingest, purge, scheduler, store

SOURCES = {"A": (1920, 1080), "B": (1500, 1000), "C": (1200, 900)}
V2_CFG = {"image": {"default_fit": "auto", "pipeline_version": 2}}
PERIOD = {"name": "all-day", "collections": [], "interval": 15, "shuffle": True, "set_size": 3, "no_repeat_days": 30}


class World:
    def __init__(self, conn, tmp_path):
        self.conn, self.tmp = conn, tmp_path
        self.derivs = tmp_path / "derivs"
        self.paths = {"derivatives": self.derivs}
        self.thumbs = config.thumbs_dir(self.paths)
        self.dev = store.ensure_device(conn, name="Frame", host="192.0.2.10", duid="uuid:purge")
        for n, (name, size) in enumerate(SOURCES.items(), start=1):
            p = tmp_path / f"{name}.jpg"
            Image.new("RGB", size, (30 * n, 90, 160)).save(p, "JPEG")
            store.insert_asset(conn, sha256=f"{n:064x}", original_path=str(p), original_name=p.name,
                               bytes_=1, mime="image/jpeg", width=size[0], height=size[1], captured_at=None)
        ingest.render_pending(conn, self.paths, "cover", 1, 90)
        ingest.render_pending(conn, self.paths, "auto", 2, 90, crop_tolerance=0.16)
        self.age_v2(20)
        for a in self.assets():                       # resident on v2; the v1 copies are history
            self.place(a, 2, "present")
            self.place(a, 1, "deleted_on_device")

    def assets(self):
        return [a["id"] for a in store.list_assets(self.conn)]

    def deriv(self, asset, pv):
        return store.get_any_derivative(self.conn, asset, "auto" if pv == 2 else "cover", pv)

    def place(self, asset, pv, state):
        d = self.deriv(asset, pv)
        n = self.conn.execute("SELECT COUNT(*) FROM placement").fetchone()[0] + 1
        pid = store.create_pending_placement(self.conn, self.dev, d["id"], "none")
        if state != "pending":
            store.set_placement_present(self.conn, pid, f"MY_F{n:04d}")
        if state == "deleted_on_device":
            store.set_placement_deleted(self.conn, pid)
        return pid

    def age_v2(self, days):
        when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE derivative SET rendered_at = ? WHERE pipeline_version = 2", (when,))
        self.conn.commit()

    def plan(self, **kw):
        return purge.build_plan(self.conn, self.derivs, "auto", 2, kw.pop("min_age_days", 14), **kw)

    def files(self, pv):
        return [Path(d["path"]) for a in self.assets() if (d := self.deriv(a, pv))]


@pytest.fixture
def w(conn, tmp_path):
    return World(conn, tmp_path)


def rows(conn, pv):
    return conn.execute("SELECT COUNT(*) FROM derivative WHERE pipeline_version = ?", (pv,)).fetchone()[0]


# --- what is purged ------------------------------------------------------------------------------
def test_the_old_renders_are_the_candidates_and_the_current_ones_never_are(w):
    plan = w.plan()
    assert sorted(c["path"] for c in plan.eligible) == sorted(str(p) for p in w.files(1))
    assert plan.bytes == sum(p.stat().st_size for p in w.files(1)) > 0
    assert (plan.too_new, plan.replacement_missing, plan.outside_dir, plan.stale_resident) == (0, 0, 0, 0)


def test_purging_removes_the_old_rows_files_and_history_and_nothing_else(w):
    originals = [Path(a["original_path"]) for a in store.list_assets(w.conn)]
    old_files, new_files = w.files(1), w.files(2)
    res = purge.apply_plan(w.conn, w.plan())

    assert res["derivatives"] == 3 and res["history_rows"] == 3 and res["freed_bytes"] > 0 and not res["file_errors"]
    assert rows(w.conn, 1) == 0 and rows(w.conn, 2) == 3
    assert not any(p.exists() for p in old_files) and all(p.exists() for p in new_files)
    assert all(p.exists() for p in originals)                                  # originals are never touched
    assert len(store.present_placements(w.conn, w.dev)) == 3                    # what is on the TV is untouched
    assert w.conn.execute("SELECT COUNT(*) FROM placement WHERE state = 'deleted_on_device'").fetchone()[0] == 0


def test_a_second_purge_finds_nothing(w):
    purge.apply_plan(w.conn, w.plan())
    assert w.plan().eligible == []


def test_the_rotation_history_survives(w):
    store.add_rotation_event(w.conn, w.dev, w.assets()[0], "removed", "test")
    w.conn.commit()
    purge.apply_plan(w.conn, w.plan())
    assert w.conn.execute("SELECT COUNT(*) FROM rotation_event").fetchone()[0] == 1


# --- the guards ------------------------------------------------------------------------------------
def test_the_soak_keeps_renders_whose_replacement_is_still_young(w):
    w.age_v2(3)
    plan = w.plan(min_age_days=14)
    assert plan.eligible == [] and plan.too_new == 3
    assert len(w.plan(min_age_days=3).eligible) == 3                            # old enough exactly at N days
    assert len(w.plan(min_age_days=0).eligible) == 3


def test_a_timestamp_without_a_timezone_is_read_as_utc_not_a_crash(w):
    w.conn.execute("UPDATE derivative SET rendered_at = '2000-01-01 00:00:00' WHERE pipeline_version = 2")
    w.conn.execute("UPDATE derivative SET rendered_at = datetime('now') WHERE id = ?", (w.deriv(w.assets()[0], 2)["id"],))
    w.conn.commit()                                                             # sqlite-style, no tz: one old, one just now
    plan = w.plan()
    assert len(plan.eligible) == 2 and plan.too_new == 1


def test_nothing_is_purged_while_the_tv_still_holds_an_old_render(w):
    a = w.assets()[0]
    w.conn.execute("UPDATE placement SET state = 'deleted_on_device' WHERE derivative_id = ?", (w.deriv(a, 2)["id"],))
    w.place(a, 1, "present")                                                    # this photo is still on the OLD pipeline
    plan = w.plan()
    assert plan.stale_resident == 1
    with pytest.raises(RuntimeError, match="old pipeline"):
        purge.apply_plan(w.conn, plan)
    assert rows(w.conn, 1) == 3 and all(p.exists() for p in w.files(1))


def test_a_render_about_to_be_uploaded_counts_as_in_use(w):
    a = w.assets()[0]
    w.place(a, 1, "pending")
    assert w.plan().stale_resident == 1
    assert w.deriv(a, 1)["id"] not in {c["id"] for c in w.plan().eligible}


def test_a_render_with_no_replacement_is_never_purged(w):
    w.conn.execute("DELETE FROM placement WHERE derivative_id IN (SELECT id FROM derivative WHERE asset_id = ? AND pipeline_version = 2)",
                   (w.assets()[0],))
    w.conn.execute("DELETE FROM derivative WHERE asset_id = ? AND pipeline_version = 2", (w.assets()[0],))
    w.conn.commit()
    plan = w.plan()
    assert len(plan.eligible) == 2                                              # the other two photos only
    keep = w.deriv(w.assets()[0], 1)
    assert keep["id"] not in {c["id"] for c in plan.eligible}


def test_a_replacement_that_is_missing_from_disk_keeps_the_old_render(w):
    victim = w.assets()[1]
    Path(w.deriv(victim, 2)["path"]).unlink()
    plan = w.plan()
    assert plan.replacement_missing == 1 and len(plan.eligible) == 2
    purge.apply_plan(w.conn, plan)
    assert Path(w.deriv(victim, 1)["path"]).exists()                            # the only good copy of that photo survives


def test_a_path_outside_the_derivatives_directory_is_never_deleted(w):
    stray = w.tmp / "elsewhere" / "precious.jpg"
    stray.parent.mkdir()
    stray.write_bytes(b"do not delete")
    d = w.deriv(w.assets()[0], 1)
    w.conn.execute("UPDATE derivative SET path = ? WHERE id = ?", (str(stray), d["id"]))
    w.conn.commit()
    plan = w.plan()
    assert plan.outside_dir == 1 and len(plan.eligible) == 2
    purge.apply_plan(w.conn, plan)
    assert stray.read_bytes() == b"do not delete"


def test_after_a_rollback_the_newer_renders_are_kept(conn, tmp_path):
    """Configured back to cover v1: the v2 renders are the ones you rolled back FROM — not purged."""
    w = World(conn, tmp_path)
    for a in w.assets():                                                        # the TV is back on v1
        w.conn.execute("UPDATE placement SET state = 'deleted_on_device' WHERE derivative_id = ?", (w.deriv(a, 2)["id"],))
        w.conn.execute("UPDATE placement SET state = 'present' WHERE derivative_id = ? AND content_id IS NOT NULL", (w.deriv(a, 1)["id"],))
    w.conn.commit()
    plan = purge.build_plan(conn, w.derivs, "cover", 1, 0)
    assert plan.eligible == [] and plan.newer_kept == 3
    assert rows(conn, 2) == 3


def test_a_failed_database_step_deletes_no_file(w, monkeypatch):
    real = store.purge_derivative
    calls = []

    def flaky(conn, did):
        calls.append(did)
        if len(calls) == 2:
            raise ValueError("boom")
        return real(conn, did)

    monkeypatch.setattr(store, "purge_derivative", flaky)
    old = w.files(1)
    with pytest.raises(ValueError):
        purge.apply_plan(w.conn, w.plan())
    assert rows(w.conn, 1) == 3 and all(p.exists() for p in old)                # all or nothing


def test_the_store_refuses_to_delete_a_derivative_that_is_in_use(w):
    with pytest.raises(ValueError, match="in use"):
        store.purge_derivative(w.conn, w.deriv(w.assets()[0], 2)["id"])         # resident on the TV


# --- rollback still works after a purge --------------------------------------------------------------
def test_a_rollback_after_a_purge_re_renders_from_the_originals(w):
    purge.apply_plan(w.conn, w.plan())
    assert rows(w.conn, 1) == 0

    assert ingest.render_pending(w.conn, w.paths, "cover", 1, 90) == 3          # rebuilt from the archived originals
    assert all(p.exists() for p in w.files(1))
    _, to_add, to_remove, _ = scheduler.plan(w.conn, device_id=w.dev, period=PERIOD,
                                             fit_mode="cover", pipeline_version=1)
    assert (len(to_add), len(to_remove)) == (3, 3)                              # and the swap back is planned as usual


# --- thumbnails --------------------------------------------------------------------------------------
def test_unused_thumbnails_are_found_and_live_ones_kept(w):
    w.thumbs.mkdir()
    a = store.list_assets(w.conn)[0]
    live = w.thumbs / images.thumb_name(a["sha256"], w.deriv(a["id"], 2)["id"])
    fallback = w.thumbs / images.thumb_name(a["sha256"], None)
    preview_of_old = w.thumbs / images.thumb_name(a["sha256"], w.deriv(a["id"], 1)["id"])
    legacy = w.thumbs / f"{a['sha256']}.jpg"                                    # the pre-M4 cache name
    other = w.thumbs / "notes.txt"
    for p in (live, fallback, preview_of_old, legacy, other):
        p.write_bytes(b"x")

    assert purge.thumb_orphans(w.conn, w.thumbs) == [legacy]                    # v1 deriv still exists -> its preview too
    purge.apply_plan(w.conn, w.plan())
    assert sorted(purge.thumb_orphans(w.conn, w.thumbs)) == sorted([legacy, preview_of_old])
    assert other.exists() and live.exists()                                     # only *.jpg are ever considered


def test_no_thumbnail_directory_is_fine(w):
    assert purge.thumb_orphans(w.conn, w.tmp / "nope") == []


# --- the command -------------------------------------------------------------------------------------
@pytest.fixture
def command(w, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_library", lambda args: (w.conn, w.paths, V2_CFG))

    def run(apply=False, min_age_days=14):
        code = cli.cmd_purge_derivatives(SimpleNamespace(apply=apply, min_age_days=min_age_days))
        return code, capsys.readouterr().out
    return run


def test_the_command_is_a_dry_run_unless_told_otherwise(w, command):
    code, out = command()
    assert code == 0 and "superseded renders to delete : 3" in out and "dry run — nothing deleted" in out
    assert rows(w.conn, 1) == 3 and all(p.exists() for p in w.files(1))


def test_the_command_applies_when_asked_and_a_rerun_is_quiet(w, command):
    code, out = command(apply=True)
    assert code == 0 and "deleted 3 render(s)" in out
    assert rows(w.conn, 1) == 0
    assert "superseded renders to delete : 0" in command()[1]


def test_the_command_refuses_mid_migration_even_with_apply(w, command):
    w.place(w.assets()[0], 1, "present")
    code, out = command(apply=True)
    assert code == cli.EXIT_REFUSED and "REFUSED" in out and "migration is not finished" in out
    assert rows(w.conn, 1) == 3


def test_the_command_explains_what_it_keeps(w, command):
    w.age_v2(2)
    code, out = command()
    assert code == 0 and "superseded renders to delete : 0" in out and "kept" in out and "younger than 14 day(s)" in out


def test_the_command_sweeps_unused_thumbnails_only_when_applying(w, command):
    w.thumbs.mkdir()
    legacy = w.thumbs / f"{'0' * 63}1.jpg"
    legacy.write_bytes(b"x" * 100)
    assert "unused thumbnails to delete  : 1" in command()[1] and legacy.exists()
    command(apply=True)
    assert not legacy.exists()
