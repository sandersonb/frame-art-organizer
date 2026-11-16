"""Passive reachability observations and summaries: no TV requests or database writes."""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from conftest import add_asset, add_derivative, add_device
from test_daemon_and_cli import make_daemon, settled

from frame_art_organizer import batches, daemon as daemon_mod, observability, store
from frame_art_organizer.observability import Observations, database_snapshot


def events(caplog, kind=None):
    rows = [json.loads(r.getMessage()[8:]) for r in caplog.records if r.getMessage().startswith("observe ")]
    return [r for r in rows if r["event"] == kind] if kind else rows


@pytest.fixture
def clock(monkeypatch):
    seconds = [0.0]
    epoch = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(observability.time, "monotonic", lambda: seconds[0])
    monkeypatch.setattr(observability, "utcnow", lambda: (epoch+timedelta(seconds=seconds[0])).isoformat())
    return seconds


def test_initial_state_is_not_counted_as_a_sleep_or_wake(clock, caplog):
    caplog.set_level(logging.INFO)
    obs = Observations()
    obs.sample(False)
    clock[0] = 60;obs.sample(False)
    clock[0] = 120;obs.sample(True)
    clock[0] = 180;obs.sample(True)
    clock[0] = 240;obs.sample(False)
    records = events(caplog, "tv.reachability")
    assert len(records) == 3 and records[0]["initial"]
    assert obs.counts["observed_up_transitions"] == obs.counts["observed_down_transitions"] == 1
    assert records[-1]["previous_observation_span_seconds"] == 60
    assert records[-1]["transition_window_seconds"] == 60
    assert all(not r["physical_sleep_confirmed"] and not r["continuous_reachability_proven"] for r in records)


def test_cooldown_gap_does_not_invent_unobserved_sleep_cycles(clock, caplog):
    caplog.set_level(logging.INFO)
    obs = Observations()
    obs.sample(True)
    clock[0] = 10;obs.pause_sampling()
    clock[0] = 20;obs.pause_sampling()
    clock[0] = 180;obs.sample(True)
    assert obs.counts["sampling_pauses"] == obs.counts["sampling_gaps"] == 1
    assert obs.counts["observed_up_transitions"] == obs.counts["observed_down_transitions"] == 0
    assert events(caplog, "observation.sampling_gap")[0]["continuity_unknown"]
    obs.report(force=True)
    assert events(caplog, "observation.summary")[-1]["interval_censored"]


def test_rpc_counts_distinguish_ack_events_from_valid_successes(clock):
    obs = Observations()
    obs.progress("worker.started", pid=123)
    obs.progress("operation.begin", operation="upload_jpeg")
    obs.progress("upload.submitted", operation="upload_jpeg")
    obs.progress("operation.failed", operation="upload_jpeg", elapsed_seconds=32,
                 outcome="FrameUploadUncertain", phase="upload.awaiting_ack",
                 remote_cause="WebSocketTimeoutException")
    obs.progress("operation.begin", operation="upload_jpeg")
    obs.progress("upload.submitted", operation="upload_jpeg")
    obs.progress("upload.acknowledged", operation="upload_jpeg")
    obs.progress("operation.complete", operation="upload_jpeg", elapsed_seconds=5, bytes=1000)
    obs.progress("worker.stopped", pid=123)
    assert not obs.workers
    assert obs.counts["upload_rpc_attempts"] == 2
    assert obs.counts["upload_successes"] == obs.counts["upload_ack_events"] == 1
    assert obs.counts["upload_uncertain_outcomes"] == obs.counts["rpc_timeout_outcomes"] == 1
    assert obs.counts["uploaded_bytes"] == 1000
    assert obs.failures["upload.awaiting_ack"] == 1
    assert obs.durations["upload_jpeg"]["total_seconds"] == 37


def test_reports_are_throttled_and_progress_does_not_collect_resources(clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    reads = []
    monkeypatch.setattr(observability, "resources", lambda: reads.append("resources") or {"pid": 1})
    monkeypatch.setattr(observability, "database_snapshot", lambda *a: reads.append("db") or {})
    obs = Observations(summary_seconds=3600)
    obs.report()
    clock[0] = 3599;obs.report()
    obs.progress("operation.begin", operation="api_version")
    obs.progress("operation.complete", operation="api_version", elapsed_seconds=1)
    assert reads == ["resources", "db"]
    clock[0] = 3600;obs.report()
    obs.report(force=True, reason="failure")
    assert len(events(caplog, "observation.summary")) == 3
    assert reads == ["resources", "db"] * 3


def test_readonly_database_counters_and_batch_progress(conn, tmp_path):
    dev = add_device(conn)
    derivative = add_derivative(conn, add_asset(conn, name="PRIVATE_PHOTO.jpg"))
    pid = store.create_pending_placement(conn, dev, derivative, "none", baseline=set())
    store.set_placement_uncertain(conn, pid)
    batches.start(conn, dev, "key", [{"derivative_id": derivative}])
    before = [tuple(r) for r in conn.execute("SELECT * FROM placement")]
    report = database_snapshot(tmp_path / "state.db", "192.0.2.10", "2000-01-01T00:00:00+00:00")
    assert report["available"] and report["schema"] == 6
    assert report["placement_attempts_since_start"] == report["uncertain_attempts_since_start"] == 1
    assert report["unresolved_total"] == 1 and report["manual_review_total"] == 0
    assert report["active_batches"][0]["pending_additions"] == 1
    assert [tuple(r) for r in conn.execute("SELECT * FROM placement")] == before
    assert "PRIVATE_PHOTO" not in str(report)


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing" / "state.db"
    assert not database_snapshot(path, None, "2000-01-01")["available"]
    assert not path.parent.exists()


def test_hourly_summaries_continue_in_cooldown_without_a_probe(tmp_path, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    d = make_daemon(tmp_path)
    d._retry_at = 7200
    monkeypatch.setattr(daemon_mod, "FrameClient", lambda *a: pytest.fail("no TV probes during cooldown"))
    d._tick_once()
    clock[0] = 3600;d._tick_once()
    assert len(events(caplog, "observation.summary")) == 2
    assert not (tmp_path / "state.db").exists()


def test_idle_logging_does_not_add_network_probes(tmp_path, clock, monkeypatch):
    probes = []
    class Client:
        def __init__(self, cfg): pass
        def is_reachable(self): probes.append(1);return True
    monkeypatch.setattr(daemon_mod, "FrameClient", Client)
    d = settled(make_daemon(tmp_path, harvest_minutes=0), last_harvest=datetime.now(timezone.utc))
    for n in range(3):
        clock[0] = n*60
        d._tick_once()
    assert len(probes) == 3
    assert d._observations.counts["daemon_tick_samples"] == 3
    assert d._observations.counts["operation_gate_samples"] == 0


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_summary_interval_must_be_positive_finite(value):
    with pytest.raises(ValueError): Observations(summary_seconds=value)
