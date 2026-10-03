"""SQLite connection + forward-only schema migrations.

Version is tracked in `PRAGMA user_version`; each entry in MIGRATIONS is one step.
The DB is mostly rebuildable (originals on disk + TV content) — its irreplaceable
payload is rotation history, collections, and weights. See SPEC.md §4.
"""
from __future__ import annotations

import math
import random
import sqlite3
from pathlib import Path


def _weighted_key(weight) -> float:
    """Exponential/reservoir sampling key: lower sorts first, higher weight → more likely.

    Registered as SQL `wkey(weight)` so the selection query does weighted-random ordering
    without depending on SQLite's optional math functions (ln/log).
    """
    w = weight if (weight and weight > 0) else 1.0
    return -math.log(1.0 - random.random()) / w

# --- migration 1: initial schema -------------------------------------------------
_V1 = """
CREATE TABLE device (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,
  host         TEXT NOT NULL,
  duid         TEXT UNIQUE,              -- stable TV UUID across IP changes
  api_version  TEXT,
  last_seen_at TEXT
);

CREATE TABLE asset (
  id            INTEGER PRIMARY KEY,
  sha256        TEXT NOT NULL UNIQUE,    -- identity + dedup on ORIGINAL bytes
  original_path TEXT NOT NULL,           -- preserved archive location
  original_name TEXT,
  bytes         INTEGER,
  mime          TEXT,
  width         INTEGER,
  height        INTEGER,
  captured_at   TEXT,                    -- EXIF DateTimeOriginal (YYYY:MM:DD HH:MM:SS)
  title         TEXT,                    -- human caption; NOT settable on the TV (parked)
  imported_at   TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','hidden','broken','deleted'))
);

CREATE TABLE asset_policy (
  asset_id   INTEGER PRIMARY KEY REFERENCES asset(id) ON DELETE CASCADE,
  pinned     INTEGER NOT NULL DEFAULT 0,
  suppressed INTEGER NOT NULL DEFAULT 0,
  weight     REAL    NOT NULL DEFAULT 1.0
);

-- Rendered pixels only (fit_mode + pipeline_version). Matte is a display/upload
-- attribute and lives on `placement`, not here (it doesn't change the pixels).
CREATE TABLE derivative (
  id               INTEGER PRIMARY KEY,
  asset_id         INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  path             TEXT NOT NULL,
  sha256           TEXT NOT NULL,
  fit_mode         TEXT NOT NULL,        -- cover | contain
  width            INTEGER,
  height           INTEGER,
  pipeline_version INTEGER NOT NULL,
  rendered_at      TEXT NOT NULL,
  UNIQUE(asset_id, fit_mode, pipeline_version)
);

-- A derivative resident in a device's MY-C0002. The only place content_id lives.
CREATE TABLE placement (
  id               INTEGER PRIMARY KEY,
  device_id        INTEGER NOT NULL REFERENCES device(id),
  derivative_id    INTEGER NOT NULL REFERENCES derivative(id),
  content_id       TEXT,                 -- TV-assigned MY_Fxxxx; NULL while pending
  matte            TEXT NOT NULL DEFAULT 'none',
  state            TEXT NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending','present','deleted_on_device','error')),
  uploaded_at      TEXT,
  last_verified_at TEXT,
  UNIQUE(device_id, content_id)
);
CREATE UNIQUE INDEX uq_present_placement
  ON placement(device_id, derivative_id) WHERE state = 'present';

CREATE TABLE rotation_event (
  id        INTEGER PRIMARY KEY,
  device_id INTEGER NOT NULL REFERENCES device(id),
  asset_id  INTEGER REFERENCES asset(id),   -- denormalized; survives content_id churn
  event     TEXT NOT NULL CHECK (event IN ('added','removed')),
  at        TEXT NOT NULL,
  rule      TEXT
);

CREATE TABLE collection (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL UNIQUE,
  kind      TEXT NOT NULL DEFAULT 'manual' CHECK (kind IN ('manual','smart')),
  rule_json TEXT
);
CREATE TABLE asset_collection (
  asset_id      INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  collection_id INTEGER NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
  PRIMARY KEY (asset_id, collection_id)
);

CREATE INDEX idx_derivative_asset       ON derivative(asset_id);
CREATE INDEX idx_placement_device_state ON placement(device_id, state);
CREATE INDEX idx_rotation_asset         ON rotation_event(asset_id, at);
"""

# --- migration 2: per-collection color (additive; ADD COLUMN is safe in SQLite) ------
_V2 = """
ALTER TABLE collection ADD COLUMN color TEXT;
"""

# --- migration 3: per-asset matte preference, harvested from the TV (additive) -----------
# The TV is the matte editor (SPEC.md §12.4): the user's choice is read back from the TV and
# remembered per asset so it survives the photo being rotated out and uploaded again.
_V3 = """
ALTER TABLE asset_policy ADD COLUMN matte TEXT;        -- NULL = no preference
ALTER TABLE asset_policy ADD COLUMN matte_shape TEXT;  -- 'wide' | 'odd': the shape class it was chosen under
"""

MIGRATIONS = [_V1, _V2, _V3]  # index+1 == version


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.create_function("wkey", 1, _weighted_key)  # weighted-random ordering
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any pending migrations; return the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS, start=1):
        if version > current:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
    return conn.execute("PRAGMA user_version").fetchone()[0]


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
