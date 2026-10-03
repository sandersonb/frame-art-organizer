"""FastAPI + HTMX web UI: upload, gallery/view, delete, categorize. LAN-only.

Photos POSTed here run the same `ingest.ingest_file` core as the folder scanner, then
render. Delete soft-deletes the asset and (if the TV is reachable) removes its resident
placements; otherwise the scheduler evicts them on the next refresh.
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

import jinja2
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from . import badges, config, db, images, ingest, store
from .frame_client import FrameAsleep, FrameClient

_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=jinja2.select_autoescape(["html"]),
)


def _render(name: str, **ctx) -> str:
    return _ENV.get_template(name).render(**ctx)


def create_app(config_path: str | None = None, run_daemon: bool | None = None) -> FastAPI:
    cfg_path = config_path or config.DEFAULT_CONFIG_PATH
    cfg = config.load(cfg_path)
    base = Path(cfg_path).resolve().parent
    paths = config.paths(cfg, base)
    for key in ("inbox", "originals", "derivatives"):
        paths[key].mkdir(parents=True, exist_ok=True)
    thumbs_dir = paths["derivatives"].parent / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    _boot = db.open_db(paths["database"])  # ensure migrated
    store.ensure_collection_colors(_boot)  # backfill colors for pre-migration collections
    _boot.close()

    img = config.image_settings(cfg)   # validated: a bad [image] value stops the server at startup
    fit, pipeline_version, quality = img.default_fit, img.pipeline_version, img.jpeg_quality

    if run_daemon is None:
        run_daemon = bool(cfg.get("daemon", {}).get("enabled", False))

    @asynccontextmanager
    async def lifespan(_app):
        daemon = None
        if run_daemon:
            from .daemon import Daemon
            daemon = Daemon(cfg, base)
            threading.Thread(target=daemon.run, name="frameart-daemon", daemon=True).start()
        yield
        if daemon:
            daemon.stop()

    app = FastAPI(title="Frame Art Organizer", lifespan=lifespan)

    def conn():
        return db.connect(paths["database"])

    def soft_delete(c, asset_id: int) -> None:
        """Remove an asset's resident placements (if the TV is reachable) then soft-delete it."""
        placements = store.present_placements_for_asset(c, asset_id)
        if placements:
            try:
                with FrameClient(config.frame_config(cfg, base)) as fc:
                    for p in placements:
                        if p["content_id"]:
                            try:
                                fc.delete(p["content_id"])
                            except Exception:  # noqa: BLE001
                                pass
                        store.set_placement_deleted(c, p["id"])
                        store.add_rotation_event(c, p["device_id"], asset_id, "removed", "web-delete")
            except FrameAsleep:
                pass  # leave placements; the scheduler evicts on next refresh
        store.mark_asset_deleted(c, asset_id)

    def look_of(a) -> badges.Look:
        """The photo's badges, from its source size, shown derivative and harvested matte."""
        return badges.describe(a["width"], a["height"], a["d_width"], a["d_height"], a["d_fit"],
                               a["pref_matte"], a["pref_shape"], img)

    def gallery_assets(c, filt, sort):
        """Gallery rows with their collections (name+color) and badges attached for rendering."""
        out = []
        for a in store.list_gallery(c, filt, sort, fit, pipeline_version):
            d = dict(a)
            d["collections"] = [dict(x) for x in store.collections_for_asset(c, a["id"])]
            d["look"] = look_of(a)
            out.append(d)
        return out

    @app.get("/", response_class=HTMLResponse)
    def index(filter: str = "all", sort: str = "newest", view: str = "grid"):
        c = conn()
        try:
            assets = gallery_assets(c, filter, sort)
            counts = store.counts_by_filter(c)
        finally:
            c.close()
        return HTMLResponse(_render("index.html", assets=assets, counts=counts,
                                    active_filter=filter, sort=sort, view=view))

    @app.get("/gallery", response_class=HTMLResponse)
    def gallery(filter: str = "all", sort: str = "newest", view: str = "grid"):
        c = conn()
        try:
            assets = gallery_assets(c, filter, sort)
        finally:
            c.close()
        return HTMLResponse(_render("_gallery.html", assets=assets, view=view))

    @app.get("/filterbar", response_class=HTMLResponse)
    def filterbar():
        c = conn()
        try:
            counts = store.counts_by_filter(c)
        finally:
            c.close()
        return HTMLResponse(_render("_filterbar.html", counts=counts))

    @app.post("/upload")
    async def upload(files: list[UploadFile] = File(...), collections: str = Form("")):
        c = conn()
        try:
            col_ids = [store.get_or_create_collection(c, n.strip())
                       for n in collections.split(",") if n.strip()]
            for f in files:
                if not f.filename or Path(f.filename).suffix.lower() not in images.SUPPORTED_EXTS:
                    continue
                tmp = paths["inbox"] / Path(f.filename).name
                tmp.write_bytes(await f.read())
                try:
                    kind, asset_id = ingest.ingest_file(c, paths, tmp)
                    for cid in col_ids:
                        store.assign_collection(c, asset_id, cid)
                finally:
                    tmp.unlink(missing_ok=True)  # original is archived by ingest
            ingest.render_pending(c, paths, fit, pipeline_version, quality,
                                  crop_tolerance=img.crop_tolerance)
        finally:
            c.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/asset/{asset_id}/collection")
    def add_collection(asset_id: int, name: str = Form(...)):
        c = conn()
        try:
            store.assign_collection(c, asset_id, store.get_or_create_collection(c, name.strip()))
        finally:
            c.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/asset/{asset_id}/delete")
    def delete_asset(asset_id: int):
        c = conn()
        try:
            soft_delete(c, asset_id)
        finally:
            c.close()
        return Response(status_code=200)  # HTMX removes the card

    @app.get("/thumb/{asset_id}")
    def thumb(asset_id: int):
        c = conn()
        try:
            a = store.get_asset(c, asset_id)
            deriv = store.get_any_derivative(c, asset_id, fit, pipeline_version) if a else None
        finally:
            c.close()
        if a is None:
            raise HTTPException(404)
        dest = thumbs_dir / images.thumb_name(a["sha256"], deriv["id"] if deriv else None)
        if not dest.exists():
            src = deriv["path"] if deriv else a["original_path"]
            try:
                images.make_thumbnail(src, dest)
            except Exception:  # noqa: BLE001 - undecodable original
                raise HTTPException(415)
        return FileResponse(dest)

    # --- organizer endpoints (ORGANIZER_UI_SPEC.md step 1) ---
    @app.post("/collection")
    def create_collection(name: str = Form(...), color: str = Form(None)):
        c = conn()
        try:
            store.get_or_create_collection(c, name.strip(), color=color)
        finally:
            c.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/asset/{asset_id}/rename")
    def rename(asset_id: int, title: str = Form("")):
        c = conn()
        try:
            store.rename_asset(c, asset_id, title)
        finally:
            c.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/asset/{asset_id}/collection/remove")
    def remove_collection(asset_id: int, collection_id: int = Form(...)):
        c = conn()
        try:
            store.remove_from_collection(c, asset_id, collection_id)
        finally:
            c.close()
        return RedirectResponse("/", status_code=303)

    @app.get("/asset/{asset_id}")
    def details(asset_id: int):
        c = conn()
        try:
            a = store.get_asset(c, asset_id)
            if a is None:
                raise HTTPException(404)
            deriv = store.get_any_derivative(c, asset_id, fit, pipeline_version)
            cols = [dict(r) for r in store.collections_for_asset(c, asset_id)]
            placements = store.present_placements_for_asset(c, asset_id)
        finally:
            c.close()
        return {
            "id": a["id"], "original_name": a["original_name"], "title": a["title"],
            "status": a["status"], "width": a["width"], "height": a["height"],
            "bytes": a["bytes"], "mime": a["mime"], "captured_at": a["captured_at"],
            "imported_at": a["imported_at"], "sha256": a["sha256"], "collections": cols,
            "on_frame": [p["content_id"] for p in placements],
            "has_derivative": deriv is not None,
            "derivative_id": deriv["id"] if deriv else None,
        }

    @app.get("/asset/{asset_id}/download")
    def download(asset_id: int):
        c = conn()
        try:
            a = store.get_asset(c, asset_id)
        finally:
            c.close()
        if a is None:
            raise HTTPException(404)
        return FileResponse(a["original_path"], filename=a["original_name"] or f"asset-{asset_id}")

    def _ids(raw: str) -> list[int]:
        return [int(x) for x in raw.replace(",", " ").split() if x.strip().isdigit()]

    @app.post("/bulk/collection")
    def bulk_collection(asset_ids: str = Form(...), name: str = Form(...)):
        c = conn()
        try:
            cid = store.get_or_create_collection(c, name.strip())
            ids = _ids(asset_ids)
            for aid in ids:
                store.assign_collection(c, aid, cid)
        finally:
            c.close()
        return {"assigned": len(ids), "collection_id": cid}

    @app.post("/bulk/delete")
    def bulk_delete(asset_ids: str = Form(...)):
        c = conn()
        try:
            ids = _ids(asset_ids)
            for aid in ids:
                soft_delete(c, aid)
        finally:
            c.close()
        return {"deleted": len(ids)}

    return app
