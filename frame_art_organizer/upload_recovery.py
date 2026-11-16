"""Conservative recovery from transport loss; it is not proof that the panel slept.

Only absence is recovered automatically. New IDs are never identified by photo
metadata: another client can upload a different image with the same size/date.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from . import store
from .frame_client import FrameAsleep, FrameConnectionError
from .device_lock import serialized_device

log = logging.getLogger(__name__)
SETTLE_SECONDS = 120
CONFIRM_SECONDS = 60
MAX_AUTO_RECOVERIES = 2


def utcnow():
    return datetime.now(timezone.utc)


class UploadRecoveryPending(FrameAsleep):
    def __init__(self, seconds):
        self.retry_after = max(1.0, seconds)
        super().__init__(f"upload recovery waiting for stable inventory; retry after {self.retry_after:.0f}s")


def inventory_ids(items):
    if not isinstance(items, list):
        raise FrameConnectionError("Invalid My Photos inventory")
    ids = []
    for item in items:
        cid = item.get("content_id") if isinstance(item, dict) else None
        if not isinstance(cid, str) or not re.fullmatch(r"MY_F[0-9]+", cid):
            raise FrameConnectionError("Invalid My Photos inventory ID")
        ids.append(cid)
    if len(set(ids)) != len(ids):
        raise FrameConnectionError("Duplicate My Photos inventory IDs")
    return set(ids)


def can_recover(rows):
    return (len(rows) == 1 and rows[0]["state"] == "pending"
            and rows[0]["upload_outcome"] in {"uncertain", "in_flight"}
            and rows[0]["upload_baseline"] is not None
            and (rows[0]["upload_failed_at"] is not None or rows[0]["upload_outcome"] == "in_flight")
            and not rows[0]["upload_recovery_blocked"])


def _manual(conn, row, reason):
    conn.execute("UPDATE placement SET upload_recovery_blocked=1 WHERE id=? AND upload_outcome='uncertain'",
                 (row["id"],))
    conn.commit()
    log.warning("upload.recovery.manual placement=%s reason=%s", row["id"], reason)
    store.require_resolved_uploads(conn, row["device_id"])


@serialized_device
def recover(client, conn, device_id):
    rows = store.unresolved_uploads(conn, device_id)
    if not rows:
        return
    # Legacy attempts have no baseline. A live managed writer holds device_lock,
    # so we cannot reach this point until it has finished or its process has died.
    if not can_recover(rows):
        store.require_resolved_uploads(conn, device_id)
    row = rows[0]
    if row["upload_outcome"] == "in_flight":
        # A crash can occur after sending the image but before journaling the
        # outcome. Start a NEW settling interval; do not immediately replay it.
        store.set_placement_uncertain(conn, row["id"])
        row = store.unresolved_uploads(conn, device_id)[0]
    retries = conn.execute("""SELECT count(*) FROM placement WHERE device_id=? AND derivative_id=?
                              AND upload_auto_recovered_at IS NOT NULL AND id > COALESCE(
                                (SELECT max(id) FROM placement WHERE device_id=? AND derivative_id=?
                                 AND content_id IS NOT NULL AND uploaded_at IS NOT NULL),0)""",
                           (device_id, row["derivative_id"], device_id, row["derivative_id"])).fetchone()[0]
    if retries >= MAX_AUTO_RECOVERIES:
        _manual(conn, row, "automatic retry limit reached")
    try:
        baseline = inventory_ids([{"content_id": cid} for cid in json.loads(row["upload_baseline"])])
        failed = datetime.fromisoformat(row["upload_failed_at"])
        seen = datetime.fromisoformat(row["upload_absence_seen_at"]) if row["upload_absence_seen_at"] else None
        now = utcnow()
        age = (now-failed).total_seconds()
        seen_age = (now-seen).total_seconds() if seen else None
    except (ValueError, TypeError, FrameConnectionError):
        store.require_resolved_uploads(conn, device_id)
    if age < 0 or (seen_age is not None and seen_age < 0):
        store.require_resolved_uploads(conn, device_id)
    if age < SETTLE_SECONDS:
        raise UploadRecoveryPending(SETTLE_SECONDS-age)
    if seen_age is not None and seen_age < CONFIRM_SECONDS:
        raise UploadRecoveryPending(CONFIRM_SECONDS-seen_age)
    try:
        info = client.device_info().get("device", {})
        if not row["duid"] or info.get("duid") != row["duid"]:
            _manual(conn, row, "TV identity not verified")
        current = inventory_ids(client.list_my_photos())
    except FrameAsleep:
        conn.execute("UPDATE placement SET upload_absence_seen_at=NULL WHERE id=? AND upload_outcome='uncertain'",
                     (row["id"],))
        conn.commit()
        raise
    if current != baseline:
        log.warning("upload.recovery.inventory_changed placement=%s new_ids=%s missing_ids=%s",
                    row["id"], sorted(current-baseline), sorted(baseline-current))
        _manual(conn, row, "inventory changed; no image identity assumed")
    if seen is None:
        conn.execute("""UPDATE placement SET upload_absence_seen_at=?
                        WHERE id=? AND state='pending' AND upload_outcome='uncertain'""",
                     (now.isoformat(), row["id"]))
        conn.commit()
        log.info("upload.recovery.absence_seen placement=%s", row["id"])
        raise UploadRecoveryPending(CONFIRM_SECONDS)
    # Conditional update cannot undo an operator's resolution made during the read.
    cur = conn.execute("""UPDATE placement SET state='error', upload_outcome='not_uploaded', upload_auto_recovered_at=?
                          WHERE id=? AND state='pending' AND content_id IS NULL
                          AND upload_outcome='uncertain' AND upload_baseline=?
                          AND upload_absence_seen_at=? AND upload_recovery_blocked=0""",
                       (now.isoformat(), row["id"], row["upload_baseline"], row["upload_absence_seen_at"]))
    conn.commit()
    if cur.rowcount:
        log.info("upload.recovery.absence_confirmed placement=%s; a new attempt is permitted", row["id"])
    store.require_resolved_uploads(conn, device_id)
