"""Never infer sleep or image identity from a lost reply. Recover verified absence only."""
from datetime import datetime, timedelta, timezone

import pytest
from conftest import add_asset, add_derivative, add_device, item, resident

from frame_art_organizer import db, store, upload_recovery
from frame_art_organizer.frame_client import FrameTimeout
from frame_art_organizer.upload_recovery import UploadRecoveryPending, recover


class TV:
    def __init__(self):
        self.items = [item("MY_F0001")]
        self.calls = []
        self.duid = "uuid:test-device"
        self.error = None

    def device_info(self):
        self.calls.append("info")
        return {"device": {"duid": self.duid}}

    def list_my_photos(self):
        self.calls.append("list")
        if self.error: raise self.error
        return self.items


@pytest.fixture
def attempt(conn, monkeypatch):
    clock = [datetime(2026, 10, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(store, "now", lambda: clock[0].isoformat())
    monkeypatch.setattr(upload_recovery, "utcnow", lambda: clock[0])
    dev = add_device(conn)
    resident(conn, dev, 1)
    derivative = add_derivative(conn, add_asset(conn, 2))
    pid = store.create_pending_placement(conn, dev, derivative, "none", baseline={"MY_F0001"})
    store.set_placement_uncertain(conn, pid)
    return dev, pid, clock, TV()


def advance(clock, seconds): clock[0] += timedelta(seconds=seconds)


def test_settlement_period_has_no_network_or_replay(conn, attempt):
    dev, pid, clock, tv = attempt
    with pytest.raises(UploadRecoveryPending) as exc: recover(tv, conn, dev)
    assert exc.value.retry_after == 120 and tv.calls == []
    assert store.unresolved_uploads(conn, dev)[0]["id"] == pid


def test_two_fresh_unchanged_inventories_are_required(conn, attempt):
    dev, pid, clock, tv = attempt
    advance(clock, 120)
    with pytest.raises(UploadRecoveryPending): recover(tv, conn, dev)
    assert tv.calls == ["info", "list"]
    advance(clock, 59)
    with pytest.raises(UploadRecoveryPending): recover(tv, conn, dev)
    assert len(tv.calls) == 2
    advance(clock, 1)
    recover(tv, conn, dev)
    assert tv.calls == ["info", "list", "info", "list"]
    assert not store.unresolved_uploads(conn, dev)
    assert conn.execute("SELECT state,upload_outcome FROM placement WHERE id=?", (pid,)).fetchone()[:] == ("error", "not_uploaded")


@pytest.mark.parametrize("items", [[item("MY_F0001"), item("MY_F0002")], []])
def test_any_inventory_change_requires_manual_review(conn, attempt, items):
    dev, pid, clock, tv = attempt
    advance(clock, 120)
    tv.items = items
    with pytest.raises(store.UploadResolutionRequired): recover(tv, conn, dev)
    assert store.unresolved_uploads(conn, dev)[0]["upload_recovery_blocked"] == 1
    calls = len(tv.calls)
    tv.items = [item("MY_F0001")]
    with pytest.raises(store.UploadResolutionRequired): recover(tv, conn, dev)
    assert len(tv.calls) == calls  # No automatic adoption even if a candidate's metadata matches.


def test_change_between_confirmation_reads_cannot_trigger_retry(conn, attempt):
    dev, _, clock, tv = attempt
    advance(clock, 120)
    with pytest.raises(UploadRecoveryPending): recover(tv, conn, dev)
    advance(clock, 60)
    tv.items.append(item("MY_F0002"))
    with pytest.raises(store.UploadResolutionRequired): recover(tv, conn, dev)


def test_sleep_during_recovery_resets_confirmation_window(conn, attempt):
    dev, _, clock, tv = attempt
    advance(clock, 120)
    with pytest.raises(UploadRecoveryPending): recover(tv, conn, dev)
    advance(clock, 60)
    tv.error = FrameTimeout("asleep or unavailable")
    with pytest.raises(FrameTimeout): recover(tv, conn, dev)
    assert store.unresolved_uploads(conn, dev)[0]["upload_absence_seen_at"] is None
    tv.error = None
    with pytest.raises(UploadRecoveryPending): recover(tv, conn, dev)


def test_recovery_state_survives_database_reopen(conn, attempt, tmp_path):
    dev, _, clock, tv = attempt
    advance(clock, 120)
    with pytest.raises(UploadRecoveryPending): recover(tv, conn, dev)
    advance(clock, 60)
    reopened = db.open_db(tmp_path / "state.db")
    try:
        recover(tv, reopened, dev)
        assert not store.unresolved_uploads(reopened, dev)
    finally: reopened.close()


@pytest.mark.parametrize("duid", [None, "uuid:other-tv"])
def test_changed_or_unknown_identity_is_never_recovered(conn, attempt, duid):
    dev, _, clock, tv = attempt
    advance(clock, 120)
    tv.duid = duid
    with pytest.raises(store.UploadResolutionRequired): recover(tv, conn, dev)


def test_legacy_records_need_manual_review(conn, attempt):
    dev, pid, clock, tv = attempt
    conn.execute("UPDATE placement SET upload_baseline=NULL WHERE id=?", (pid,))
    conn.commit()
    advance(clock, 3600)
    with pytest.raises(store.UploadResolutionRequired): recover(tv, conn, dev)
    assert tv.calls == []


def test_crashed_inflight_attempt_starts_new_settlement_not_immediate_replay(conn, attempt):
    dev, pid, clock, tv = attempt
    conn.execute("UPDATE placement SET upload_outcome='in_flight',upload_failed_at=NULL WHERE id=?", (pid,))
    conn.commit()
    advance(clock, 3600)
    with pytest.raises(UploadRecoveryPending) as exc: recover(tv, conn, dev)
    assert exc.value.retry_after == 120 and tv.calls == []
    assert store.unresolved_uploads(conn, dev)[0]["upload_outcome"] == "uncertain"


def test_operator_resolution_is_not_overwritten_by_stale_recovery(conn, attempt):
    dev, pid, clock, tv = attempt
    advance(clock, 120)
    with pytest.raises(UploadRecoveryPending): recover(tv, conn, dev)
    advance(clock, 60)
    original = tv.list_my_photos

    def resolved_during_read():
        store.set_placement_present(conn, pid, "MY_F0099")
        return original()

    tv.list_my_photos = resolved_during_read
    recover(tv, conn, dev)
    assert conn.execute("SELECT content_id,state FROM placement WHERE id=?", (pid,)).fetchone()[:] == ("MY_F0099", "present")


def test_repeated_interruptions_are_capped_not_retried_forever(conn, attempt):
    dev, pid, clock, tv = attempt
    derivative = store.unresolved_uploads(conn, dev)[0]["derivative_id"]
    for _ in range(2):
        advance(clock, 120)
        with pytest.raises(UploadRecoveryPending): recover(tv, conn, dev)
        advance(clock, 60)
        recover(tv, conn, dev)
        pid = store.create_pending_placement(conn, dev, derivative, "none", baseline={"MY_F0001"})
        store.set_placement_uncertain(conn, pid)
    calls = len(tv.calls)
    with pytest.raises(store.UploadResolutionRequired): recover(tv, conn, dev)
    assert len(tv.calls) == calls
    assert store.unresolved_uploads(conn, dev)[0]["upload_recovery_blocked"] == 1
