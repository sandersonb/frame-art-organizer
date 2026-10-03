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
import threading
from datetime import datetime, timedelta, timezone

from . import config, db, scheduler, store
from . import harvest as harvest_mod
from .frame_client import FrameAsleep, FrameClient

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
        img = cfg.get("image", {})
        self.fit = img.get("default_fit", "cover")
        self.pv = int(img.get("pipeline_version", 1))
        self.matte = img.get("default_matte", "none")
        self.refresh_interval = timedelta(
            hours=float(cfg.get("schedule", {}).get("refresh_hours", 24))
        )
        self._stop = threading.Event()
        self._applied_period: str | None = None
        self._last_refresh: datetime | None = None
        self._last_harvest: datetime | None = None
        self._was_reachable: bool | None = None

    def stop(self) -> None:
        self._stop.set()

    def _harvest_due(self, now: datetime) -> bool:
        if self.harvest_interval.total_seconds() <= 0:
            return False
        return self._last_harvest is None or (now - self._last_harvest) >= self.harvest_interval

    def run(self) -> None:
        log.info("daemon started (tick=%ss, refresh every %s)", self.tick, self.refresh_interval)
        while not self._stop.is_set():
            try:
                self._tick_once()
            except Exception:  # noqa: BLE001 - a bad tick must never kill the loop
                log.exception("daemon tick error")
            self._stop.wait(self.tick)
        log.info("daemon stopped")

    def _tick_once(self) -> None:
        fc = FrameClient(config.frame_config(self.cfg, self.base))
        reachable = fc.is_reachable()
        woke = reachable and self._was_reachable is False
        self._was_reachable = reachable
        if not reachable:
            return  # asleep/off — skip (and cannot wake the room)

        period = config.active_period(config.schedule(self.cfg))
        now = datetime.now(timezone.utc)
        due = (period["name"] != self._applied_period
               or self._last_refresh is None
               or (now - self._last_refresh) >= self.refresh_interval)
        harvest_due = self._harvest_due(now)
        if not (due or woke or harvest_due):
            return  # awake, nothing to do this tick

        conn = db.open_db(self.paths["database"])
        try:
            with fc:
                device_id = self._ensure_device(conn, fc)
                if due:
                    res = scheduler.refresh(
                        fc, conn, device_id=device_id, period=period,
                        fit_mode=self.fit, pipeline_version=self.pv, matte=self.matte,
                    )
                    self._applied_period = period["name"]
                    self._last_refresh = now
                    self._last_harvest = now  # refresh harvests first, so this counts
                    log.info("refresh[%s] %s", period["name"], res)
                else:
                    if woke:
                        fc.set_slideshow(duration=period["interval"], shuffle=period["shuffle"])
                        log.info("woke → re-asserted slideshow (%s, %sm shuffle=%s)",
                                 period["name"], period["interval"], period["shuffle"])
                    if harvest_due:
                        # Attempt-based: a failing TV backs off to the next interval rather
                        # than being retried every tick.
                        self._last_harvest = now
                        changes = harvest_mod.harvest(fc, conn, device_id)
                        if changes:
                            log.info("harvest: recorded %d matte edit(s) made on the TV", len(changes))
        except FrameAsleep as e:
            log.info("TV became unreachable mid-tick (%s); will retry", type(e).__name__)
        finally:
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
