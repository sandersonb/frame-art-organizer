"""Shared test helpers. No TV, no network — a throwaway SQLite DB per test."""
from __future__ import annotations

import pytest

from frame_art_organizer import db, store


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "state.db")
    yield c
    c.close()


def add_device(conn, host="192.0.2.10") -> int:  # RFC 5737 documentation address
    return store.ensure_device(conn, name="Test Frame", host=host, duid="uuid:test-device")


def add_asset(conn, n=1, name=None, width=4000, height=3000, status="active") -> int:
    sha = f"{n:064x}"
    return store.insert_asset(
        conn, sha256=sha, original_path=f"/orig/{sha}.jpg", original_name=name or f"photo{n}.jpg",
        bytes_=1, mime="image/jpeg", width=width, height=height, captured_at=None, status=status,
    )


def add_derivative(conn, asset_id, width=3840, height=2160, fit="cover", pv=1, path=None) -> int:
    return store.add_derivative(
        conn, asset_id=asset_id, path=path or f"/deriv/{asset_id}_{fit}_v{pv}.jpg", sha256=f"d{asset_id:063x}",
        fit_mode=fit, width=width, height=height, pipeline_version=pv,
    )


def add_placement(conn, device_id, derivative_id, content_id, matte="none", state="present") -> int:
    pid = store.create_pending_placement(conn, device_id, derivative_id, matte)
    if state == "present":
        store.set_placement_present(conn, pid, content_id)
    elif state == "deleted_on_device":
        store.set_placement_present(conn, pid, content_id)
        store.set_placement_deleted(conn, pid)
    elif state == "error":
        store.set_placement_error(conn, pid)
    return pid  # state == "pending": left as created


def resident(conn, device_id, n, content_id=None, matte="none", width=3840, height=2160, **kw):
    """One asset + derivative + present placement; returns (asset_id, derivative_id, placement_id)."""
    a = add_asset(conn, n, **kw)
    d = add_derivative(conn, a, width=width, height=height)
    p = add_placement(conn, device_id, d, content_id or f"MY_F{n:04d}", matte=matte)
    return a, d, p


def item(content_id, matte="none", **extra):
    """A fake `available('MY-C0002')` listing entry."""
    return {"content_id": content_id, "category_id": "MY-C0002", "matte_id": matte,
            "portrait_matte_id": "none", "width": 3840, "height": 2160, **extra}
