"""Uploader / placement reconciler — the bridge between the local library and the TV.

Two-phase upload (pending → present) makes crashes recoverable, and a reconcile pass
converges DB intent with device reality. All device work is reachability-gated by the
FrameClient (raises FrameAsleep → caller treats as skip/retry).

v1 `sync` uploads every active asset's current derivative that isn't yet resident; the
Phase-2 scheduler will narrow this to a working set.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import store
from .frame_client import FrameAsleep, FrameClient


def sync(client: FrameClient, conn: sqlite3.Connection, *, device_id: int,
         fit_mode: str, pipeline_version: int, matte: str) -> dict:
    """Upload derivatives not yet resident on the device. Two-phase per item."""
    summary = {"uploaded": 0, "errors": 0}
    for d in store.derivatives_to_upload(conn, device_id, fit_mode, pipeline_version):
        placement_id = store.create_pending_placement(conn, device_id, d["derivative_id"], matte)
        try:
            data = Path(d["path"]).read_bytes()
            content_id = client.upload_jpeg(data, matte=matte, date=d["captured_at"])
        except FrameAsleep:
            # TV went to sleep mid-sync: abort, leave a resolvable breadcrumb.
            store.set_placement_error(conn, placement_id)
            raise
        except Exception:  # noqa: BLE001 - one bad item shouldn't sink the batch
            store.set_placement_error(conn, placement_id)
            summary["errors"] += 1
            continue
        store.set_placement_present(conn, placement_id, content_id)
        summary["uploaded"] += 1
    return summary


def reconcile(client: FrameClient, conn: sqlite3.Connection, device_id: int) -> dict:
    """Diff device MY-C0002 against our `present` placements; converge and flag orphans."""
    on_device = {item.get("content_id") for item in client.list_my_photos()}
    on_device.discard(None)

    ours = store.present_placements(conn, device_id)
    our_ids = set()
    vanished = 0
    for p in ours:
        our_ids.add(p["content_id"])
        if p["content_id"] in on_device:
            store.touch_placement_verified(conn, p["id"])
        else:
            store.set_placement_deleted(conn, p["id"])  # deleted on the TV out from under us
            vanished += 1

    # Content we didn't put there (manual adds via the Samsung app): leave & flag.
    orphans = sorted(cid for cid in on_device if cid not in our_ids)
    return {
        "on_device": len(on_device),
        "ours_present": len(ours),
        "vanished": vanished,
        "orphans": orphans,
    }
