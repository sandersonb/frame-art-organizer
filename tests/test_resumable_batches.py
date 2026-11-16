"""Interrupted rotations retain their selection and resume without waking the TV."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import add_asset, add_derivative, add_device, item, resident

from frame_art_organizer import batches, db, scheduler, store, upload_recovery
from frame_art_organizer.frame_client import FrameAsleep, FrameUploadUncertain
from frame_art_organizer.mattes import MatteConfig
from frame_art_organizer.upload_recovery import UploadRecoveryPending

PERIOD = {"name": "all-day", "collections": [], "interval": 15, "shuffle": True,
          "set_size": 3, "no_repeat_days": 30}


class TV:
    def __init__(self):
        self.items = {f"MY_F{n:04d}": item(f"MY_F{n:04d}") for n in (1, 2, 3)}
        self.counter = 100
        self.uploads = []
        self.deletes = []
        self.sleep_on_upload = None
        self.sleep_on_delete = False
        self.sleep_on_slideshow = False
        self.awake = True

    def gate(self):
        if not self.awake: raise FrameAsleep("API unreachable; panel may be asleep")
    def device_info(self):
        self.gate()
        return {"device": {"duid": "uuid:test-device"}}
    def list_my_photos(self):
        self.gate()
        return list(self.items.values())
    def upload_jpeg(self, data, **kw):
        self.gate()
        self.uploads.append(data)
        if len(self.uploads) == self.sleep_on_upload:
            self.awake = False
            raise FrameUploadUncertain("connection closed during upload")
        self.counter += 1
        cid = f"MY_F{self.counter:04d}"
        self.items[cid] = item(cid)
        return cid
    def delete(self, cid):
        self.gate()
        self.deletes.append(cid)
        self.items.pop(cid, None)  # FrameClient verifies absence if a previous delete lost its reply.
        if self.sleep_on_delete:
            self.sleep_on_delete, self.awake = False, False
            raise FrameAsleep("delete reply interrupted")
        return True
    def artmode(self):
        self.gate()
        return "off"
    def set_slideshow(self, **kw):
        self.gate()
        if self.sleep_on_slideshow:
            self.sleep_on_slideshow, self.awake = False, False
            raise FrameAsleep("slideshow reply interrupted")


@pytest.fixture
def world(conn, tmp_path):
    dev = add_device(conn)
    for n in (1, 2, 3): resident(conn, dev, n)
    for n in (4, 5, 6):
        asset = add_asset(conn, n)
        path = tmp_path / f"{n}.jpg"
        path.write_bytes(str(n).encode())
        add_derivative(conn, asset, path=str(path))
        store.set_policy(conn, asset, pinned=True)
    return dev, TV()


def refresh(conn, dev, tv, **kw):
    return scheduler.refresh(tv, conn, device_id=dev, period=PERIOD,
                             fit_mode="cover", pipeline_version=1, matte_cfg=MatteConfig(), **kw)


def test_chunks_freeze_selection_and_never_evict_before_all_additions(conn, world, monkeypatch):
    dev, tv = world
    first = refresh(conn, dev, tv, max_operations=2)
    assert first["added"] == 2 and first["removed"] == 0 and not first["complete"]
    selected = batches.active(conn, dev)["desired"]
    monkeypatch.setattr(store, "compose_working_set", lambda *a, **k: pytest.fail("must resume saved choice"))
    second = refresh(conn, dev, tv, max_operations=2)
    assert second["added"] == 1 and second["removed"] == 1
    assert batches.active(conn, dev)["desired"] == selected
    third = refresh(conn, dev, tv, max_operations=2)
    assert third["added"] == 0 and third["removed"] == 2 and third["complete"]
    assert len(tv.uploads) == len(tv.deletes) == 3
    assert batches.active(conn, dev) is None and len(tv.items) == 3


def test_sleep_during_upload_recovers_absence_then_resumes_same_batch(conn, world, monkeypatch):
    dev, tv = world
    clock = [datetime(2026, 10, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(store, "now", lambda: clock[0].isoformat())
    monkeypatch.setattr(upload_recovery, "utcnow", lambda: clock[0])
    tv.sleep_on_upload = 2
    with pytest.raises(FrameUploadUncertain): refresh(conn, dev, tv, max_operations=3)
    selected = batches.active(conn, dev)["desired"]
    assert len(tv.items) == 4 and tv.deletes == []
    assert store.unresolved_uploads(conn, dev)[0]["upload_baseline"] == json.dumps(sorted(tv.items))
    tv.awake = True
    clock[0] += timedelta(seconds=120)
    with pytest.raises(UploadRecoveryPending): refresh(conn, dev, tv, max_operations=3)
    clock[0] += timedelta(seconds=60)
    result = refresh(conn, dev, tv, max_operations=3)
    assert result["added"] == 2 and result["removed"] == 1
    assert batches.active(conn, dev)["desired"] == selected
    result = refresh(conn, dev, tv, max_operations=3)
    assert result["complete"] and not store.unresolved_uploads(conn, dev)
    assert len(tv.items) == 3
    assert conn.execute("SELECT count(*) FROM rotation_event WHERE event='added'").fetchone()[0] == 3


def test_database_restart_does_not_recompose_or_repeat_acknowledged_uploads(conn, world, tmp_path, monkeypatch):
    dev, tv = world
    first = refresh(conn, dev, tv, max_operations=1)
    batch_id = first["batch_id"]
    reopened = db.open_db(tmp_path / "state.db")
    monkeypatch.setattr(store, "compose_working_set", lambda *a, **k: pytest.fail("restart must retain selection"))
    try:
        for _ in range(5):
            if refresh(reopened, dev, tv, max_operations=2)["complete"]:
                break
            assert batches.active(reopened, dev)["id"] == batch_id
        else:
            pytest.fail("batch did not converge")
    finally: reopened.close()
    assert len(tv.uploads) == 3 and len(set(tv.uploads)) == 3


def test_sleep_during_delete_retries_only_remaining_work(conn, world):
    dev, tv = world
    tv.sleep_on_delete = True
    with pytest.raises(FrameAsleep): refresh(conn, dev, tv)
    assert len(tv.uploads) == 3 and batches.active(conn, dev) is not None
    tv.awake = True
    result = refresh(conn, dev, tv)
    assert result["complete"] and len(tv.uploads) == 3 and len(tv.items) == 3


def test_sleep_during_final_slideshow_does_not_repeat_uploads_or_deletes(conn, world):
    dev, tv = world
    tv.sleep_on_slideshow = True
    with pytest.raises(FrameAsleep): refresh(conn, dev, tv)
    assert len(tv.uploads) == len(tv.deletes) == 3
    tv.awake = True
    result = refresh(conn, dev, tv)
    assert result["complete"] and result["added"] == result["removed"] == 0
    assert len(tv.uploads) == len(tv.deletes) == 3


def test_chunk_completion_requires_fresh_inventory_not_just_acknowledgements(conn, world):
    from frame_art_organizer.frame_client import FrameConnectionError
    dev, tv = world
    original = tv.list_my_photos
    def incomplete():
        rows = original()
        return rows[:-1] if tv.deletes else rows
    tv.list_my_photos = incomplete
    with pytest.raises(FrameConnectionError): refresh(conn, dev, tv, max_operations=10)
    assert batches.active(conn, dev) is not None
    counts = len(tv.uploads), len(tv.deletes)
    tv.list_my_photos = original
    result = refresh(conn, dev, tv, max_operations=10)
    assert result["complete"] and (len(tv.uploads), len(tv.deletes)) == counts


def test_new_period_supersedes_old_selection_without_discarding_placements(conn, world):
    dev, tv = world
    first = refresh(conn, dev, tv, max_operations=1)
    new_period = {**PERIOD, "name": "evening"}
    result = scheduler.refresh(tv, conn, device_id=dev, period=new_period,
                               fit_mode="cover", pipeline_version=1, matte_cfg=MatteConfig(), max_operations=1)
    assert result["batch_id"] != first["batch_id"]
    assert conn.execute("SELECT state FROM refresh_batch WHERE id=?", (first["batch_id"],)).fetchone()[0] == "superseded"
    assert len(tv.uploads) == 2 and len(tv.items) == 5


def test_suppressed_asset_invalidates_saved_selection(conn, world):
    dev, tv = world
    first = refresh(conn, dev, tv, max_operations=1)
    rows = batches.desired_rows(conn, batches.active(conn, dev), PERIOD)
    missing = next(r for r in rows if r["derivative_id"] not in {p["derivative_id"] for p in store.present_placements(conn, dev)})
    store.set_policy(conn, missing["asset_id"], suppressed=True)
    result = refresh(conn, dev, tv, max_operations=1)
    assert result["batch_id"] != first["batch_id"]
    assert str(missing["asset_id"]).encode() not in tv.uploads


def test_elapsed_budget_stops_between_operations_not_during_an_upload(conn, world, monkeypatch):
    dev, tv = world
    clock = [0.0]
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock[0])
    upload = tv.upload_jpeg
    def slow(*args, **kw):
        result = upload(*args, **kw)
        clock[0] += 4
        return result
    tv.upload_jpeg = slow
    result = refresh(conn, dev, tv, max_operations=20, max_seconds=5)
    assert result["added"] == 2 and not result["complete"] and tv.deletes == []


def test_daemon_resumes_after_sleep_and_does_not_rerotate_on_restart(conn, world, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from frame_art_organizer import daemon as daemon_mod
    dev, tv = world
    clock = [datetime.now(timezone.utc)]
    mono = [0.0]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return clock[0]
    monkeypatch.setattr(daemon_mod, "datetime", Clock)
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(store, "now", lambda: clock[0].isoformat())
    monkeypatch.setattr(upload_recovery, "utcnow", lambda: clock[0])
    monkeypatch.setattr(daemon_mod.config, "active_period", lambda *a: PERIOD)
    tv.cfg = SimpleNamespace(host="192.0.2.10")
    monkeypatch.setattr(TV, "__enter__", lambda self: self, raising=False)
    monkeypatch.setattr(TV, "__exit__", lambda *a: None, raising=False)
    tv.is_reachable = lambda: tv.awake
    tv.api_version = lambda: "5.0.1.0"
    constructed = []
    monkeypatch.setattr(daemon_mod, "FrameClient", lambda cfg: constructed.append(1) or tv)
    cfg = {"frame": {"host": "192.0.2.10"}, "paths": {"database": "state.db"},
           "daemon": {"batch_operations": 3}, "schedule": {"refresh_hours": 24}}
    d = daemon_mod.Daemon(cfg, tmp_path)
    render_calls = []
    monkeypatch.setattr(d, "_render_pending", lambda c: render_calls.append(1))
    tv.sleep_on_upload = 2
    d._tick_once()
    assert d._last_refresh is None and d._retry_at == 120
    chosen = batches.active(conn, dev)["desired"]
    count = len(constructed)
    mono[0] = 60
    clock[0] += timedelta(seconds=60)
    d._tick_once()
    assert len(constructed) == count
    mono[0] = 120
    clock[0] += timedelta(seconds=60)
    d._tick_once()  # Still asleep: no API work and no escalating failure backoff.
    assert len(tv.uploads) == 2 and d._failure_count == 0
    tv.awake = True
    mono[0] = 180
    clock[0] += timedelta(seconds=60)
    d._tick_once()  # First stable inventory; wait, do not replay.
    assert len(tv.uploads) == 2 and d._retry_at == 240
    mono[0] = 240
    clock[0] += timedelta(seconds=60)
    d._tick_once()
    assert batches.active(conn, dev)["desired"] == chosen and d._last_refresh is None
    mono[0] = 300
    clock[0] += timedelta(seconds=60)
    d._tick_once()
    assert batches.active(conn, dev) is None and d._last_refresh is not None
    assert len(render_calls) == 1 and len(tv.items) == 3
    counts = len(tv.uploads), len(tv.deletes)
    restarted = daemon_mod.Daemon(cfg, tmp_path)
    restarted._tick_once()
    assert (len(tv.uploads), len(tv.deletes)) == counts
    assert restarted._last_refresh == d._last_refresh


def test_recovering_manual_sync_does_not_trigger_an_early_scheduled_rotation(conn, world, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from frame_art_organizer import daemon as daemon_mod
    dev, tv = world
    assert refresh(conn, dev, tv)["complete"]
    counts = len(tv.uploads), len(tv.deletes)
    clock = [datetime.now(timezone.utc)]
    mono = [0.0]
    monkeypatch.setattr(store, "now", lambda: clock[0].isoformat())
    monkeypatch.setattr(upload_recovery, "utcnow", lambda: clock[0])
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(daemon_mod.config, "active_period", lambda *a: PERIOD)
    tv.cfg = SimpleNamespace(host="192.0.2.10")
    monkeypatch.setattr(TV, "__enter__", lambda self: self, raising=False)
    monkeypatch.setattr(TV, "__exit__", lambda *a: None, raising=False)
    tv.is_reachable = lambda: True
    tv.api_version = lambda: "5.0.1.0"
    monkeypatch.setattr(daemon_mod, "FrameClient", lambda cfg: tv)
    derivative = add_derivative(conn, add_asset(conn, 7))
    pid = store.create_pending_placement(conn, dev, derivative, "none", baseline=set(tv.items))
    store.set_placement_uncertain(conn, pid)
    cfg = {"frame": {"host": "192.0.2.10"}, "paths": {"database": "state.db"},
           "schedule": {"refresh_hours": 24}}
    d = daemon_mod.Daemon(cfg, tmp_path)
    clock[0] += timedelta(seconds=120);mono[0] = 120
    d._tick_once()
    clock[0] += timedelta(seconds=60);mono[0] = 180
    d._tick_once()
    assert not store.unresolved_uploads(conn, dev)
    assert (len(tv.uploads), len(tv.deletes)) == counts
    assert conn.execute("SELECT count(*) FROM refresh_batch").fetchone()[0] == 1


def test_restart_does_not_restore_an_older_matching_period_over_a_newer_period(conn, world, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from frame_art_organizer import daemon as daemon_mod
    dev, tv = world
    assert refresh(conn, dev, tv)["complete"]
    other = batches.start(conn, dev, batches.key({**PERIOD, "name": "other"}, "cover", 1), [])
    batches.finish(conn, other["id"])
    monkeypatch.setattr(daemon_mod.config, "active_period", lambda *a: PERIOD)
    tv.cfg = SimpleNamespace(host="192.0.2.10")
    monkeypatch.setattr(TV, "__enter__", lambda self: self, raising=False)
    monkeypatch.setattr(TV, "__exit__", lambda *a: None, raising=False)
    tv.is_reachable = lambda: True
    tv.api_version = lambda: "5.0.1.0"
    monkeypatch.setattr(daemon_mod, "FrameClient", lambda cfg: tv)
    calls = []
    monkeypatch.setattr(daemon_mod.scheduler, "refresh", lambda *a, **k: calls.append(1) or {"errors": 0})
    cfg = {"frame": {"host": "192.0.2.10"}, "paths": {"database": "state.db"}}
    daemon_mod.Daemon(cfg, tmp_path)._tick_once()
    assert calls == [1]
