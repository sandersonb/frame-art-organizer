"""Passive observations: reuse existing probes, never infer physical sleep from TCP.

Counters are per observation session. JSON records carry UTC timestamps even if
the surrounding service logger uses local time. Database/resource snapshots are
read-only and never make TV requests.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import math
import os
import resource
import sqlite3
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
OPERATIONS = {"device_info", "api_version", "artmode", "current", "list_my_photos",
              "upload_jpeg", "select", "delete", "set_slideshow"}


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def emit(logger, level, event, **fields):
    # All call sites supply fixed labels/aggregates, not request arguments,
    # exception strings, tokens, URLs or raw server responses.
    try:
        record = {"utc": utcnow(), "event": event, **fields}
        logger.log(level, "observe %s", json.dumps(record, sort_keys=True))
    except Exception:
        pass  # A diagnostic sink must never change the outcome of a TV operation.


def resources():
    out = {"pid": os.getpid(), "python_threads": threading.active_count()}
    try:
        status = Path("/proc/self/status")
        if status.is_file():
            values = dict(line.split(":", 1) for line in status.read_text().splitlines() if ":" in line)
            out["rss_kib"] = int(values["VmRSS"].split()[0])
            out["os_threads"] = int(values["Threads"].strip())
        else:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            out["peak_rss_kib"] = int(peak / 1024 if sys.platform == "darwin" else peak)
        fd_dir = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
        out["open_fds_observed"] = len(os.listdir(fd_dir))
    except (OSError, KeyError, ValueError):
        out["partial"] = True
    return out


def database_snapshot(path, host, since):
    if path is None:
        return {"available": False, "reason": "no_database_path"}
    conn = None
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True, timeout=.05)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")  # All counters refer to the same committed snapshot.
        out = {"available": True, "schema": conn.execute("PRAGMA user_version").fetchone()[0],
               "database_since_utc": since}
        where, args = ("dev.host=?", [host]) if host is not None else ("1=1", [])
        base = "FROM placement p JOIN device dev ON dev.id=p.device_id WHERE " + where
        out["placement_states"] = {r[0]: r[1] for r in conn.execute(
            "SELECT p.state,count(*) "+base+" GROUP BY p.state", args)}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(placement)")}
        counters = {
            "placement_attempts_since_start": "upload_attempted_at",
            "placements_recorded_since_start": "uploaded_at",  # Includes operator resolution, not just uploads.
            "uncertain_attempts_since_start": "upload_failed_at",
            "auto_recoveries_since_start": "upload_auto_recovered_at",
        }
        for name, column in counters.items():
            if column in cols:
                out[name] = conn.execute("SELECT count(*) "+base+f" AND p.{column}>=?", args+[since]).fetchone()[0]
        if "upload_outcome" in cols:
            unresolved = "(p.state='pending' OR p.upload_outcome='uncertain')"
            out["unresolved_total"] = conn.execute("SELECT count(*) "+base+" AND "+unresolved, args).fetchone()[0]
            if "upload_baseline" in cols:
                out["manual_review_total"] = conn.execute(
                    "SELECT count(*) "+base+" AND "+unresolved+
                    " AND (p.upload_baseline IS NULL OR p.upload_recovery_blocked=1)", args).fetchone()[0]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='refresh_batch'").fetchone():
            batch_base = "FROM refresh_batch b JOIN device dev ON dev.id=b.device_id WHERE "+where
            out["completed_batches_since_start"] = conn.execute(
                "SELECT count(*) "+batch_base+" AND b.state='complete' AND b.completed_at>=?", args+[since]).fetchone()[0]
            out["active_batches"] = []
            for b in conn.execute("SELECT b.* "+batch_base+" AND b.state='active'", args):
                desired = set(json.loads(b["desired"]))
                present = {r[0] for r in conn.execute(
                    "SELECT derivative_id FROM placement WHERE device_id=? AND state='present'", (b["device_id"],))}
                out["active_batches"].append({"batch_id": b["id"], "target_count": len(desired),
                    "target_present": len(desired & present), "pending_additions": len(desired-present),
                    "non_target_present": len(present-desired),
                    "selection_hash": hashlib.sha256(b["desired"].encode()).hexdigest()})
        return out
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        return {"available": False, "error": type(exc).__name__}
    finally:
        if conn is not None:
            conn.close()


class Observations:
    def __init__(self, host=None, *, tick_seconds=60, summary_seconds=3600, scope="daemon"):
        if not math.isfinite(summary_seconds) or summary_seconds <= 0:
            raise ValueError("observation_summary_seconds must be finite and positive")
        self.host, self.scope = host, scope
        self.session = uuid.uuid4().hex[:12]
        self.started = utcnow()
        self.database_since = datetime.fromisoformat(self.started).isoformat(timespec="seconds")
        self.started_mono = time.monotonic()
        self.summary_seconds = summary_seconds
        self.gap_seconds = max(1.0, tick_seconds * 1.5)
        self.next_summary = self.started_mono
        self.started_logged = False
        self.counts = Counter()
        self.durations = {}
        self.failures = Counter()
        self.workers = set()
        self.probes = {}
        self.state = None
        self.state_since = self.last_sample = None
        self.state_since_utc = self.last_sample_utc = None
        self.state_has_gap = True  # Initial interval is left-censored.
        self.paused = False
        self._lock = threading.RLock()

    def start(self):
        with self._lock:
            if self.started_logged:
                return
            self.started_logged = True
            try:
                version = importlib.metadata.version("samsungtvws")
            except importlib.metadata.PackageNotFoundError:
                version = "unknown"
            emit(log, logging.INFO, "observation.start", observation_id=self.session,
                 scope=self.scope, started_utc=self.started, python=sys.version.split()[0],
                 samsungtvws=version, pid=os.getpid(),
                 meaning="API reachability observations, not confirmed panel sleep")

    def sample(self, reachable, *, source="daemon-tick", probe_seconds=0):
        with self._lock:
            self.start()
            now, stamp = time.monotonic(), utcnow()
            self.counts["reachable_samples" if reachable else "unreachable_samples"] += 1
            self.counts["daemon_tick_samples" if source == "daemon-tick" else "operation_gate_samples"] += 1
            gap = now-self.last_sample if self.last_sample is not None else None
            had_gap = self.last_sample is not None and (self.paused or gap > self.gap_seconds)
            if had_gap:
                self.counts["sampling_gaps"] += 1
                self.state_has_gap = True
                emit(log, logging.INFO, "observation.sampling_gap", observation_id=self.session,
                     seconds=round(gap, 3), last_sample_utc=self.last_sample_utc,
                     continuity_unknown=True)
            self.paused = False
            if self.state is None or self.state != reachable:
                initial = self.state is None
                if not initial:
                    self.counts["observed_up_transitions" if reachable else "observed_down_transitions"] += 1
                emit(log, logging.INFO, "tv.reachability", observation_id=self.session,
                     reachable=reachable, initial=initial, source=source,
                     probe_seconds=round(probe_seconds, 3),
                     previous_first_observed_utc=self.state_since_utc,
                     previous_last_observed_utc=self.last_sample_utc,
                     previous_observation_span_seconds=round(self.last_sample-self.state_since, 3) if not initial else None,
                     transition_window_seconds=round(gap, 3) if gap is not None else None,
                     previous_interval_censored=self.state_has_gap,
                     continuous_reachability_proven=False, physical_sleep_confirmed=False)
                self.state, self.state_since, self.state_since_utc = reachable, now, stamp
                self.state_has_gap = initial or had_gap
            self.last_sample, self.last_sample_utc = now, stamp

    def pause_sampling(self, reason="cooldown"):
        with self._lock:
            if not self.paused:
                self.paused = True
                self.state_has_gap = True
                self.counts["sampling_pauses"] += 1
                emit(log, logging.INFO, "observation.sampling_paused", observation_id=self.session,
                     reason=reason, last_sample_utc=self.last_sample_utc)

    def note(self, kind, **fields):
        with self._lock:
            self.counts[kind] += 1
            emit(log, logging.INFO, "daemon."+kind, observation_id=self.session, **fields)

    def progress(self, stage, **fields):
        # This callback is passive: no SQL reads or TV calls on the hot path.
        with self._lock:
            pid = fields.get("pid")
            if stage == "worker.started" and isinstance(pid, int): self.workers.add(pid)
            if stage == "worker.stopped" and isinstance(pid, int): self.workers.discard(pid)
            client = fields.get("client_id", "default")
            if stage == "tcp.probe.begin": self.probes[client] = time.monotonic()
            if stage in {"tcp.probe.complete", "tcp.probe.unreachable"}:
                begun = self.probes.pop(client, time.monotonic())
                self.sample(stage == "tcp.probe.complete", source="operation-gate",
                            probe_seconds=time.monotonic()-begun)
            if stage in {"operation.complete", "operation.failed", "client.close.complete"}:
                self.probes.pop(client, None)
            if stage == "connection.retry": self.counts["connection_retries"] += 1
            if stage == "upload.submitted": self.counts["upload_submission_events"] += 1
            if stage == "upload.acknowledged": self.counts["upload_ack_events"] += 1
            operation = fields.get("operation")
            if operation not in OPERATIONS: return
            if stage == "operation.begin":
                self.counts["rpc_attempts"] += 1
                if operation == "upload_jpeg": self.counts["upload_rpc_attempts"] += 1
            if stage in {"operation.complete", "operation.failed"}:
                duration = float(fields.get("elapsed_seconds", 0))
                stats = self.durations.setdefault(operation, {"calls": 0, "failures": 0, "total_seconds": 0.0, "max_seconds": 0.0})
                stats["calls"] += 1
                stats["total_seconds"] += duration
                stats["max_seconds"] = max(stats["max_seconds"], duration)
                if stage == "operation.complete":
                    self.counts["rpc_successes"] += 1
                    if operation == "upload_jpeg":
                        self.counts["upload_successes"] += 1
                        if isinstance(fields.get("bytes"), int):
                            self.counts["uploaded_bytes"] += fields["bytes"]
                else:
                    stats["failures"] += 1
                    self.counts["rpc_failures"] += 1
                    # These labels come from our FrameClient, not exception messages.
                    phase = fields.get("phase", "unknown")
                    self.failures[phase] += 1
                    if fields.get("outcome") == "FrameUploadUncertain": self.counts["upload_uncertain_outcomes"] += 1
                    if (fields.get("outcome") == "FrameTimeout"
                            or "Timeout" in (fields.get("remote_cause") or "")
                            or fields.get("failure_reason") == "deadline"):
                        self.counts["rpc_timeout_outcomes"] += 1

    def report(self, database=None, *, force=False, reason="periodic"):
        with self._lock:
            now = time.monotonic()
            if not force and now < self.next_summary:
                return
            self.start()
            self.next_summary = now+self.summary_seconds
            emit(log, logging.INFO, "observation.summary", observation_id=self.session, scope=self.scope,
                 reason=reason, elapsed_seconds=round(now-self.started_mono, 3), counters=dict(self.counts),
                 operations={op: {k: round(v, 3) if isinstance(v, float) else v for k, v in stats.items()}
                             for op, stats in self.durations.items()}, failures_by_phase=dict(self.failures),
                 active_worker_pids=sorted(self.workers), last_reachable=self.state,
                 state_first_observed_utc=self.state_since_utc, last_sample_utc=self.last_sample_utc,
                 current_observation_span_seconds=round(self.last_sample-self.state_since, 3) if self.last_sample is not None else None,
                 interval_censored=self.state_has_gap, continuous_reachability_proven=False,
                 resources=resources(),
                 database=database_snapshot(database, self.host, self.database_since))
