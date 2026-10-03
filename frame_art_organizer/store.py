"""Repository layer over the SQLite store — thin typed helpers, no ORM.

Keeps SQL in one place so components (ingest, uploader, scheduler) don't hand-roll
queries. All timestamps are UTC ISO-8601 strings.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import images, mattes


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- assets ----------------------------------------------------------------------
def get_asset_by_sha(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM asset WHERE sha256 = ?", (sha256,)).fetchone()


def insert_asset(conn: sqlite3.Connection, *, sha256, original_path, original_name,
                 bytes_, mime, width, height, captured_at, status="active") -> int:
    cur = conn.execute(
        """INSERT INTO asset
             (sha256, original_path, original_name, bytes, mime, width, height,
              captured_at, imported_at, status)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sha256, original_path, original_name, bytes_, mime, width, height,
         captured_at, now(), status),
    )
    conn.commit()
    return cur.lastrowid


def list_assets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT a.*,
                  (SELECT COUNT(*) FROM derivative d WHERE d.asset_id = a.id) AS derivatives
           FROM asset a
           ORDER BY a.imported_at DESC, a.id DESC"""
    ).fetchall()


def get_asset(conn, asset_id) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone()


# Which derivative stands for a photo in the UI: the one at the configured pipeline if it exists
# (so after a rollback the gallery shows what the TV shows), else the newest. The two `?` are
# (fit_mode, pipeline_version); NULLs simply fall through to "newest".
_PREFER_CURRENT = "ORDER BY (fit_mode = ? AND pipeline_version = ?) DESC, pipeline_version DESC"


def get_any_derivative(conn, asset_id, fit_mode=None, pipeline_version=None) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM derivative WHERE asset_id = ? {_PREFER_CURRENT} LIMIT 1",
        (asset_id, fit_mode, pipeline_version),
    ).fetchone()


def mark_asset_deleted(conn, asset_id) -> None:
    conn.execute("UPDATE asset SET status = 'deleted' WHERE id = ?", (asset_id,))
    conn.commit()


_SORTS = {
    "newest": "a.imported_at DESC, a.id DESC",
    "oldest": "a.imported_at ASC, a.id ASC",
    "name": "COALESCE(a.title, a.original_name) COLLATE NOCASE ASC",
    "taken": "a.captured_at DESC NULLS LAST, a.imported_at DESC",
}


def list_gallery(conn, filt: str = "all", sort: str = "newest",
                 fit_mode=None, pipeline_version=None) -> list[sqlite3.Row]:
    """Filtered + sorted gallery rows. `filt`: all | none | broken | col:<id>.

    Each row also carries the geometry of its shown derivative (`d_id`, `d_width`, `d_height`,
    `d_fit`, `d_pv` — NULL if none is rendered) and the matte preference harvested from the TV
    (`pref_matte`, `pref_shape`), which is what the gallery badges are computed from."""
    where = ["a.status != 'deleted'"]
    params: list = []
    if filt == "broken":
        where = ["a.status = 'broken'"]
    elif filt == "none":
        where.append("NOT EXISTS (SELECT 1 FROM asset_collection ac WHERE ac.asset_id = a.id)")
    elif filt.startswith("col:"):
        where.append("EXISTS (SELECT 1 FROM asset_collection ac "
                     "WHERE ac.asset_id = a.id AND ac.collection_id = ?)")
        params.append(int(filt[4:]))
    order = _SORTS.get(sort, _SORTS["newest"])
    sql = f"""SELECT a.*,
             (SELECT COUNT(*) FROM derivative x WHERE x.asset_id = a.id) AS derivatives,
             (SELECT GROUP_CONCAT(c.name, ', ')
                FROM asset_collection ac JOIN collection c ON c.id = ac.collection_id
               WHERE ac.asset_id = a.id) AS collections,
             d.id AS d_id, d.width AS d_width, d.height AS d_height,
             d.fit_mode AS d_fit, d.pipeline_version AS d_pv,
             ap.matte AS pref_matte, ap.matte_shape AS pref_shape
           FROM asset a
           LEFT JOIN derivative d ON d.id = (
               SELECT id FROM derivative WHERE asset_id = a.id {_PREFER_CURRENT} LIMIT 1)
           LEFT JOIN asset_policy ap ON ap.asset_id = a.id
           WHERE {' AND '.join(where)}
           ORDER BY {order}"""
    return conn.execute(sql, [fit_mode, pipeline_version] + params).fetchall()


def counts_by_filter(conn) -> dict:
    """Counts for the filter-bar chips: total, no-collection, broken, and per collection."""
    total = conn.execute("SELECT COUNT(*) FROM asset WHERE status != 'deleted'").fetchone()[0]
    broken = conn.execute("SELECT COUNT(*) FROM asset WHERE status = 'broken'").fetchone()[0]
    none = conn.execute(
        """SELECT COUNT(*) FROM asset a WHERE a.status != 'deleted'
             AND NOT EXISTS (SELECT 1 FROM asset_collection ac WHERE ac.asset_id = a.id)"""
    ).fetchone()[0]
    cols = conn.execute(
        """SELECT c.id, c.name, c.color,
                  (SELECT COUNT(*) FROM asset_collection ac JOIN asset a ON a.id = ac.asset_id
                    WHERE ac.collection_id = c.id AND a.status != 'deleted') AS count
           FROM collection c ORDER BY c.name"""
    ).fetchall()
    return {"all": total, "none": none, "broken": broken,
            "collections": [dict(r) for r in cols]}


def rename_asset(conn, asset_id, title) -> None:
    """Set a local display label (never shown on the Frame). Empty clears it."""
    title = (title or "").strip() or None
    conn.execute("UPDATE asset SET title = ? WHERE id = ?", (title, asset_id))
    conn.commit()


# --- purging superseded derivatives (M4; see purge.py for the rules) -------------------------
def superseded_derivatives(conn, fit_mode, pipeline_version) -> list[sqlite3.Row]:
    """Derivatives that are not at the configured pipeline (nor from a newer one), whose photo has
    one that is, and that no present/pending placement uses. Candidates only — purge.py decides."""
    return conn.execute(
        """SELECT old.id AS id, old.asset_id AS asset_id, old.path AS path,
                  old.pipeline_version AS pipeline_version, old.fit_mode AS fit_mode,
                  cur.path AS replacement_path, cur.rendered_at AS replacement_rendered_at
           FROM derivative old
           JOIN derivative cur ON cur.asset_id = old.asset_id
                              AND cur.fit_mode = ? AND cur.pipeline_version = ?
           WHERE NOT (old.fit_mode = ? AND old.pipeline_version = ?)
             AND old.pipeline_version <= ?
             AND NOT EXISTS (SELECT 1 FROM placement p WHERE p.derivative_id = old.id
                             AND p.state IN ('present','pending'))
           ORDER BY old.id""",
        (fit_mode, pipeline_version, fit_mode, pipeline_version, pipeline_version),
    ).fetchall()


def stale_resident_count(conn, fit_mode, pipeline_version) -> int:
    """Placements on the TV (or about to be) whose derivative is not at the configured pipeline."""
    return conn.execute(
        """SELECT COUNT(*) FROM placement p JOIN derivative d ON d.id = p.derivative_id
           WHERE p.state IN ('present','pending')
             AND NOT (d.fit_mode = ? AND d.pipeline_version = ?)""",
        (fit_mode, pipeline_version),
    ).fetchone()[0]


def newer_derivative_count(conn, pipeline_version) -> int:
    return conn.execute("SELECT COUNT(*) FROM derivative WHERE pipeline_version > ?",
                        (pipeline_version,)).fetchone()[0]


def purge_derivative(conn, derivative_id) -> int:
    """Delete a derivative row and its dead placement history; returns how many history rows went.
    Refuses (ValueError) if anything present or pending uses it. Does NOT commit or touch files.

    The FK from placement has no cascade; non-present placements are history only (the real
    history, rotation_event, is keyed by asset and is untouched)."""
    live = conn.execute("SELECT COUNT(*) FROM placement WHERE derivative_id = ? "
                        "AND state IN ('present','pending')", (derivative_id,)).fetchone()[0]
    if live:
        raise ValueError(f"derivative {derivative_id} is in use by {live} placement(s)")
    n = conn.execute("DELETE FROM placement WHERE derivative_id = ?", (derivative_id,)).rowcount
    conn.execute("DELETE FROM derivative WHERE id = ?", (derivative_id,))
    return n


def get_matte_pref(conn, asset_id) -> tuple:
    """(matte, matte_shape) the user chose on the TV for this asset, or (None, None)."""
    r = conn.execute("SELECT matte, matte_shape FROM asset_policy WHERE asset_id = ?", (asset_id,)).fetchone()
    return (r["matte"], r["matte_shape"]) if r else (None, None)


def present_placements_for_asset(conn, asset_id) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.id, p.content_id, p.device_id, p.matte AS matte,
                  d.width AS width, d.height AS height,
                  d.fit_mode AS fit_mode, d.pipeline_version AS pipeline_version
           FROM placement p JOIN derivative d ON d.id = p.derivative_id
           WHERE d.asset_id = ? AND p.state = 'present'""",
        (asset_id,),
    ).fetchall()


def counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM asset)                         AS assets,
             (SELECT COUNT(*) FROM asset WHERE status='active')   AS active,
             (SELECT COUNT(*) FROM asset WHERE status='broken')   AS broken,
             (SELECT COUNT(*) FROM derivative)                    AS derivatives,
             (SELECT COUNT(*) FROM placement WHERE state='present') AS placed"""
    ).fetchone()
    return dict(row)


# --- derivatives -----------------------------------------------------------------
def assets_needing_render(conn: sqlite3.Connection, fit_mode: str,
                          pipeline_version: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT a.* FROM asset a
           WHERE a.status = 'active'
             AND NOT EXISTS (
               SELECT 1 FROM derivative d
               WHERE d.asset_id = a.id
                 AND d.fit_mode = ? AND d.pipeline_version = ?)""",
        (fit_mode, pipeline_version),
    ).fetchall()


def add_derivative(conn: sqlite3.Connection, *, asset_id, path, sha256, fit_mode,
                   width, height, pipeline_version) -> int:
    cur = conn.execute(
        """INSERT INTO derivative
             (asset_id, path, sha256, fit_mode, width, height, pipeline_version, rendered_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (asset_id, path, sha256, fit_mode, width, height, pipeline_version, now()),
    )
    conn.commit()
    return cur.lastrowid


# --- devices ---------------------------------------------------------------------
def ensure_device(conn: sqlite3.Connection, *, name, host, duid=None, api_version=None) -> int:
    """Get-or-create the device row, keyed by duid (stable) then host."""
    row = None
    if duid:
        row = conn.execute("SELECT * FROM device WHERE duid = ?", (duid,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM device WHERE host = ?", (host,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO device (name, host, duid, api_version, last_seen_at) VALUES (?,?,?,?,?)",
            (name, host, duid, api_version, now()),
        )
        conn.commit()
        return cur.lastrowid
    conn.execute(
        """UPDATE device SET name=?, host=?, duid=COALESCE(?, duid),
                            api_version=?, last_seen_at=? WHERE id=?""",
        (name, host, duid, api_version, now(), row["id"]),
    )
    conn.commit()
    return row["id"]


# --- placements ------------------------------------------------------------------
def derivatives_to_upload(conn: sqlite3.Connection, device_id, fit_mode,
                          pipeline_version) -> list[sqlite3.Row]:
    """Current derivatives of active assets with no present/pending placement here."""
    return conn.execute(
        """SELECT d.id AS derivative_id, d.path AS path, a.id AS asset_id,
                  a.captured_at AS captured_at, a.original_name AS original_name,
                  d.width AS width, d.height AS height,
                  ap.matte AS pref_matte, ap.matte_shape AS pref_shape
           FROM derivative d
           JOIN asset a ON a.id = d.asset_id
           LEFT JOIN asset_policy ap ON ap.asset_id = a.id
           WHERE a.status = 'active' AND d.fit_mode = ? AND d.pipeline_version = ?
             AND NOT EXISTS (
               SELECT 1 FROM placement p
               WHERE p.derivative_id = d.id AND p.device_id = ?
                 AND p.state IN ('present','pending'))
           ORDER BY a.imported_at""",
        (fit_mode, pipeline_version, device_id),
    ).fetchall()


def create_pending_placement(conn, device_id, derivative_id, matte) -> int:
    cur = conn.execute(
        "INSERT INTO placement (device_id, derivative_id, matte, state) VALUES (?,?,?,'pending')",
        (device_id, derivative_id, matte),
    )
    conn.commit()
    return cur.lastrowid


def set_placement_present(conn, placement_id, content_id) -> None:
    conn.execute(
        "UPDATE placement SET content_id=?, state='present', uploaded_at=?, last_verified_at=? WHERE id=?",
        (content_id, now(), now(), placement_id),
    )
    conn.commit()


def set_placement_error(conn, placement_id) -> None:
    conn.execute("UPDATE placement SET state='error' WHERE id=?", (placement_id,))
    conn.commit()


def set_placement_deleted(conn, placement_id) -> None:
    conn.execute(
        "UPDATE placement SET state='deleted_on_device', last_verified_at=? WHERE id=?",
        (now(), placement_id),
    )
    conn.commit()


def touch_placement_verified(conn, placement_id) -> None:
    conn.execute("UPDATE placement SET last_verified_at=? WHERE id=?", (now(), placement_id))
    conn.commit()


def present_placements(conn, device_id) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.id, p.content_id, p.matte AS matte, d.id AS derivative_id,
                  d.asset_id AS asset_id, d.width AS width, d.height AS height,
                  d.fit_mode AS fit_mode, d.pipeline_version AS pipeline_version,
                  a.original_name AS original_name
           FROM placement p
           JOIN derivative d ON d.id = p.derivative_id
           JOIN asset a ON a.id = d.asset_id
           WHERE p.device_id = ? AND p.state = 'present'""",
        (device_id,),
    ).fetchall()


# --- matte harvest (TV -> DB), SPEC.md §12.4 -----------------------------------------------
def set_asset_matte(conn, asset_id, matte, shape) -> None:
    """Remember the matte the user chose on the TV for this asset (and the shape class it was
    chosen under). Does not commit — callers batch it with the placement update."""
    conn.execute("INSERT OR IGNORE INTO asset_policy (asset_id) VALUES (?)", (asset_id,))
    conn.execute(
        "UPDATE asset_policy SET matte = ?, matte_shape = ? WHERE asset_id = ?",
        (matte, shape, asset_id),
    )


def asset_matte_prefs(conn) -> dict:
    """{asset_id: (matte, matte_shape)} for every asset the user chose a matte for on the TV."""
    return {r["asset_id"]: (r["matte"], r["matte_shape"]) for r in conn.execute(
        "SELECT asset_id, matte, matte_shape FROM asset_policy WHERE matte IS NOT NULL")}


def set_placement_matte(conn, placement_id, matte) -> None:
    """Record the matte currently on the TV for a placement. Does not commit."""
    conn.execute("UPDATE placement SET matte = ? WHERE id = ?", (matte, placement_id))


def harvest_mattes(conn, device_id, listing, *, apply=True) -> list[dict]:
    """TV -> DB: find matte edits the user made on the TV and remember them per asset.

    `listing` is the `available('MY-C0002')` result. For every placement of ours that is
    resident, compare the matte the TV reports now with the matte we last recorded for it; a
    difference means the user changed it in the TV's own UI (the TV wins). Only `matte_id` is
    read — `portrait_matte_id` is ignored (SPEC.md §12.4).

    Skipped: placements not in the listing (reconcile deals with those), listing items with no
    `matte_id` key at all (unknown is not "none"), and content we didn't upload (orphans).
    Returns one dict per change. With `apply=False` nothing is written (dry run).
    """
    by_id = {it.get("content_id"): it for it in (listing or []) if it.get("content_id")}
    changes = []
    for p in present_placements(conn, device_id):
        item = by_id.get(p["content_id"])
        if item is None or "matte_id" not in item:
            continue
        tv, old = mattes.normalize(item["matte_id"]), mattes.normalize(p["matte"])
        if tv == old:
            continue
        shape = images.shape_class(p["width"], p["height"])
        changes.append({
            "asset_id": p["asset_id"], "placement_id": p["id"], "content_id": p["content_id"],
            "original_name": p["original_name"], "old": old, "new": tv, "shape": shape,
        })
    if apply and changes:
        with conn:  # one transaction: preference + placement stay consistent
            for c in changes:
                set_asset_matte(conn, c["asset_id"], c["new"], c["shape"])
                set_placement_matte(conn, c["placement_id"], c["new"])
    return changes


def list_placements(conn, device_id) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.id, p.content_id, p.state, p.matte, d.asset_id AS asset_id,
                  a.original_name AS original_name
           FROM placement p
           JOIN derivative d ON d.id = p.derivative_id
           JOIN asset a ON a.id = d.asset_id
           WHERE p.device_id = ? ORDER BY p.id""",
        (device_id,),
    ).fetchall()


# --- rotation history ------------------------------------------------------------
def add_rotation_event(conn, device_id, asset_id, event, rule=None) -> None:
    conn.execute(
        "INSERT INTO rotation_event (device_id, asset_id, event, at, rule) VALUES (?,?,?,?,?)",
        (device_id, asset_id, event, now(), rule),
    )
    conn.commit()


# --- collections & policy --------------------------------------------------------
# Assignable collection colors (see ORGANIZER_UI_SPEC.md §4).
COLLECTION_PALETTE = [
    "#3b82f6", "#8b5cf6", "#10b981", "#ef4444",
    "#f59e0b", "#14b8a6", "#ec4899", "#6366f1",
]


def _next_color(conn) -> str:
    """Next palette color not already used; cycles once the palette is exhausted."""
    used = {r[0] for r in conn.execute("SELECT color FROM collection WHERE color IS NOT NULL")}
    for color in COLLECTION_PALETTE:
        if color not in used:
            return color
    n = conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0]
    return COLLECTION_PALETTE[n % len(COLLECTION_PALETTE)]


def get_or_create_collection(conn, name, kind="manual", color=None) -> int:
    row = conn.execute("SELECT id, color FROM collection WHERE name = ?", (name,)).fetchone()
    if row is not None:
        if color and row["color"] is None:
            conn.execute("UPDATE collection SET color = ? WHERE id = ?", (color, row["id"]))
            conn.commit()
        return row["id"]
    color = color or _next_color(conn)
    cur = conn.execute(
        "INSERT INTO collection (name, kind, color) VALUES (?, ?, ?)", (name, kind, color)
    )
    conn.commit()
    return cur.lastrowid


def set_collection_color(conn, collection_id, color) -> None:
    conn.execute("UPDATE collection SET color = ? WHERE id = ?", (color, collection_id))
    conn.commit()


def ensure_collection_colors(conn) -> None:
    """Backfill a color for any collection still missing one (e.g. pre-migration rows)."""
    for row in conn.execute("SELECT id FROM collection WHERE color IS NULL").fetchall():
        conn.execute("UPDATE collection SET color = ? WHERE id = ?", (_next_color(conn), row["id"]))
    conn.commit()


def remove_from_collection(conn, asset_id, collection_id) -> None:
    conn.execute(
        "DELETE FROM asset_collection WHERE asset_id = ? AND collection_id = ?",
        (asset_id, collection_id),
    )
    conn.commit()


def collections_for_asset(conn, asset_id) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.id, c.name, c.color
           FROM asset_collection ac JOIN collection c ON c.id = ac.collection_id
           WHERE ac.asset_id = ? ORDER BY c.name""",
        (asset_id,),
    ).fetchall()


def assign_collection(conn, asset_id, collection_id) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO asset_collection (asset_id, collection_id) VALUES (?, ?)",
        (asset_id, collection_id),
    )
    conn.commit()


def list_collections(conn) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.*,
                  (SELECT COUNT(*) FROM asset_collection ac WHERE ac.collection_id = c.id) AS members
           FROM collection c ORDER BY c.name"""
    ).fetchall()


def set_policy(conn, asset_id, *, pinned=None, suppressed=None, weight=None) -> None:
    conn.execute("INSERT OR IGNORE INTO asset_policy (asset_id) VALUES (?)", (asset_id,))
    if pinned is not None:
        conn.execute("UPDATE asset_policy SET pinned=? WHERE asset_id=?", (int(bool(pinned)), asset_id))
    if suppressed is not None:
        conn.execute("UPDATE asset_policy SET suppressed=? WHERE asset_id=?", (int(bool(suppressed)), asset_id))
    if weight is not None:
        conn.execute("UPDATE asset_policy SET weight=? WHERE asset_id=?", (float(weight), asset_id))
    conn.commit()


# --- working-set selection -------------------------------------------------------
def compose_working_set(conn, *, device_id, collections, fit_mode, pipeline_version,
                        no_repeat_days, set_size) -> list[sqlite3.Row]:
    """The desired rotation set: active, un-suppressed assets (in the given collections,
    or the WHOLE active library if `collections` is empty) with a current derivative;
    pinned first, then photos NOT rotated-in within the no-repeat window, then
    weighted-random — capped at set_size. No starvation: recently-rotated photos still
    fill in when the eligible pool is small."""
    window = f"-{int(no_repeat_days)} days"
    params: list = []
    if collections:
        placeholders = ",".join("?" * len(collections))
        col_join = (
            "JOIN asset_collection ac ON ac.asset_id = a.id\n"
            f"        JOIN collection c ON c.id = ac.collection_id AND c.name IN ({placeholders})"
        )
        params.extend(collections)
    else:
        col_join = ""  # empty → no collection filter → the whole active library
    sql = f"""
        SELECT d.id AS derivative_id, a.id AS asset_id, d.path AS path,
               a.captured_at AS captured_at, a.original_name AS original_name,
               COALESCE(ap.pinned, 0) AS pinned,
               d.width AS width, d.height AS height,
               ap.matte AS pref_matte, ap.matte_shape AS pref_shape
        FROM asset a
        {col_join}
        JOIN derivative d ON d.asset_id = a.id AND d.fit_mode = ? AND d.pipeline_version = ?
        LEFT JOIN asset_policy ap ON ap.asset_id = a.id
        WHERE a.status = 'active' AND COALESCE(ap.suppressed, 0) = 0
        GROUP BY a.id
        ORDER BY
          COALESCE(ap.pinned, 0) DESC,
          (EXISTS (SELECT 1 FROM rotation_event re
                   WHERE re.asset_id = a.id AND re.device_id = ?
                     AND re.event = 'added' AND re.at > datetime('now', ?))) ASC,
          wkey(COALESCE(ap.weight, 1.0)) ASC
        LIMIT ?"""
    params.extend([fit_mode, pipeline_version, device_id, window, set_size])
    return conn.execute(sql, params).fetchall()
