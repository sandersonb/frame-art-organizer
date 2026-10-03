"""Ingest: scan the inbox, record new images as assets, render Frame-ready derivatives.

Ingest is idempotent on the SHA-256 of the original bytes: dropping the same file
twice (or a renamed copy) yields one asset. Originals are copied into a content-addressed
archive and preserved; removing a file from the inbox does NOT remove the asset.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import images, store


def _iter_images(inbox: Path):
    for p in sorted(Path(inbox).rglob("*")):
        if p.is_file() and p.suffix.lower() in images.SUPPORTED_EXTS:
            yield p


def ingest_file(conn: sqlite3.Connection, paths: dict, path: Path) -> tuple[str, int]:
    """Returns (kind, asset_id) where kind is 'new' | 'dup' | 'broken'."""
    data = path.read_bytes()
    sha = images.sha256_bytes(data)

    existing = store.get_asset_by_sha(conn, sha)
    if existing is not None:
        return ("dup", existing["id"])

    try:
        meta = images.read_metadata(path)
        status = "active"
    except Exception:  # undecodable — record it so we don't retry endlessly
        meta = {"width": None, "height": None, "captured_at": None, "mime": None}
        status = "broken"

    # content-addressed archive: originals/<aa>/<sha><ext>
    dest = Path(paths["originals"]) / sha[:2] / f"{sha}{path.suffix.lower()}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(data)

    asset_id = store.insert_asset(
        conn, sha256=sha, original_path=str(dest), original_name=path.name,
        bytes_=len(data), mime=meta["mime"], width=meta["width"], height=meta["height"],
        captured_at=meta["captured_at"], status=status,
    )
    return (status if status == "broken" else "new", asset_id)


def scan(conn: sqlite3.Connection, paths: dict) -> dict:
    summary = {"new": 0, "dup": 0, "broken": 0}
    for p in _iter_images(paths["inbox"]):
        kind, _ = ingest_file(conn, paths, p)
        summary[kind] += 1
    return summary


def render_pending(conn: sqlite3.Connection, paths: dict, fit_mode: str,
                   pipeline_version: int, quality: int) -> int:
    rendered = 0
    for a in store.assets_needing_render(conn, fit_mode, pipeline_version):
        dest = Path(paths["derivatives"]) / f"{a['sha256']}_{fit_mode}_v{pipeline_version}.jpg"
        info = images.render_to_file(a["original_path"], dest, mode=fit_mode, quality=quality)
        store.add_derivative(
            conn, asset_id=a["id"], path=str(dest), sha256=info["sha256"],
            fit_mode=fit_mode, width=info["width"], height=info["height"],
            pipeline_version=pipeline_version,
        )
        rendered += 1
    return rendered
