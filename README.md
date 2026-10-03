# Frame Art Organizer

A small, self-hosted service that turns a photo library into a curated, self-cycling art
display on a **Samsung The Frame** TV. You drop photos into a web UI, organize them into
collections, and a scheduler keeps the Frame's **native Art-Mode slideshow** rotating
through them on whatever schedule you like — themed by day of week and time of day.

The Pi (or any always-on host) owns the library and the scheduling; the TV keeps owning
Art Mode (matte, ambient brightness, motion/night sleep). We drive the Frame's local Art
API rather than piping HDMI, so its low-power behavior stays intact — and because the Art
API is unreachable while the panel sleeps, the scheduler **can't wake the room**.

- **[SAMSUNG_FRAME_API.md](SAMSUNG_FRAME_API.md)** — the reverse-engineered Frame Art API
  (verified on a QN65LS03FAFXZA, API `5.0.1.0`): auth, power-state behavior, operations, caveats.
- **[SPEC.md](SPEC.md)** — architecture, the SQLite schema, components, process model.
- **[ORGANIZER_UI_SPEC.md](ORGANIZER_UI_SPEC.md)** — the web UI design.

## Status
Complete and verified end-to-end on real hardware: feasibility → library/ingest →
scheduler (live rotation proven) → web UI (full organizer) → the always-on daemon +
systemd packaging.

---

## How it works

```
   browser/phone  ─upload→  ┌──────────── Frame Art Organizer (one process) ───────────┐
                            │  ingest: hash → dedupe → preserve original → render 4K   │
   collections, filters ────│  web UI (FastAPI + HTMX, LAN-only)                       │
                            │  daemon: every tick, if the TV is awake and a refresh is │
                            │    due → compose the working set → upload the delta to   │
                            │    My Photos → set the native slideshow                  │
                            └───────────────────────┬──────────────────────────────────┘
                                                    │  local Art API (reachability-gated)
                                                    ▼
                                         Samsung The Frame — cycles My Photos itself
```

Two loops at different speeds: the **host** curates the set slowly (on schedule, only when
the TV is reachable); the **TV** cycles that set every few minutes on its own — so it keeps
running even if the host is offline, and honors the remote's "next".

---

## Concepts

- **Asset** — one imported photo. Identified by the SHA-256 of its original bytes, so
  re-uploading the same file (even renamed) is a no-op. Originals are preserved in a
  content-addressed archive; a Frame-ready 3840×2160 JPEG ("derivative") is rendered from it.
- **Collection** — a colored label you group photos with (`family`, `landscapes`, …). A
  photo can be in many. Used to theme what shows when.
- **Working set** — the subset currently resident in the Frame's *My Photos* and cycling.
  The scheduler composes it (see below) and reconciles the TV toward it.
- **Placement** — the record that a photo is resident on the TV (its device-assigned
  `content_id`). The bridge between the library and the device.
- **Period** — a schedule rule choosing which collections are eligible right now, by day
  of week and/or time of day (see [Configuration](#configuration)).

---

## Quick start (dev, on your workstation)
```sh
python3 -m venv .venv
./.venv/bin/pip install -e '.[web,heic]'      # heic = iPhone photos; web = the UI
cp config.example.toml config.toml             # then set [frame].host to your TV's LAN IP
./.venv/bin/fao db-init                        # create/migrate the SQLite database
./.venv/bin/fao info                           # first connect → accept the Allow prompt on the TV
./.venv/bin/fao serve                          # web UI at http://localhost:8080
```
Open the UI, upload a few photos, then drive a one-off rotation to see it on the wall:
```sh
./.venv/bin/fao schedule-refresh               # compose set → upload → start the slideshow
```

## Deploy on a Raspberry Pi (the appliance)
```sh
git clone <repo> /opt/frame-art-organizer && cd /opt/frame-art-organizer
python3 -m venv .venv && ./.venv/bin/pip install -e '.[web,heic]'
cp config.example.toml config.toml             # then set [frame].host to your TV's IP at minimum
./.venv/bin/fao db-init
./.venv/bin/fao info                           # one-time pairing: accept Allow on the TV
./.venv/bin/fao serve --daemon                 # web UI + scheduler → http://<pi-ip>:8080
```
Run it as a service (edit the paths inside the unit first):
```sh
sudo cp deploy/frame-art-organizer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now frame-art-organizer
journalctl -u frame-art-organizer -f           # watch the scheduler work
```
The unit runs `fao serve --daemon`, so the web UI and the scheduler run together. Bind it
**LAN-only**; there is no authentication in v1 — don't expose it to the internet.

---

## Configuration

All configuration is one file, **`config.toml`**, read at startup. It is **gitignored** (it
holds your TV's LAN address): create it with `cp config.example.toml config.toml`, then edit
it. Paths and the token file are resolved relative to that file's location.

### `[frame]` — the TV connection
```toml
[frame]
host = "192.168.1.XXX"     # the Frame's LAN IP (find it in SmartThings or your router)
port = 8002               # TLS websocket with token auth — leave at 8002
token_file = ".token"     # pairing token, written on first connect. NEVER commit it.
name = "FrameArtOrganizer" # the name shown in the TV's device list
connect_timeout = 3.0     # seconds for the fast "is it awake?" TCP probe before any call
```
The **first** connection pops an *Allow* prompt on the TV — accept it once with the remote
(`fao info` is an easy way to trigger it). The token is then saved to `token_file`.

### `[paths]` — where things live
```toml
[paths]
inbox       = "data/inbox"       # optional watched folder for bulk drops (`fao ingest`)
originals   = "data/originals"   # preserved, content-addressed archive (the keepers)
derivatives = "data/derivatives" # rendered 3840x2160 JPEGs (a regenerable cache)
database    = "data/state.db"    # the SQLite store
```
Primary ingest is the web UI's upload; the `inbox` folder is an optional secondary path.
On a Pi you'd typically point these at persistent storage (e.g. `/srv/frame/...`).
Web thumbnails are cached next to `derivatives` in a `thumbs/` dir.

### `[image]` — how photos are rendered
```toml
[image]
width = 3840              # the panel is native 3840x2160 — leave these
height = 2160
jpeg_quality = 92
default_matte = "none"    # matte for 16:9 photos: "none" fills the panel, else <type>_<color>
default_fit = "cover"     # cover = crop-fill to 16:9; contain = letterbox with bars; auto = v2
pipeline_version = 1      # bump this to force a re-render of every derivative
# v2 pipeline keys — ignored until default_fit = "auto":
crop_tolerance = 0.16     # crop to 16:9 only if that trims <= this (3:2 trims 15.6 %)
auto_matte = true         # keep other photos whole inside a matte (false = the TV crops them)
fit_matte_type = "flexible"   # flexible | shadowbox (verified for non-16:9 photos)
fit_matte_color = "black"
low_res_long_edge = 1280  # plan-report flags photos below this; never blocks
```
**The v2 pipeline** (opt-in) renders at native resolution and **never upscales**: a photo
within `crop_tolerance` of 16:9 is cropped to exactly 16:9; anything else (portraits,
squares, 4:3…) is kept whole and uploaded with a matte so it isn't cropped on the wall.
`fao plan-report` shows exactly what it would do to each photo — review it before switching.
Invalid values (e.g. a matte type the TV can't show for that shape) are rejected at startup.
Note: the Frame's info overlay can show a photo's **date** (taken from EXIF on import) but
**not a custom title** — that's a platform limitation (see SAMSUNG_FRAME_API.md). "Rename"
in the UI sets a local label only.

### `[schedule]` — rotation defaults
```toml
[schedule]
interval_minutes = 15    # how often the TV advances. MUST be one of:
                         #   3, 15, 60, 720, 1440, 10080  (3m,15m,1h,12h,1d,7d)
shuffle = true           # shuffle the set vs. fixed order
set_size = 40            # max photos resident/cycling at once
no_repeat_days = 30      # prefer photos not rotated in within this window
refresh_hours = 24       # recompose the set at least this often (rotates photos over time)
```
`interval_minutes` is a **hard enum** — any other value is rejected by the TV. Selection
picks pinned photos first, then ones not shown recently, then weighted-random up to
`set_size` (photos never starve — recently-shown ones fill in if the pool is small).

### `[[schedule.period]]` — what shows when
Periods choose which **collections** are eligible, by day of week and/or time of day.
A period may set: `name`, `collections` (`[]` = **all photos**), `days`, `start`, and any of
the `[schedule]` keys as an override. **Day-restricted periods win over every-day ones.**

```toml
# Simplest: one period, all photos, always.
[[schedule.period]]
name = "all-day"
collections = []

# Day-of-week example (Mon–Fri / Sat / Sun):
[[schedule.period]]
name = "weekdays"
days = ["mon", "tue", "wed", "thu", "fri"]   # names or mon..sun; omit = every day
collections = ["family", "landscapes"]

[[schedule.period]]
name = "saturday"
days = ["sat"]
collections = ["family", "landscapes", "art"]

[[schedule.period]]
name = "sunday"
days = ["sun"]
collections = ["portraits"]

# Time-of-day (optionally combined with days) — active period is the last whose start passed:
[[schedule.period]]
name = "evening"
start = "18:00"
collections = ["landscapes"]
interval_minutes = 60        # per-period override
```
When the active period's `name` changes (a day/time boundary), the daemon recomposes the
set on its next reachable tick — or on wake if the TV was asleep at the boundary. Inspect
the current decision with `fao schedule-show`.

> If a period's collections have **no photos**, that period composes an empty set; the
> scheduler logs a friendly note and **leaves the Frame as-is** (it won't blank it).

### `[daemon]` — the always-on loop
```toml
[daemon]
enabled = false          # `fao serve` is web-only unless this is true OR you pass --daemon
tick_seconds = 60        # how often the loop checks reachability / whether a refresh is due
harvest_minutes = 15     # while the TV is awake, how often to read back matte edits made on the TV
                         #   (0 = off). See "Matte" below.
```
Safe default: a plain `fao serve` won't touch your Frame. The systemd unit passes
`--daemon` to enable the scheduler on the appliance.

### Matte — change it on the TV
The Frame's own Art-Mode menu is the matte editor, so this app doesn't have one. If you
change a photo's matte on the TV, the app **reads it back** (`fao harvest`, also run
automatically every `harvest_minutes` and at the start of every rotation) and remembers it
per photo. The harvest always runs *before* a photo is rotated off the TV, because deleting a
photo destroys its matte. `fao harvest --dry-run` shows what it would record.

---

## The web UI
LAN-only, mobile-friendly (FastAPI + HTMX). Upload (drag-and-drop or picker, incl. HEIC),
a gallery with grid/list views and sort, **collection filter chips** with live counts,
per-photo menu (add to collection, rename, view details, download, delete), color-coded
**collections** with a swatch picker, and multi-select **bulk** add/delete. Deleting a
photo removes it from the Frame too.

## The `fao` CLI
```
Service   serve [--host --port --daemon/--no-daemon]   daemon [--tick]
Library   db-init   ingest   assets   collections   collection-add <name>
          collection-assign <asset_id> <collection>   policy <asset_id> [--pin --suppress --weight W]
Rotation  schedule-show            schedule-refresh
Frame     ping   info   current   list   sync   reconcile   placements   harvest [--dry-run]
          push <file> [--fit cover|contain --matte M --show]   show <content_id>   delete <content_id>
```
Run `fao --help` (or `fao <cmd> --help`) for details.

---

## Layout
```
frame_art_organizer/
  config.py        config.toml loader (+ schedule period selection)
  db.py            SQLite connection + schema migrations
  store.py         repository layer (assets, collections, placements, selection query)
  images.py        any image → 3840x2160 JPEG; EXIF date + thumbnails
  ingest.py        scan/upload → assets → derivatives
  frame_client.py  reachability-gated, timeout-bounded Art API wrapper
  scheduler.py     compose the working set + reconcile the TV + set the slideshow
  uploader.py      two-phase upload + device reconcile
  daemon.py        the always-on scheduler loop
  web.py           FastAPI app (gallery, upload, collections, bulk, thumbnails)
  templates/       Jinja + HTMX + Alpine; _icons.html = vendored Tabler icons
  cli.py           the `fao` command
scripts/           historical validation/demo scripts (living API examples)
deploy/            systemd unit
config.toml        configuration
SPEC.md  SAMSUNG_FRAME_API.md  ORGANIZER_UI_SPEC.md
```

## Notes & troubleshooting
- **The scheduler only acts when the TV is awake.** Asleep = a clean skip; a boundary
  crossed during sleep is applied on the next wake. This is by design and can't be
  bypassed (the API port is closed while the panel sleeps).
- **`set_slideshow_status ... error number -7`** = the interval isn't in the enum above,
  *or* My Photos is empty (nothing to cycle). Add photos / check the period's collections.
- **First connection hangs then a prompt appears on the TV** — accept *Allow*; the token
  is saved after that.
- **Offline:** htmx/alpine load from a CDN today; vendor them locally for a fully offline Pi.
- **Backups:** the originals archive is the irreplaceable part; `state.db` is largely
  rebuildable, but back it up to keep collections/history/weights.

---

## License
[MIT](LICENSE). This is an independent hobby project and is not affiliated with or
endorsed by Samsung; it drives the TV through an **unofficial**, reverse-engineered local
API (see `SAMSUNG_FRAME_API.md`). "The Frame" and "Samsung" are trademarks of their owners.
