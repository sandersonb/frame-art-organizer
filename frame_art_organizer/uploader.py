"""Uploader / placement reconciler — the bridge between the local library and the TV.

Two-phase upload (pending → present) makes crashes recoverable, and a reconcile pass
converges DB intent with device reality. All device work is reachability-gated by the
FrameClient (raises FrameAsleep → caller treats as skip/retry).

v1 `sync` uploads every active asset's current derivative that isn't yet resident; the
Phase-2 scheduler will narrow this to a working set.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from . import harvest as harvest_mod
from . import mattes, store
from .frame_client import FrameAsleep, FrameClient, FrameUploadUncertain

log = logging.getLogger(__name__)


def sync(client: FrameClient, conn: sqlite3.Connection, *, device_id: int,
         fit_mode: str, pipeline_version: int, matte_cfg: mattes.MatteConfig) -> dict:
    """Upload derivatives not yet resident on the device. Two-phase per item.

    Each photo's matte comes from `mattes.matte_for` (SPEC.md §12.3)."""
    store.require_resolved_uploads(conn, device_id)
    summary = {"uploaded": 0, "errors": 0}
    for d in store.derivatives_to_upload(conn, device_id, fit_mode, pipeline_version):
        matte = mattes.matte_for(d["pref_matte"], d["pref_shape"], d["width"], d["height"], matte_cfg)
        placement_id = store.create_pending_placement(conn, device_id, d["derivative_id"], matte)
        log.info("upload.begin placement=%s asset=%s bytes_from=%s", placement_id, d["asset_id"], d["original_name"])
        try:
            data = Path(d["path"]).read_bytes()
            log.info("upload.prepared placement=%s asset=%s bytes=%s size=%sx%s matte=%s",
                     placement_id, d["asset_id"], len(data), d["width"], d["height"], matte)
            content_id = client.upload_jpeg(data, matte=matte, date=d["captured_at"])
        except FrameUploadUncertain:
            store.set_placement_uncertain(conn, placement_id)
            log.warning("upload.uncertain placement=%s asset=%s; automatic replay blocked", placement_id, d["asset_id"])
            raise
        except FrameAsleep as exc:
            # TV went to sleep mid-sync: abort, leave a resolvable breadcrumb.
            store.set_placement_error(conn, placement_id)
            log.warning("upload.not_submitted placement=%s asset=%s error=%s",
                        placement_id, d["asset_id"], type(exc).__name__)
            raise
        except Exception as exc:  # noqa: BLE001
            store.set_placement_error(conn, placement_id)
            log.warning("upload.failed placement=%s asset=%s error=%s",
                        placement_id, d["asset_id"], type(exc).__name__)
            summary["errors"] += 1
            continue
        store.set_placement_present(conn, placement_id, content_id)
        log.info("upload.recorded placement=%s asset=%s content_id=%s", placement_id, d["asset_id"], content_id)
        summary["uploaded"] += 1
    return summary


def reconcile(client: FrameClient, conn: sqlite3.Connection, device_id: int) -> dict:
    """Diff device MY-C0002 against our `present` placements; converge and flag orphans.

    Also harvests matte edits from the same listing (SPEC.md §12.4). A harvest failure is
    logged and reported but never stops reconcile's own work.
    """
    items = client.list_my_photos()
    on_device = {item.get("content_id") for item in items}
    on_device.discard(None)

    harvested = []
    harvest_error = None
    try:
        harvested = harvest_mod.harvest(client, conn, device_id, listing=items)
    except Exception as e:  # noqa: BLE001
        log.exception("reconcile: matte harvest failed")
        harvest_error = type(e).__name__

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
        "harvested": len(harvested),
        "harvest_error": harvest_error,
    }
