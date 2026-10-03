"""Scheduler — compose the desired working set and reconcile the TV toward it.

`plan()` is pure (DB only): it computes what the rotation *should* be and the add/remove
delta vs. what's currently resident. `refresh()` applies that delta to the device (upload
new, evict old, record rotation history) and sets the native slideshow. All device work
is reachability-gated by the FrameClient (FrameAsleep → caller skips/retries).

A `refresh` is a deliberate rotation of the set (run per period boundary / daily), not
every tick — so churn is intentional, not accidental.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from samsungtvws import exceptions

from . import harvest as harvest_mod
from . import mattes, store
from .frame_client import FrameAsleep, FrameClient

log = logging.getLogger(__name__)


def plan(conn: sqlite3.Connection, *, device_id, period, fit_mode, pipeline_version):
    """Returns (desired_rows, to_add_rows, to_remove_placements, present_placements).

    Diffs by DERIVATIVE, not by asset. After a `pipeline_version` bump the resident (old)
    derivative of a still-wanted photo is stale and its current one is "to add", so a version
    change swaps stale for current through the normal add-before-remove order. In steady state
    a photo has exactly one derivative, so this is identical to diffing by asset.

    A resident photo whose current derivative has not been rendered yet is KEPT, never evicted
    for lack of a replacement: the set must not shrink while a migration is half-rendered.
    """
    desired = store.compose_working_set(
        conn, device_id=device_id, collections=period["collections"],
        fit_mode=fit_mode, pipeline_version=pipeline_version,
        no_repeat_days=period["no_repeat_days"], set_size=period["set_size"],
    )
    present = store.present_placements(conn, device_id)
    present_derivs = {p["derivative_id"] for p in present}
    desired_derivs = {r["derivative_id"] for r in desired}
    unrendered = {a["id"] for a in store.assets_needing_render(conn, fit_mode, pipeline_version)}
    to_add = [r for r in desired if r["derivative_id"] not in present_derivs]
    to_remove = [p for p in present
                 if p["derivative_id"] not in desired_derivs and p["asset_id"] not in unrendered]
    return desired, to_add, to_remove, present


def _apply_slideshow(client: FrameClient, period) -> None:
    """Set the MY-C0002 slideshow; if the interval is rejected, fall back to 3 min."""
    try:
        client.set_slideshow(duration=period["interval"], shuffle=period["shuffle"])
    except exceptions.ResponseError:
        if period["interval"] != 3:
            log.warning("slideshow interval %sm rejected by the Frame; falling back to 3m",
                        period["interval"])
            client.set_slideshow(duration=3, shuffle=period["shuffle"])
        else:
            raise


def refresh(client: FrameClient, conn: sqlite3.Connection, *, device_id, period,
            fit_mode, pipeline_version, matte_cfg: mattes.MatteConfig,
            limit: int | None = None) -> dict:
    """Reconcile MY-C0002 to the desired set and set the slideshow.

    Each upload's matte comes from `mattes.matte_for` (the user's TV-side choice for that photo
    if any, else the shape's default). `limit` is for canaries (SPEC.md §12.8 M3): upload at
    most that many photos this run and evict no more than were added, so the set never shrinks;
    stale versions of photos just re-added are evicted first.
    """
    desired, to_add, to_remove, _ = plan(
        conn, device_id=device_id, period=period,
        fit_mode=fit_mode, pipeline_version=pipeline_version,
    )
    # An empty desired set almost always means misconfiguration (collections with no
    # photos, or an empty library) — NOT "blank the Frame". Skip: don't evict what's
    # resident, and never set a slideshow over an empty category (that returns -7).
    if not desired:
        log.warning(
            "refresh[%s]: no eligible photos (collections=%s) — leaving the Frame as-is. "
            "Add photos to those collections, or set collections=[] for all photos.",
            period["name"], period["collections"] or "all",
        )
        return {"desired": 0, "added": 0, "removed": 0, "errors": 0, "skipped": "empty-set",
                "harvested": 0, "removals_skipped": False, "pending_adds": 0, "mattes_used": {},
                "interval": period["interval"], "shuffle": period["shuffle"]}

    added = removed = errors = 0

    # No harvest, no evict (SPEC.md §12.4): read the user's TV-side matte edits BEFORE anything
    # is deleted, because deleting a photo destroys its matte. A TV that can't answer
    # (FrameAsleep/FrameTimeout) aborts the whole refresh — nothing is evicted and the daemon
    # retries. Any other harvest failure skips only the evictions so a bug can't stall rotation.
    harvested: list[dict] = []
    removals_ok = True
    try:
        harvested = harvest_mod.harvest(client, conn, device_id)
    except FrameAsleep:
        raise
    except Exception:  # noqa: BLE001
        log.exception("refresh[%s]: matte harvest failed — skipping evictions this run",
                      period["name"])
        removals_ok = False

    adds = to_add if limit is None else to_add[:max(0, limit)]
    added_assets: set[int] = set()
    mattes_used: dict[str, int] = {}
    for r in adds:
        matte = mattes.matte_for(r["pref_matte"], r["pref_shape"], r["width"], r["height"], matte_cfg)
        placement_id = store.create_pending_placement(conn, device_id, r["derivative_id"], matte)
        try:
            data = Path(r["path"]).read_bytes()
            content_id = client.upload_jpeg(data, matte=matte, date=r["captured_at"])
        except FrameAsleep:
            store.set_placement_error(conn, placement_id)
            raise
        except Exception:  # noqa: BLE001
            store.set_placement_error(conn, placement_id)
            errors += 1
            continue
        store.set_placement_present(conn, placement_id, content_id)
        store.add_rotation_event(conn, device_id, r["asset_id"], "added", period["name"])
        added_assets.add(r["asset_id"])
        mattes_used[matte] = mattes_used.get(matte, 0) + 1
        added += 1

    removals = to_remove if removals_ok else []
    if limit is not None:
        removals = sorted(removals, key=lambda p: p["asset_id"] not in added_assets)[:added]
    for p in removals:
        try:
            if p["content_id"]:
                client.delete(p["content_id"])
        except FrameAsleep:
            raise
        except Exception:  # noqa: BLE001 - a failed evict shouldn't sink the pass
            pass
        store.set_placement_deleted(conn, p["id"])
        store.add_rotation_event(conn, device_id, p["asset_id"], "removed", period["name"])
        removed += 1

    # Only drive the slideshow if something is actually resident (empty category → -7).
    if store.present_placements(conn, device_id):
        _apply_slideshow(client, period)
    else:
        log.warning("refresh[%s]: nothing resident after sync — slideshow left unchanged",
                    period["name"])
    return {"desired": len(desired), "added": added, "removed": removed, "errors": errors,
            "harvested": len(harvested), "removals_skipped": not removals_ok,
            "pending_adds": len(to_add) - len(adds), "mattes_used": mattes_used,
            "interval": period["interval"], "shuffle": period["shuffle"]}
