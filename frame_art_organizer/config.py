"""Load config.toml and build typed config objects."""
from __future__ import annotations

import tomllib
from datetime import datetime
from pathlib import Path

from .frame_client import FrameConfig

# Slideshow intervals the Frame accepts for MY-C0002 (minutes). See SAMSUNG_FRAME_API.md.
VALID_INTERVALS = (3, 15, 60, 720, 1440, 10080)

_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _parse_days(value):
    """Normalize a period's `days` (e.g. ["mon","fri"] or ["saturday"]) to a set of
    weekday indices (Mon=0 … Sun=6). None/empty means the period applies every day."""
    if not value:
        return None
    days = set()
    for token in value:
        idx = _WEEKDAYS.get(str(token).strip().lower())
        if idx is not None:
            days.add(idx)
    return days or None

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def load(path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def frame_config(cfg: dict, base_dir: str | Path) -> FrameConfig:
    """Build a FrameConfig, resolving token_file relative to the config file's dir."""
    f = cfg["frame"]
    token = Path(base_dir) / f.get("token_file", ".token")
    return FrameConfig(
        host=f["host"],
        port=int(f.get("port", 8002)),
        token_file=str(token.resolve()),
        name=f.get("name", "FrameArtOrganizer"),
        connect_timeout=float(f.get("connect_timeout", 3.0)),
    )


def paths(cfg: dict, base_dir: str | Path) -> dict:
    """Resolve the [paths] section to absolute Paths relative to the config file's dir."""
    p = cfg.get("paths", {})
    base = Path(base_dir)

    def resolve(key: str, default: str) -> Path:
        return (base / p.get(key, default)).resolve()

    return {
        "inbox": resolve("inbox", "data/inbox"),
        "originals": resolve("originals", "data/originals"),
        "derivatives": resolve("derivatives", "data/derivatives"),
        "database": resolve("database", "data/state.db"),
    }


def schedule(cfg: dict) -> dict:
    """Parse [schedule] into defaults + fully-resolved periods."""
    s = cfg.get("schedule", {})
    defaults = {
        "interval": int(s.get("interval_minutes", 15)),
        "shuffle": bool(s.get("shuffle", True)),
        "set_size": int(s.get("set_size", 40)),
        "no_repeat_days": int(s.get("no_repeat_days", 30)),
    }
    periods = []
    for p in s.get("period", []):
        periods.append({
            "name": p.get("name", "period"),
            "collections": list(p.get("collections", [])),
            "days": _parse_days(p.get("days")),  # None = every day
            "start": p.get("start"),             # "HH:MM" or None
            "interval": int(p.get("interval_minutes", defaults["interval"])),
            "shuffle": bool(p.get("shuffle", defaults["shuffle"])),
            "set_size": int(p.get("set_size", defaults["set_size"])),
            "no_repeat_days": int(p.get("no_repeat_days", defaults["no_repeat_days"])),
        })
    if not periods:
        periods = [{"name": "all-day", "collections": [], "days": None, "start": None, **defaults}]
    return {"defaults": defaults, "periods": periods}


def active_period(sched: dict, now: datetime | None = None) -> dict:
    """Pick the active period for `now`.

    1. Keep periods whose `days` include today (day-restricted periods win over
       every-day ones, so a "sunday" period overrides an "all-day" fallback).
    2. Within those, pick by time of day: the last period whose `start` has passed
       (wrapping overnight). If none have a `start`, the first matching period wins.
    """
    now = now or datetime.now()
    weekday = now.weekday()  # Mon=0 … Sun=6

    def today(p) -> bool:
        return not p.get("days") or weekday in p["days"]

    matching = [p for p in sched["periods"] if today(p)]
    if not matching:
        matching = sched["periods"]  # nothing configured for today → fall back to all
    restricted = [p for p in matching if p.get("days")]
    pool = restricted or matching  # prefer the more specific, day-restricted periods

    current = now.strftime("%H:%M")
    timed = sorted((p for p in pool if p.get("start")), key=lambda p: p["start"])
    if not timed:
        return pool[0]
    active = timed[-1]  # before the first start today → yesterday's last window
    for p in timed:
        if p["start"] <= current:
            active = p
    return active
