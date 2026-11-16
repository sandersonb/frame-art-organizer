"""Persist the desired working set across bounded ticks, sleep and process restarts."""
from __future__ import annotations

import json

from . import store


def key(period, fit_mode, pipeline_version):
    settings = {k: period[k] for k in ("name", "collections", "interval", "shuffle", "set_size", "no_repeat_days")}
    settings["collections"] = sorted(settings["collections"])
    return json.dumps([settings, fit_mode, pipeline_version], sort_keys=True)


def active(conn, device_id):
    return conn.execute("SELECT * FROM refresh_batch WHERE device_id=? AND state='active'", (device_id,)).fetchone()


def desired_rows(conn, batch, period):
    rows = []
    for did in json.loads(batch["desired"]):
        row = conn.execute("""SELECT d.id AS derivative_id, d.path, d.width, d.height,
                                     a.id AS asset_id, a.original_name, a.captured_at,
                                     COALESCE(ap.pinned,0) AS pinned,
                                     ap.matte AS pref_matte, ap.matte_shape AS pref_shape
                              FROM derivative d JOIN asset a ON a.id=d.asset_id
                              LEFT JOIN asset_policy ap ON ap.asset_id=a.id
                              WHERE d.id=? AND a.status='active' AND COALESCE(ap.suppressed,0)=0""", (did,)).fetchone()
        if row is None:
            return None
        if period["collections"]:
            names = {c["name"] for c in store.collections_for_asset(conn, row["asset_id"])}
            if not names.intersection(period["collections"]):
                return None
        rows.append(row)
    return rows


def start(conn, device_id, plan_key, desired):
    # One atomic statement, so a concurrent refresh cannot replace an active plan.
    conn.execute("""INSERT INTO refresh_batch(device_id,plan_key,desired,state,created_at)
                    SELECT ?,?,?,'active',? WHERE NOT EXISTS
                    (SELECT 1 FROM refresh_batch WHERE device_id=? AND state='active')""",
                 (device_id, plan_key, json.dumps([r["derivative_id"] for r in desired]), store.now(), device_id))
    conn.commit()
    return active(conn, device_id)


def finish(conn, batch_id, *, superseded=False):
    conn.execute("UPDATE refresh_batch SET state=?,completed_at=? WHERE id=? AND state='active'",
                 ("superseded" if superseded else "complete", store.now(), batch_id))
    conn.commit()


def for_host(conn, host, *, state, plan_key=None):
    where, args = "dev.host=? AND b.state=?", [host, state]
    if plan_key is not None:
        where += " AND b.plan_key=?"
        args.append(plan_key)
    return conn.execute(f"""SELECT b.* FROM refresh_batch b JOIN device dev ON dev.id=b.device_id
                            WHERE {where} ORDER BY b.id DESC LIMIT 1""", args).fetchone()
