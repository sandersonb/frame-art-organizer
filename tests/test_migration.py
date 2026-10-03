"""Migration 3 adds the per-asset matte preference columns without touching existing data."""
from frame_art_organizer import db


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_database_is_at_the_latest_version(tmp_path):
    conn = db.open_db(tmp_path / "fresh.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) == 3
    assert {"matte", "matte_shape"} <= _columns(conn, "asset_policy")


def test_upgrade_from_v2_preserves_existing_rows(tmp_path):
    # Build a database exactly as the deployed (version 2) one looks.
    conn = db.connect(tmp_path / "v2.db")
    conn.executescript(db._V1)
    conn.executescript(db._V2)
    conn.execute("PRAGMA user_version = 2")
    conn.execute(
        "INSERT INTO asset (sha256, original_path, imported_at) VALUES ('a', '/x', '2026-01-01')"
    )
    conn.execute("INSERT INTO asset_policy (asset_id, pinned, weight) VALUES (1, 1, 2.5)")
    conn.commit()
    assert "matte" not in _columns(conn, "asset_policy")

    assert db.migrate(conn) == 3

    row = conn.execute("SELECT pinned, weight, matte, matte_shape FROM asset_policy").fetchone()
    assert (row["pinned"], row["weight"]) == (1, 2.5)          # untouched
    assert row["matte"] is None and row["matte_shape"] is None  # NULL = no preference yet


def test_migration_is_idempotent(tmp_path):
    conn = db.open_db(tmp_path / "idem.db")
    assert db.migrate(conn) == 3
    assert db.migrate(conn) == 3
