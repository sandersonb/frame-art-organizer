"""Run the matte harvest against a live TV: fetch the listing, record edits, log them.

The pure comparison lives in `store.harvest_mattes`; this is the thin glue the CLI,
`reconcile`, `scheduler.refresh` and the daemon share (SPEC.md §12.4).
"""
from __future__ import annotations

import logging
import sqlite3

from . import store

log = logging.getLogger(__name__)


def harvest(client, conn: sqlite3.Connection, device_id: int, *, apply: bool = True,
            listing: list[dict] | None = None) -> list[dict]:
    """Record matte edits the user made on the TV. Returns the list of changes.

    Pass `listing` if the caller already fetched `available('MY-C0002')` (reconcile does).
    A TV that can't answer raises FrameAsleep/FrameTimeout from `list_my_photos` — it
    propagates, because callers treat that as "not now, retry later".
    """
    if listing is None:
        listing = client.list_my_photos()
    changes = store.harvest_mattes(conn, device_id, listing, apply=apply)
    for c in changes:
        log.info("matte edited on the TV%s: asset=%s (%s) %s  %s -> %s  [shape=%s]",
                 "" if apply else " (dry run)", c["asset_id"], c["original_name"],
                 c["content_id"], c["old"], c["new"], c["shape"])
    return changes
