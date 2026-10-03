"""`fao` command-line interface — drive the Frame with the proven core.

Subcommands:
  ping      is the TV reachable right now?
  info      device + art-api summary
  current   what's on the wall
  list      your uploaded photos (My Photos)
  push      normalize a local image and upload it (optionally display it)
  show      display an already-uploaded content_id
  delete    remove an uploaded content_id

A sleeping TV exits with code 2 and a clear message (not a crash), so cron/systemd
can tell "asleep, try later" apart from real failures.
"""
from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

import urllib3

from . import config, db, images, ingest, scheduler, store, uploader
from . import harvest as harvest_mod
from .frame_client import FrameAsleep, FrameClient

# The Frame's 8002 cert is self-signed; we intentionally don't verify it. Quiet the noise.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EXIT_ASLEEP = 2


def _client(args) -> tuple[FrameClient, dict]:
    cfg = config.load(args.config)
    base_dir = Path(args.config).resolve().parent
    return FrameClient(config.frame_config(cfg, base_dir)), cfg


def _library(args):
    """Open the database + resolved paths + config for library commands."""
    cfg = config.load(args.config)
    base_dir = Path(args.config).resolve().parent
    paths = config.paths(cfg, base_dir)
    conn = db.open_db(paths["database"])
    return conn, paths, cfg


# ---- library commands (local; no TV) ----
def cmd_db_init(args) -> int:
    conn, paths, _ = _library(args)
    version = db.migrate(conn)
    print(f"database      : {paths['database']}")
    print(f"schema version: {version}")
    c = store.counts(conn)
    print(f"assets {c['assets']} (active {c['active']}, broken {c['broken']}), "
          f"derivatives {c['derivatives']}, placed {c['placed']}")
    return 0


def cmd_ingest(args) -> int:
    conn, paths, cfg = _library(args)
    img = cfg.get("image", {})
    for key in ("inbox", "originals", "derivatives"):
        paths[key].mkdir(parents=True, exist_ok=True)
    s = ingest.scan(conn, paths)
    rendered = ingest.render_pending(
        conn, paths,
        fit_mode=img.get("default_fit", "cover"),
        pipeline_version=int(img.get("pipeline_version", 1)),
        quality=int(img.get("jpeg_quality", 92)),
    )
    print(f"ingest: new={s['new']} dup={s['dup']} broken={s['broken']}; rendered={rendered}")
    return 0


def cmd_assets(args) -> int:
    conn, _, _ = _library(args)
    rows = store.list_assets(conn)
    if not rows:
        print("(no assets — drop images into the inbox and run `fao ingest`)")
        return 0
    for a in rows:
        dims = f"{a['width']}x{a['height']}" if a["width"] else "?"
        print(f"{a['id']:>4}  {a['sha256'][:10]}  {a['status']:<7} {dims:<11} "
              f"deriv={a['derivatives']}  taken={a['captured_at'] or '-'}  {a['original_name']}")
    return 0


def _ensure_device(conn, fc) -> int:
    info = fc.device_info().get("device", {})
    return store.ensure_device(
        conn, name=html.unescape(info.get("name", "Frame")), host=fc.cfg.host,
        duid=info.get("duid"), api_version=fc.api_version(),
    )


def cmd_sync(args) -> int:
    conn, _, cfg = _library(args)
    fc, _ = _client(args)
    img = config.image_settings(cfg)
    with fc:
        device_id = _ensure_device(conn, fc)
        s = uploader.sync(
            fc, conn, device_id=device_id,
            fit_mode=img.default_fit, pipeline_version=img.pipeline_version,
            matte_cfg=img.matte,
        )
        r = uploader.reconcile(fc, conn, device_id)
    print(f"sync: uploaded={s['uploaded']} errors={s['errors']}")
    print(f"reconcile: on_device={r['on_device']} ours_present={r['ours_present']} "
          f"vanished={r['vanished']} orphans={len(r['orphans'])} "
          f"matte_edits={r['harvested']}")
    if r["harvest_error"]:
        print(f"  WARNING: matte harvest failed ({r['harvest_error']}); see the log")
    if r["orphans"]:
        print(f"  flagged orphans (not ours, left alone): {r['orphans']}")
    return 0


def cmd_reconcile(args) -> int:
    conn, _, _ = _library(args)
    fc, _ = _client(args)
    with fc:
        device_id = _ensure_device(conn, fc)
        r = uploader.reconcile(fc, conn, device_id)
    print(f"reconcile: on_device={r['on_device']} ours_present={r['ours_present']} "
          f"vanished={r['vanished']} orphans={len(r['orphans'])} "
          f"matte_edits={r['harvested']}")
    if r["harvest_error"]:
        print(f"  WARNING: matte harvest failed ({r['harvest_error']}); see the log")
    if r["orphans"]:
        print(f"  flagged orphans: {r['orphans']}")
    return 0


def _print_matte_changes(changes, *, dry_run: bool) -> None:
    verb = "would record" if dry_run else "recorded"
    if not changes:
        print("harvest: no matte edits found — the TV matches what we last uploaded")
        return
    print(f"harvest: {verb} {len(changes)} matte edit(s) made on the TV:")
    for c in changes:
        print(f"  asset={c['asset_id']:<4} {c['content_id']:<10} {c['old']} -> {c['new']:<22} "
              f"shape={c['shape']}  {c['original_name']}")
    if dry_run:
        print("(dry run: nothing was written; run without --dry-run to record them)")


def cmd_harvest(args) -> int:
    """Read back matte edits made in the TV's own UI and remember them per asset."""
    conn, _, _ = _library(args)
    fc, _ = _client(args)
    with fc:
        device_id = _ensure_device(conn, fc)
        changes = harvest_mod.harvest(fc, conn, device_id, apply=not args.dry_run)
    _print_matte_changes(changes, dry_run=args.dry_run)
    return 0


def cmd_placements(args) -> int:
    conn, _, cfg = _library(args)
    fc, _ = _client(args)
    with fc:
        device_id = _ensure_device(conn, fc)
    for p in store.list_placements(conn, device_id):
        print(f"{p['id']:>4}  {p['state']:<17} {str(p['content_id'] or '-'):<10} "
              f"matte={p['matte']:<6} asset={p['asset_id']}  {p['original_name']}")
    return 0


# ---- collections & policy (local) ----
def cmd_collections(args) -> int:
    conn, _, _ = _library(args)
    rows = store.list_collections(conn)
    if not rows:
        print("(no collections)")
        return 0
    for c in rows:
        print(f"{c['id']:>3}  {c['name']:<20} {c['kind']:<7} members={c['members']}")
    return 0


def cmd_collection_add(args) -> int:
    conn, _, _ = _library(args)
    cid = store.get_or_create_collection(conn, args.name)
    print(f"collection '{args.name}' id={cid}")
    return 0


def cmd_collection_assign(args) -> int:
    conn, _, _ = _library(args)
    cid = store.get_or_create_collection(conn, args.collection)
    store.assign_collection(conn, args.asset_id, cid)
    print(f"asset {args.asset_id} → '{args.collection}'")
    return 0


def cmd_policy(args) -> int:
    conn, _, _ = _library(args)
    store.set_policy(conn, args.asset_id, pinned=args.pin, suppressed=args.suppress,
                     weight=args.weight)
    print(f"policy updated for asset {args.asset_id}")
    return 0


# ---- scheduler ----
def _active_period(cfg):
    return config.active_period(config.schedule(cfg))


def cmd_schedule_show(args) -> int:
    conn, _, cfg = _library(args)
    img = cfg.get("image", {})
    period = _active_period(cfg)
    dev = conn.execute("SELECT id FROM device ORDER BY id LIMIT 1").fetchone()
    device_id = dev["id"] if dev else 0
    desired, to_add, to_remove, present = scheduler.plan(
        conn, device_id=device_id, period=period,
        fit_mode=img.get("default_fit", "cover"),
        pipeline_version=int(img.get("pipeline_version", 1)),
    )
    print(f"active period : {period['name']}  collections={period['collections']}")
    print(f"slideshow     : interval={period['interval']}m shuffle={period['shuffle']} "
          f"set_size={period['set_size']} no_repeat_days={period['no_repeat_days']}")
    print(f"desired set ({len(desired)}):")
    for r in desired:
        print(f"  asset={r['asset_id']:<4} pinned={r['pinned']}  {r['original_name']}")
    print(f"currently resident={len(present)} → would add {len(to_add)}, remove {len(to_remove)}")
    return 0


def cmd_schedule_refresh(args) -> int:
    conn, _, cfg = _library(args)
    fc, _ = _client(args)
    img = config.image_settings(cfg)
    period = _active_period(cfg)
    with fc:
        device_id = _ensure_device(conn, fc)
        res = scheduler.refresh(
            fc, conn, device_id=device_id, period=period,
            fit_mode=img.default_fit, pipeline_version=img.pipeline_version,
            matte_cfg=img.matte,
        )
    print(f"refresh[{period['name']}]: desired={res['desired']} added={res['added']} "
          f"removed={res['removed']} errors={res['errors']} matte_edits={res['harvested']}; "
          f"slideshow {res['interval']}m shuffle={res['shuffle']}")
    if res["removals_skipped"]:
        print("  WARNING: matte harvest failed, so evictions were skipped this run; see the log")
    return 0


def _setup_logging() -> None:
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_serve(args) -> int:
    try:
        import uvicorn
    except ImportError:
        print("web deps not installed — run:  pip install -e '.[web]'", file=sys.stderr)
        return 1
    _setup_logging()
    from .web import create_app

    app = create_app(args.config, run_daemon=args.daemon)
    print(f"Frame Art Organizer → http://{args.host}:{args.port}  (LAN-only)")
    uvicorn.run(app, host=args.host, port=int(args.port))
    return 0


def cmd_daemon(args) -> int:
    _setup_logging()
    cfg = config.load(args.config)
    base = Path(args.config).resolve().parent
    from .daemon import run_forever
    run_forever(cfg, base, tick=args.tick)  # blocks until Ctrl-C
    return 0


def cmd_ping(args) -> int:
    fc, _ = _client(args)
    reachable = fc.is_reachable()
    print("reachable (awake)" if reachable else "unreachable (off/asleep)")
    return 0 if reachable else EXIT_ASLEEP


def cmd_info(args) -> int:
    fc, _ = _client(args)
    with fc:
        dev = fc.device_info().get("device", {})
        print(f"name        : {html.unescape(dev.get('name', ''))}")
        print(f"model       : {dev.get('modelName')}  ({dev.get('model')})")
        print(f"power       : {dev.get('PowerState')}")
        print(f"resolution  : {dev.get('resolution')}")
        print(f"art api     : {fc.api_version()}")
        print(f"art mode    : {fc.artmode()}")
        print(f"my photos   : {len(fc.list_my_photos())}")
    return 0


def cmd_current(args) -> int:
    fc, _ = _client(args)
    with fc:
        cur = fc.current()
    print(f"{cur.get('content_id')}  "
          f"(category={cur.get('category_id')}, type={cur.get('content_type')}, "
          f"matte={cur.get('matte_id')})")
    return 0


def cmd_list(args) -> int:
    fc, _ = _client(args)
    with fc:
        photos = fc.list_my_photos()
    if not photos:
        print("(no uploaded photos)")
        return 0
    for p in photos:
        print(f"{p.get('content_id'):<12} {p.get('width')}x{p.get('height')}  "
              f"matte={p.get('matte_id')}  date={p.get('image_date') or '-'}")
    return 0


def cmd_push(args) -> int:
    fc, cfg = _client(args)
    imgcfg = cfg.get("image", {})
    data = images.normalize_to_frame(
        args.file,
        mode=args.fit or imgcfg.get("default_fit", "cover"),
        quality=int(imgcfg.get("jpeg_quality", 92)),
    )
    print(f"normalized {args.file} -> {len(data):,} bytes ({images.FRAME_W}x{images.FRAME_H})")
    with fc:
        cid = fc.upload_jpeg(data, matte=args.matte or imgcfg.get("default_matte", "none"))
        print(f"uploaded -> {cid}")
        if args.show:
            fc.select(cid, show=True)
            print(f"now displaying {cid}")
    return 0


def cmd_show(args) -> int:
    fc, _ = _client(args)
    with fc:
        fc.select(args.content_id, show=True)
    print(f"displaying {args.content_id}")
    return 0


def cmd_delete(args) -> int:
    fc, _ = _client(args)
    with fc:
        ok = fc.delete(args.content_id)
    print(f"deleted {args.content_id}: {ok}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fao", description="Frame Art Organizer")
    p.add_argument("--config", default=str(config.DEFAULT_CONFIG_PATH),
                   help="path to config.toml")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="is the TV reachable?").set_defaults(func=cmd_ping)
    sub.add_parser("info", help="device + art-api summary").set_defaults(func=cmd_info)
    sub.add_parser("current", help="what's on the wall").set_defaults(func=cmd_current)
    sub.add_parser("list", help="uploaded photos").set_defaults(func=cmd_list)

    pp = sub.add_parser("push", help="normalize + upload a local image")
    pp.add_argument("file")
    pp.add_argument("--fit", choices=["cover", "contain"], help="override fit mode")
    pp.add_argument("--matte", help="override Frame matte (e.g. none, modern)")
    pp.add_argument("--show", action="store_true", help="display it after upload")
    pp.set_defaults(func=cmd_push)

    ps = sub.add_parser("show", help="display an uploaded content_id")
    ps.add_argument("content_id")
    ps.set_defaults(func=cmd_show)

    pd = sub.add_parser("delete", help="remove an uploaded content_id")
    pd.add_argument("content_id")
    pd.set_defaults(func=cmd_delete)

    # library (local; no TV)
    sub.add_parser("db-init", help="create/migrate the database").set_defaults(func=cmd_db_init)
    sub.add_parser("ingest", help="scan inbox → assets → derivatives").set_defaults(func=cmd_ingest)
    sub.add_parser("assets", help="list the library").set_defaults(func=cmd_assets)

    # placement (needs the TV)
    sub.add_parser("sync", help="upload derivatives to the Frame + reconcile").set_defaults(func=cmd_sync)
    sub.add_parser("reconcile", help="diff DB placements vs the Frame").set_defaults(func=cmd_reconcile)
    ph = sub.add_parser("harvest", help="read back matte edits made on the TV and remember them")
    ph.add_argument("--dry-run", action="store_true", help="show what would be recorded; write nothing")
    ph.set_defaults(func=cmd_harvest)
    sub.add_parser("placements", help="list recorded placements").set_defaults(func=cmd_placements)

    # collections & policy (local)
    sub.add_parser("collections", help="list collections").set_defaults(func=cmd_collections)
    pca = sub.add_parser("collection-add", help="create a collection")
    pca.add_argument("name")
    pca.set_defaults(func=cmd_collection_add)
    pcx = sub.add_parser("collection-assign", help="assign an asset to a collection")
    pcx.add_argument("asset_id", type=int)
    pcx.add_argument("collection")
    pcx.set_defaults(func=cmd_collection_assign)
    pol = sub.add_parser("policy", help="set an asset's pin/suppress/weight")
    pol.add_argument("asset_id", type=int)
    pol.add_argument("--pin", dest="pin", action="store_true", default=None)
    pol.add_argument("--no-pin", dest="pin", action="store_false")
    pol.add_argument("--suppress", dest="suppress", action="store_true", default=None)
    pol.add_argument("--no-suppress", dest="suppress", action="store_false")
    pol.add_argument("--weight", type=float, default=None)
    pol.set_defaults(func=cmd_policy)

    # scheduler
    sub.add_parser("schedule-show", help="show active period + desired set (dry run)").set_defaults(func=cmd_schedule_show)
    sub.add_parser("schedule-refresh", help="compose the set, push to the TV, set slideshow").set_defaults(func=cmd_schedule_refresh)

    # web UI + daemon
    ps = sub.add_parser("serve", help="run the web UI (+ scheduler daemon per config)")
    ps.add_argument("--host", default="0.0.0.0")
    ps.add_argument("--port", default=8080, type=int)
    ps.add_argument("--daemon", dest="daemon", action="store_true", default=None,
                    help="force-enable the background scheduler")
    ps.add_argument("--no-daemon", dest="daemon", action="store_false",
                    help="disable the background scheduler")
    ps.set_defaults(func=cmd_serve)
    pdm = sub.add_parser("daemon", help="run the scheduler loop headless (no web UI)")
    pdm.add_argument("--tick", type=float, default=None, help="seconds between ticks")
    pdm.set_defaults(func=cmd_daemon)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FrameAsleep as e:
        print(f"TV asleep: {e}", file=sys.stderr)
        return EXIT_ASLEEP


if __name__ == "__main__":
    sys.exit(main())
