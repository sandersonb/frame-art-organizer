"""The always-on scheduler daemon.

A single loop, safe to run in a background thread alongside the web server:

  every tick (default 60s):
    - probe reachability (fast TCP). Asleep → do nothing (can't, and mustn't, wake it).
    - awake → decide if a refresh is DUE:
        * the active schedule period changed (incl. a boundary that passed while asleep), OR
        * the working set is stale (older than schedule.refresh_hours) — rotates photos.
      if due  → scheduler.refresh() (compose set → reconcile MY-C0002 → set slideshow).
      elif we just woke → re-assert the slideshow config (cheap insurance), no recompose.

Refresh is a deliberate rotation, not something that fires on every wake — so a person
walking in doesn't churn the whole library. All Frame calls are reachability-gated and
timeout-bounded by FrameClient (FrameAsleep/FrameTimeout → skip, retry next tick).
"""
from __future__ import annotations

import html
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from . import batches, config, db, ingest, scheduler, store, upload_recovery
from . import harvest as harvest_mod
from .frame_client import FrameAsleep, FrameClient, FrameUploadUncertain
from .device_lock import DeviceBusy
from .observability import Observations
from .daemon_diagnostics import DaemonDiagnostics, ERRORS
from .stack_capture import exception_stacks

log = logging.getLogger("frame_art_organizer.daemon")


class Daemon:
    def __init__(self, cfg: dict, base_dir, *, tick: float | None = None):
        self.cfg = cfg
        self.base = base_dir
        self.paths = config.paths(cfg, base_dir)
        dcfg = cfg.get("daemon", {})
        self.tick = float(tick if tick is not None else dcfg.get("tick_seconds", 60))
        # How often (while the TV is awake) to read back matte edits made on the TV; 0 = off.
        self.harvest_interval = timedelta(minutes=float(dcfg.get("harvest_minutes", 15)))
        img = config.image_settings(cfg)   # validated: a bad [image] value fails at startup
        self.fit = img.default_fit
        self.pv = img.pipeline_version
        self.matte_cfg = img.matte
        self.quality = img.jpeg_quality
        self.tolerance = img.crop_tolerance
        self.refresh_interval = timedelta(
            hours=float(cfg.get("schedule", {}).get("refresh_hours", 24))
        )
        self._stop = threading.Event()
        self._applied_period: str | None = None
        self._last_refresh: datetime | None = None
        self._last_harvest: datetime | None = None
        self._was_reachable: bool | None = None
        self._wake_pending = False
        self._failure_count = 0
        self._retry_at = 0.0
        self.retry_initial = float(dcfg.get("retry_initial_seconds", 300))
        self.retry_max = float(dcfg.get("retry_max_seconds", 1800))
        if (not math.isfinite(self.retry_initial) or not math.isfinite(self.retry_max)
                or self.retry_initial <= 0 or self.retry_max < self.retry_initial):
            raise ValueError("[daemon] retry delays must be finite, positive, and max >= initial")
        self.batch_operations = dcfg.get("batch_operations", 3)
        self.batch_seconds = float(dcfg.get("batch_seconds", 30))
        if (not isinstance(self.batch_operations, int) or isinstance(self.batch_operations, bool)
                or self.batch_operations < 1 or not math.isfinite(self.batch_seconds) or self.batch_seconds <= 0):
            raise ValueError("[daemon] batch_operations must be a positive integer and batch_seconds finite and positive")
        self._observations = Observations(
            config.frame_config(cfg, base_dir).host, tick_seconds=self.tick,
            summary_seconds=float(dcfg.get("observation_summary_seconds", 3600)),
        )
        self._diagnostics = DaemonDiagnostics(
            cfg, base_dir, self.paths["database"], config.frame_config(cfg, base_dir).host,
        )

    def _progress(self, stage, **fields):
        self._observations.progress(stage, **fields)
        self._diagnostics.progress(stage, **fields)

    def _back_off(self, reason):
        self._failure_count += 1
        delay = min(self.retry_max, self.retry_initial * 2 ** min(self._failure_count - 1, 20))
        self._retry_at = time.monotonic() + delay
        log.warning("TV work paused for %ss (%s; consecutive failures=%s)", delay, reason, self._failure_count)
        self._observations.note("backoff", reason=reason, seconds=delay, consecutive_failures=self._failure_count)
        self._diagnostics.cooldown(self._retry_at)
        self._diagnostics.record('backoff', seconds=delay, consecutive_failures=self._failure_count,
                                 reason=reason if reason in ERRORS | {'incomplete refresh', 'tick failure', 'unresolved upload'} else 'other')

    def stop(self) -> None:
        self._stop.set()

    def _render_pending(self, conn) -> None:
        """Render derivatives for photos lacking one at the current pipeline version, BEFORE a
        refresh. Only the web upload and `fao ingest` rendered before, so a pipeline_version bump
        would have looked like an empty library. A bad photo must never block rotation."""
        try:
            n = ingest.render_pending(conn, self.paths, self.fit, self.pv, self.quality,
                                      crop_tolerance=self.tolerance)
            if n:
                log.info("rendered %d pending derivative(s) before the refresh", n)
        except Exception as exc:  # noqa: BLE001
            label = type(exc).__name__ if type(exc).__name__ in ERRORS else 'other'
            log.error("render_pending failed (%s); continuing with what is already rendered", label)
            self._diagnostics.record('render.failed', error=label, stacks=exception_stacks(exc))
            self._diagnostics.incident('render_failed')

    def _harvest_due(self, now: datetime) -> bool:
        if self.harvest_interval.total_seconds() <= 0:
            return False
        return self._last_harvest is None or (now - self._last_harvest) >= self.harvest_interval

    def run(self) -> None:
        log.info("daemon started (tick=%ss, refresh every %s)", self.tick, self.refresh_interval)
        self._observations.start()
        fcfg = config.frame_config(self.cfg, self.base)
        self._diagnostics.start({
            'tick_seconds': self.tick, 'harvest_seconds': self.harvest_interval.total_seconds(),
            'refresh_seconds': self.refresh_interval.total_seconds(), 'batch_operations': self.batch_operations,
            'batch_seconds': self.batch_seconds, 'retry_initial_seconds': self.retry_initial,
            'retry_max_seconds': self.retry_max, 'connect_timeout': fcfg.connect_timeout,
            'socket_timeout': fcfg.ws_timeout, 'call_timeout': fcfg.ws_call_timeout,
            'connect_retries': fcfg.connect_retries, 'upload_minimum_deadline_seconds': 45,
        })
        try:
            while not self._stop.is_set():
                try:
                    self._tick_once()
                except Exception as exc:  # noqa: BLE001 - a bad tick must never kill the loop
                    label = type(exc).__name__ if type(exc).__name__ in ERRORS else 'other'
                    log.error("daemon tick error (%s)", label)
                    self._observations.note("tick_error", error=label)
                    self._back_off("tick failure")
                    self._diagnostics.cooldown(self._retry_at)
                    self._observations.report(self.paths["database"], force=True, reason="tick_failure")
                self._stop.wait(self.tick)
        finally:
            self._diagnostics.close()
            self._observations.report(self.paths["database"], force=True, reason="daemon_stop")
            log.info("daemon stopped")

    def _tick_once(self) -> None:
        with self._diagnostics.tick():
            try:
                self._tick_body()
            except Exception as exc:
                label = type(exc).__name__ if type(exc).__name__ in ERRORS else 'other'
                self._diagnostics.record('tick.failed', error=label, stacks=exception_stacks(exc))
                self._diagnostics.incident('tick_failed')
                raise
            finally:
                self._diagnostics.cooldown(self._retry_at)

    def _tick_body(self) -> None:
        with self._diagnostics.stage('summary'):
            self._observations.report(self.paths["database"])
        if time.monotonic() < self._retry_at:
            self._observations.pause_sampling()
            self._diagnostics.record('tick.deferred', cooldown_seconds=max(0, self._retry_at-time.monotonic()))
            return  # No TCP probes, identity calls, harvests or refreshes during cooldown.
        fc = FrameClient(config.frame_config(self.cfg, self.base))
        if self._diagnostics.enabled:
            fc._diagnostic_stack_seconds = self._diagnostics.cfg['slow_operation_seconds']
        setter = getattr(fc, "set_observer", None)
        if setter is not None:
            setter(self._progress, observation_id=self._observations.session)
        probe_started = time.monotonic()
        with self._diagnostics.stage('reachability'):
            reachable = fc.is_reachable()
        self._observations.sample(reachable, probe_seconds=time.monotonic()-probe_started)
        self._diagnostics.record('reachability.sample', reachable=reachable,
                                 probe_seconds=round(time.monotonic()-probe_started, 3))
        woke = reachable and self._was_reachable is False
        self._wake_pending = self._wake_pending or woke
        self._was_reachable = reachable
        if not reachable:
            return  # asleep/off — skip (and cannot wake the room)

        period = config.active_period(config.schedule(self.cfg))
        now = datetime.now(timezone.utc)
        with self._diagnostics.stage('database_open'):
            conn = db.open_db(self.paths["database"])
        try:
            with self._diagnostics.stage('planning'):
                host = config.frame_config(self.cfg, self.base).host
                pending = store.unresolved_uploads(conn, host=host)
                if pending and not upload_recovery.can_recover(pending):
                    store.require_resolved_uploads(conn, host=host)
                plan_key = batches.key(period, self.fit, self.pv)
                active = batches.for_host(conn, host, state="active")
                if self._last_refresh is None:
                    completed = batches.for_host(conn, host, state="complete")
                    if completed is not None and completed["plan_key"] == plan_key:
                        self._last_refresh = datetime.fromisoformat(completed["completed_at"])
                        self._applied_period = period["name"]
                due = (active is not None or period["name"] != self._applied_period
                       or self._last_refresh is None or (now-self._last_refresh) >= self.refresh_interval)
                harvest_due = self._harvest_due(now)
            self._diagnostics.record('planning.result', refresh_due=due, harvest_due=harvest_due,
                                     wake_pending=self._wake_pending, unresolved_count=len(pending),
                                     batch_id=active['id'] if active is not None else None)
            if not (due or pending or self._wake_pending or harvest_due):
                return
            if due and (active is None or active["plan_key"] != plan_key):
                with self._diagnostics.stage('rendering'):
                    self._render_pending(conn)   # CPU/disk work: do it before holding the TV connection
            with fc:
                with self._diagnostics.stage('identity'):
                    device_id = self._ensure_device(conn, fc)
                if pending and any(p["device_id"] != device_id for p in pending):
                    store.require_resolved_uploads(conn, host=host)
                with self._diagnostics.stage('recovery'):
                    upload_recovery.recover(fc, conn, device_id)
                if due:
                    with self._diagnostics.stage('refresh'):
                        res = scheduler.refresh(
                            fc, conn, device_id=device_id, period=period,
                            fit_mode=self.fit, pipeline_version=self.pv, matte_cfg=self.matte_cfg,
                            max_operations=self.batch_operations, max_seconds=self.batch_seconds,
                        )
                    self._diagnostics.record('refresh.result', **{key: res.get(key) for key in
                        ('batch_id', 'complete', 'added', 'removed', 'pending_adds', 'pending_removals', 'errors')})
                    if res.get("errors", 0):
                        self._diagnostics.incident('refresh_incomplete')
                        self._back_off("incomplete refresh")
                        log.warning("refresh[%s] incomplete: %s", period["name"],
                                    {k: v for k, v in res.items() if k != "uploads"})
                        self._observations.report(self.paths["database"], force=True, reason="refresh_error")
                        return
                    self._observations.note("chunk_result", batch_id=res.get("batch_id"),
                                            complete=res.get("complete", True), added=res.get("added", 0),
                                            removed=res.get("removed", 0), pending_adds=res.get("pending_adds", 0),
                                            pending_removals=res.get("pending_removals", 0))
                    if res.get("complete", True):
                        self._applied_period = period["name"]
                        self._last_refresh = datetime.now(timezone.utc)
                    self._last_harvest = now  # refresh harvests first, so this counts
                    log.info("refresh[%s] %s", period["name"],
                             {k: v for k, v in res.items() if k != "uploads"})
                else:
                    if self._wake_pending:
                        with self._diagnostics.stage('slideshow'):
                            fc.set_slideshow(duration=period["interval"], shuffle=period["shuffle"])
                        log.info("woke → re-asserted slideshow (%s, %sm shuffle=%s)",
                                  period["name"], period["interval"], period["shuffle"])
                        self._wake_pending = False
                    if harvest_due:
                        # Attempt-based: a failing TV backs off to the next interval rather
                        # than being retried every tick.
                        self._last_harvest = now
                        with self._diagnostics.stage('harvest'):
                            changes = harvest_mod.harvest(fc, conn, device_id)
                        if changes:
                            log.info("harvest: recorded %d matte edit(s) made on the TV", len(changes))
            self._wake_pending = False
            self._failure_count = 0
            self._retry_at = 0.0
        except DeviceBusy as e:
            self._retry_at = time.monotonic() + self.tick
            log.info("%s; defer this tick", e)
            self._observations.note("device_busy")
            self._diagnostics.record('device.busy')
        except upload_recovery.UploadRecoveryPending as e:
            self._retry_at = time.monotonic() + e.retry_after
            log.info("%s", e)
            self._observations.note("recovery_wait", seconds=e.retry_after)
            self._diagnostics.record('recovery.wait', seconds=e.retry_after)
        except FrameUploadUncertain:
            self._diagnostics.incident('upload_uncertain')
            self._retry_at = time.monotonic() + upload_recovery.SETTLE_SECONDS
            log.warning("upload interrupted; pause the batch and verify inventory after settling (sleep is possible, not proven)")
            self._observations.report(self.paths["database"], force=True, reason="upload_uncertain")
        except FrameAsleep as e:
            if type(e) is FrameAsleep:
                # This is the fast TCP gate, not a hung Art call. A normal sleep
                # must not grow a 30-minute outage backoff across wake cycles.
                self._retry_at = time.monotonic() + self.tick
                log.info("TV API unreachable mid-batch; preserve progress and wait for reachability")
            else:
                self._back_off(type(e).__name__)
            self._observations.report(self.paths["database"], force=True, reason="tv_unavailable")
        except store.UploadResolutionRequired as e:
            self._back_off("unresolved upload")
            log.warning("unresolved upload requires manual review")
            self._diagnostics.incident('manual_review')
            self._observations.report(self.paths["database"], force=True, reason="manual_review")
        finally:
            with self._diagnostics.stage('database_close'):
                conn.close()

    def _ensure_device(self, conn, fc: FrameClient) -> int:
        info = fc.device_info().get("device", {})
        return store.ensure_device(
            conn, name=html.unescape(info.get("name", "Frame")), host=fc.cfg.host,
            duid=info.get("duid"), api_version=fc.api_version(),
        )


def run_forever(cfg: dict, base_dir, tick: float | None = None) -> None:
    """Blocking entry point (for `fao daemon`)."""
    Daemon(cfg, base_dir, tick=tick).run()
