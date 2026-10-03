"""Purge superseded derivatives (SPEC.md §12.8, M4): after the soak, the old pipeline's renders
are dead weight — at 3840x2160 they are the bulk of the library's disk use.

A derivative is purged only if ALL of these hold:
  * it is not at the configured (fit_mode, pipeline_version), and is not from a NEWER pipeline
    (after a rollback the renders you rolled back from are kept);
  * its photo has a derivative at the configured pipeline, and that file exists on disk;
  * that replacement is at least `min_age_days` old — the soak, while the new renders are still
    unproven and a rollback might want the old ones;
  * nothing on the TV (present) or about to go there (pending) uses it;
  * its file lives inside the derivatives directory, so a bad path in the DB can never delete
    something else.
The whole purge is refused while any photo on the TV is still on an old pipeline (the migration
is not finished). Originals are never touched, so a rollback after a purge costs a re-render
(`render_pending` rebuilds from the archived originals), not data.

Order matters: the DB transaction commits first, files are unlinked after. A crash in between
leaves harmless orphan files, never rows pointing at nothing.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import images, store

log = logging.getLogger(__name__)


@dataclass
class Plan:
    eligible: list[dict] = field(default_factory=list)   # {id, asset_id, path, bytes}
    too_new: int = 0              # replacement is younger than the soak
    replacement_missing: int = 0  # the replacement's file is not on disk
    outside_dir: int = 0          # the file is not inside the derivatives directory
    newer_kept: int = 0           # from a newer pipeline than configured (rolled back?)
    stale_resident: int = 0       # placements on the TV still on an old pipeline -> blocks the purge

    @property
    def bytes(self) -> int:
        return sum(c["bytes"] for c in self.eligible)


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def build_plan(conn: sqlite3.Connection, derivs_dir: Path, fit_mode: str, pipeline_version: int,
               min_age_days: int, now: datetime | None = None) -> Plan:
    """What a purge would delete, and why it would keep the rest. Pure reads."""
    now = now or datetime.now(timezone.utc)
    plan = Plan(stale_resident=store.stale_resident_count(conn, fit_mode, pipeline_version),
                newer_kept=store.newer_derivative_count(conn, pipeline_version))
    for r in store.superseded_derivatives(conn, fit_mode, pipeline_version):
        path = Path(r["path"])
        if not _inside(path, derivs_dir):
            log.warning("derivative %s: %s is outside %s — not touching it", r["id"], path, derivs_dir)
            plan.outside_dir += 1
            continue
        if not Path(r["replacement_path"]).is_file():
            plan.replacement_missing += 1
            continue
        rendered = datetime.fromisoformat(r["replacement_rendered_at"])
        if rendered.tzinfo is None:                      # store.now() writes UTC; tolerate a hand-edited one
            rendered = rendered.replace(tzinfo=timezone.utc)
        if (now - rendered).days < min_age_days:
            plan.too_new += 1
            continue
        plan.eligible.append({"id": r["id"], "asset_id": r["asset_id"], "path": str(path),
                              "bytes": path.stat().st_size if path.is_file() else 0})
    return plan


def apply_plan(conn: sqlite3.Connection, plan: Plan) -> dict:
    """Delete the planned derivatives: rows first (one transaction — all or nothing), then files."""
    if plan.stale_resident:
        raise RuntimeError("refusing to purge while photos on the TV are on an old pipeline")
    history = 0
    with conn:
        for c in plan.eligible:
            history += store.purge_derivative(conn, c["id"])
    freed, errors = 0, []
    for c in plan.eligible:
        try:
            p = Path(c["path"])
            if p.is_file():
                p.unlink()
                freed += c["bytes"]
        except OSError as e:
            errors.append((c["path"], str(e)))
            log.warning("could not remove %s: %s", c["path"], e)
    return {"derivatives": len(plan.eligible), "freed_bytes": freed, "history_rows": history,
            "file_errors": errors}


def thumb_orphans(conn: sqlite3.Connection, thumbs_dir: Path) -> list[Path]:
    """Cached thumbnails nothing refers to any more: previews of purged derivatives, and the old
    `<sha>.jpg` files from before thumbnails were keyed by derivative. All regenerable."""
    if not thumbs_dir.is_dir():
        return []
    keep: set[str] = set()
    for r in conn.execute("SELECT a.sha256 AS sha, d.id AS did FROM derivative d JOIN asset a ON a.id = d.asset_id"):
        keep.add(images.thumb_name(r["sha"], r["did"]))
    for r in conn.execute("SELECT sha256 FROM asset"):
        keep.add(images.thumb_name(r["sha256"], None))
    return sorted(p for p in thumbs_dir.glob("*.jpg") if p.name not in keep)
