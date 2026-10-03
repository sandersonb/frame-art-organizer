# Frame Art Organizer — Specification

A local service that turns a shared photo folder into a curated, self-cycling art
display on a Samsung The Frame — while letting the TV keep doing what it's good at
(matte, ambient brightness, motion/night sleep).

See **`SAMSUNG_FRAME_API.md`** for the verified device/API behavior this spec relies on.

---

## 1. Purpose & scope

**Goal.** Family members drop photos into a network share. The service preserves the
originals, renders Frame-ready copies, uploads them to the Frame's *My Photos*, and
keeps a curated rotation cycling on the wall — themed and refreshed over time, with no
manual fiddling.

**Principle.** *The Pi curates; the TV cycles.* We drive the Frame's **native
slideshow** over our uploaded set rather than pushing HDMI or acting as the slideshow
clock. This preserves the Frame's low-power/ambient behavior and the remote's "next".

**In scope (v1):** SMB ingest, image normalization, upload/sync to `MY-C0002`,
slideshow configuration, a reachability-safe scheduler, a CLI, SQLite state.

**Non-goals (v1):** video/animated art; per-image display analytics; face/people
tagging; multi-device; excluding manually-added photos from rotation; managing the
Frame's motion/brightness settings; in-UI TV control (show-now/pin) — deferred.

---

## 2. Locked decisions

| Area | Decision | Rationale |
|---|---|---|
| Host / process | **Raspberry Pi, single `systemd` daemon** | Always-on to catch wake windows; serializes SQLite writes in-process |
| Ingest | **Web UI** (upload/view/delete/categorize); folder scan optional | Works on mobile via a file picker; SMB is awkward on phones |
| Web stack | **FastAPI + HTMX/Jinja**, LAN-only, built after the scheduler | Server-rendered, no build step, Pi-friendly |
| Control model | **Model B:** upload a set, TV runs the slideshow | Robust; survives Pi downtime; honors remote "next"; can't wake the room |
| Rotation scope | **`MY-C0002` (My Photos) only** | No Favourites split in v1 — don't overcomplicate |
| Orphans | **Leave & flag** | Never delete outside content; accept it *may* appear in rotation (category-scoped slideshow) |
| Policy location | **Rules in `config.toml`, overrides in DB** | Config = reviewable policy; DB = runtime facts |
| Store | **SQLite (WAL)** | Embedded, ACID, real query engine for selection; mostly rebuildable |
| Identity | **SHA-256 of original bytes** | `content_id` is TV-assigned & churny — never an identity |
| Titles | **Field kept in model; not set on TV (yet)** | No API/known path today; pending research |
| Pixels *(v2)* | **Native resolution, downscale-only — never upscale** | Upscaling adds no detail and bloats files; the TV scales for the panel itself (API notes §7.2) |
| Fit *(v2)* | **FILL** (centered crop to exact 16:9) when ≤ `crop_tolerance` is trimmed, else **FIT** (native aspect, no crop, no padding) | Near-16:9 stays full-bleed; portraits/4:3/squares show whole instead of a middle band |
| Matte *(v2)* | **The TV owns the matte UX.** The Pi sets an *initial* matte at upload and **harvests** user changes (TV wins) | `change_matte` is broken (`-7`); the TV UI is the editor and read-back works (API notes §7.4–7.5) |
| Matte safety *(v2)* | **Only upload matte types valid for the derivative's shape; no harvest ⇒ no evict** | A mismatched matte crashed the TV (API notes §7.6); eviction destroys the TV-side edit |

---

## 3. Architecture

```
   SMB share (inbox)                         Raspberry Pi (systemd daemon)
   family drops files                 ┌──────────────────────────────────────────┐
        │                             │  ingest → render → upload/reconcile      │
        ▼                             │  scheduler → set slideshow config        │
  ┌───────────┐   preserve   ┌────────┴──────┐   Art API (reachability-gated)    │
  │ originals │◄─────────────│   daemon      │──────────────┐                    │
  └───────────┘   render     │  + SQLite     │              │                    │
  ┌───────────┐              │  (state.db)   │              ▼                    │
  │derivatives│◄─────────────└───────────────┘        Samsung The Frame          │
  └───────────┘                                   MY-C0002 + native slideshow    │
                                                 (cycles every 3/15/60… min)    ─┘
```

Two loops at very different cadences:
- **Pi (slow, deferrable, reachability-gated):** ingest, render, compose the desired
  set, reconcile it onto the TV, set the slideshow config. Runs on ingest events and
  schedule boundaries; skips silently when the TV is asleep.
- **TV (fast, autonomous):** cycles the resident `MY-C0002` set at the configured
  interval. Works with the Pi disconnected; honors the remote.

---

## 4. Data model (SQLite)

**Source-of-truth framing.** The **TV** is authoritative for what physically exists in
`MY-C0002` and what's showing; the **DB** is authoritative for lineage, derivatives,
collections, intent, and (best-effort) rotation history. The two are **reconciled**,
never assumed consistent. Corollary: `content_id` (`MY_Fxxxx`) is a *device-assigned,
mutable, nullable placement attribute* — not identity.

Connection PRAGMAs: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`,
`synchronous=NORMAL`. Schema version via `PRAGMA user_version` (migrations).

```sql
-- The physical TV(s). One row in v1; modeled for future multi-device.
CREATE TABLE device (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,
  host         TEXT NOT NULL,
  duid         TEXT UNIQUE,              -- stable TV UUID across IP changes
  api_version  TEXT,
  last_seen_at TEXT
);

-- System-of-record for a dropped image. Identity = hash of the ORIGINAL bytes.
CREATE TABLE asset (
  id            INTEGER PRIMARY KEY,
  sha256        TEXT NOT NULL UNIQUE,    -- dedup + rename-survival
  original_path TEXT NOT NULL,           -- preserved archive
  original_name TEXT,
  bytes         INTEGER,
  mime          TEXT,
  width         INTEGER,
  height        INTEGER,
  captured_at   TEXT,                    -- EXIF DateTimeOriginal → Frame's shown date
  title         TEXT,                    -- human caption; NOT settable on TV yet (future)
  imported_at   TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','hidden','broken'))
);

-- Runtime, user-driven overrides (the "keep up"/"never show"/"boost" actions).
CREATE TABLE asset_policy (
  asset_id   INTEGER PRIMARY KEY REFERENCES asset(id) ON DELETE CASCADE,
  pinned     INTEGER NOT NULL DEFAULT 0,
  suppressed INTEGER NOT NULL DEFAULT 0,
  weight     REAL    NOT NULL DEFAULT 1.0,
  matte       TEXT,   -- v3 (§12.5): matte the user last chose on the TV (harvested); NULL = no preference
  matte_shape TEXT    -- v3: shape class ('wide'|'odd') it was chosen under; applied only to the same class
);

-- Rendered PIXELS only (regenerable cache). Matte is a display/upload param and lives
-- on `placement`, not here — it doesn't change the pixels, so it must not force a re-render.
CREATE TABLE derivative (
  id               INTEGER PRIMARY KEY,
  asset_id         INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  path             TEXT NOT NULL,        -- JPEG on disk (v1: always 3840x2160; v2: native size, ≤ 3840x2160)
  sha256           TEXT NOT NULL,
  fit_mode         TEXT NOT NULL,        -- v1: cover | contain ; v2: auto (plan resolved per photo, §12.2)
  width            INTEGER,              -- actual rendered size (v2: varies per asset)
  height           INTEGER,
  pipeline_version INTEGER NOT NULL,
  rendered_at      TEXT NOT NULL,
  UNIQUE(asset_id, fit_mode, pipeline_version)
);

-- THE BOUNDARY: a derivative resident in a device's MY-C0002. Only place content_id lives.
CREATE TABLE placement (
  id               INTEGER PRIMARY KEY,
  device_id        INTEGER NOT NULL REFERENCES device(id),
  derivative_id    INTEGER NOT NULL REFERENCES derivative(id),
  content_id       TEXT,                 -- TV-assigned MY_Fxxxx; NULL while pending
  matte            TEXT NOT NULL DEFAULT 'none',  -- matte on the TV item: set at upload, updated by harvest (§12.4)
  state            TEXT NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending','present','deleted_on_device','error')),
  uploaded_at      TEXT,
  last_verified_at TEXT,
  UNIQUE(device_id, content_id)
);
-- At most one live copy of a given render per device.
CREATE UNIQUE INDEX uq_present_placement
  ON placement(device_id, derivative_id) WHERE state = 'present';

-- Best-effort, SET-LEVEL history (the TV drives transitions, so no per-image timing).
CREATE TABLE rotation_event (
  id        INTEGER PRIMARY KEY,
  device_id INTEGER NOT NULL REFERENCES device(id),
  asset_id  INTEGER REFERENCES asset(id),   -- denormalized; survives content_id churn
  event     TEXT NOT NULL CHECK (event IN ('added','removed')),
  at        TEXT NOT NULL,
  rule      TEXT                             -- schedule rule that caused it
);

-- Logical groupings (family / landscapes / december / person:grandma).
CREATE TABLE collection (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL UNIQUE,
  kind      TEXT NOT NULL DEFAULT 'manual' CHECK (kind IN ('manual','smart')),
  rule_json TEXT                             -- for smart/query-defined collections
);
CREATE TABLE asset_collection (
  asset_id      INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
  collection_id INTEGER NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
  PRIMARY KEY (asset_id, collection_id)
);
```

### Invariants
- Ingest is idempotent on `asset.sha256`.
- An asset needs a current `derivative` (matching `pipeline_version`) before placement.
- `content_id` is unique per device; at most one `present` placement per `(device, derivative)`.
- `content_id` is never treated as identity — always reached via `placement`.
- Only the **reconciler** flips `present → deleted_on_device`; the **scheduler** never mutates `placement`.
- *(v2)* A matte is only uploaded if it is valid for the derivative's shape class (§12.3).
- *(v2)* **No harvest, no evict:** a placement is never deleted from the TV in a session
  whose matte harvest did not succeed (§12.4).

### Selection query (composing the desired set)
Because policy is a query, not hand-rolled iteration — this is why SQLite earns its place:

```sql
-- eligible = active, not suppressed, in a target collection, has a current derivative,
-- not rotated-in within the no-repeat window; weighted-random, take N.
SELECT a.id, d.id AS derivative_id
FROM asset a
JOIN asset_collection ac ON ac.asset_id = a.id
JOIN collection c  ON c.id = ac.collection_id AND c.name IN (:collections)
JOIN derivative d  ON d.asset_id = a.id AND d.pipeline_version = :pv
LEFT JOIN asset_policy ap ON ap.asset_id = a.id
WHERE a.status = 'active' AND COALESCE(ap.suppressed, 0) = 0
  AND NOT EXISTS (
    SELECT 1 FROM rotation_event re
    WHERE re.asset_id = a.id AND re.device_id = :dev
      AND re.event = 'added' AND re.at > datetime('now', :no_repeat_window))
ORDER BY -ln(1.0 - random()/9.2e18) / COALESCE(ap.weight, 1.0)   -- weighted sampling
LIMIT :set_size;
```

Pinned assets (`ap.pinned = 1`) are force-included ahead of the sampled remainder.

---

## 5. Components

### 5.1 Frame client (`frame_client.py`) — *built*
Reachability-gated wrapper over `samsungtvws`. Hardening required by the API notes:
- `is_reachable()` (TCP 8002) **and** `is_awake()` (REST `PowerState != standby`) gates.
- **Every art call bounded** by a hard timeout (thread/`SIGALRM`); never call known-hangers (`get_auto_rotation_status`).
- Ops: `device_info, api_version, artmode, current, list_my_photos, upload_jpeg(date=…),
  select, delete, set_slideshow(duration, shuffle)`. All raise `FrameAsleep` when gated out.

### 5.2 Image pipeline (`images.py`) — *v1 built; v2 planned (§12)*
Any input (JPEG/PNG/WebP/TIFF/HEIC) → JPEG, EXIF-transposed first.
- **v1 (current, `pipeline_version = 1`):** always exactly **3840×2160** via `cover`
  (crop-fill) or `contain` (letterbox). That **upscales** anything smaller (×4.8 for an
  800×600) and crops portraits/squares to a middle band (a 2:3 portrait keeps 38 % of its
  pixels).
- **v2 (planned, `pipeline_version = 2`, §12):** **native resolution, downscale-only**,
  with a per-photo plan chosen by a pure function of the image's dimensions — **FILL**
  (centered crop to exact 16:9 when the trim is ≤ `crop_tolerance`) or **FIT** (native
  aspect; no crop, no padding). Matte handling: §12.3.
- `captured_at` (EXIF `DateTimeOriginal`) is carried through so the uploader can pass it
  as the `date` **param** (format `YYYY:MM:DD HH:MM:SS`) → the Frame shows the real
  capture date, not the import date.
**Confirmed 2026-09-23: the Frame reads the upload `date` param, NOT embedded EXIF** —
so it must be passed explicitly; embedding it in the file is ignored. (Titles: no path,
tested & failed — nothing to write.)

### 5.3 Ingest (web upload; optional folder scan)
Photos arrive via an **HTTP upload endpoint** (browser file picker — works on mobile,
accepts HEIC). Each upload runs the same core `ingest_file`: hash → dedupe on SHA-256 →
**copy original into the preserved archive** → extract EXIF (`captured_at`, dimensions) →
insert `asset` (optionally with user-chosen collection[s]) → render. The folder scanner
(`ingest.scan`) is retained as an **optional secondary** source (bulk desktop drops); it
calls the same `ingest_file`, so a source is a thin adapter, not a model change.

**Delete (web-driven, new):** deleting a photo **soft-deletes** the asset
(`status='deleted'`), removes its `present` placements from the TV (`delete(content_id)`),
and may purge the archived original. Soft-delete preserves `rotation_event` history
(it denormalizes `asset_id`). This adds `'deleted'` to the `asset.status` CHECK — the
**only** model change the web pivot needs.

**View / categorize:** the UI lists the library with **thumbnails** (a small per-asset
thumb generated at render time) and edits collection membership via `asset_collection`
— no model change. Optional `asset.source` (`'web'`/`'folder'`) records provenance.

### 5.4 Renderer
For assets lacking a `derivative` at the current `pipeline_version`, render + store the
JPEG on disk and insert the `derivative` row. Bumping `pipeline_version` re-renders the
library (an insert wave, not a mutation of source-of-truth).

### 5.5 Uploader / placement reconciler
Two-phase, idempotent, converges DB-intent ↔ device-reality:
1. **Upload:** insert `placement(state='pending')` **before** `upload()`; on the returned
   `content_id`, update to `present`. A crash leaves a resolvable `pending` breadcrumb.
2. **Reconcile:** when reachable, diff `available('MY-C0002')` against `present`
   placements → mark vanished ones `deleted_on_device`; resolve `pending`; **leave &
   flag** any device content we didn't upload (log, never delete).
3. **Harvest *(v2, §12.4)*:** the same `available()` listing carries each item's
   `matte_id`. If it differs from what we uploaded, the user changed it on the TV → store
   it per asset (TV wins). Runs at the top of every refresh/sync, in reconcile, and on a
   slow daemon tick.
4. **Matte at upload *(v2, §12.3)*:** `matte_for(asset_policy, derivative)`;
   `portrait_matte` is always `none`.

### 5.6 Scheduler
Composes the **desired resident set** for the current schedule period (the §4 query),
then drives the working set + slideshow:
1. Compute desired set (collections, `set_size`, no-repeat, weights, pins).
2. Reconcile `MY-C0002` toward it — upload missing, delete no-longer-wanted (recording
   `rotation_event` added/removed).
3. `set_slideshow(duration=<enum>, shuffle=<bool>)` scoped to `MY-C0002`.
Runs on schedule boundaries, on new ingest, and on a **wake transition** (unreachable→
reachable) so a fresh set is applied soon after the TV wakes. All reachability-gated:
asleep ⇒ skip, retry next tick.

> **v2 changes (§12.7):** `plan()` diffs by **derivative**, not asset, so a
> `pipeline_version` bump swaps stale derivatives for current ones through the normal
> add-before-remove reconcile; `refresh()` harvests mattes **before** any delete; and the
> daemon renders pending derivatives at the top of each due refresh (today only the web
> upload and `fao ingest` render).

> **Interval is a discrete enum** `{3, 15, 60, 720, 1440, 10080}` min (floor 3). Config
> values are validated against it. **Set-swap cost:** with `MY-C0002`-only, changing the
> themed set per period means upload/delete churn; keep periods coarse (e.g. daily
> refresh) in v1. Fine-grained time-of-day themes are a *later* feature (would want the
> Favourites escape hatch).

### 5.7 CLI (`fao`) — *built, to extend*
`ping/info/current/list/push/show/delete` exist. Add: `ingest`, `render`, `sync`,
`schedule run`, `pin/suppress/weight`, `collection` management, `slideshow` config,
`reconcile`, `status`.

---

## 6. Process model & concurrency
Single `systemd` daemon owns the write path; its tasks (renderer, uploader, scheduler,
reconciler) plus the **embedded web server** (UI + upload/delete/categorize API) run in
one process. The web server brings concurrent requests, so **all DB writes funnel through
a single serialization point** (one writer thread/queue) over **WAL** + `busy_timeout`;
concurrent reads are free. The `fao` CLI remains a separate short-lived process over WAL.
No external DB, no network DB. Bind the web server **LAN-only**; auth is a decision (open
on a trusted LAN vs. a shared household password) — never expose it to the internet.

Task cadences (indicative): scanner = fs-events + 5-min sweep; reconciler = 15 min when
reachable; scheduler = on period boundary + on ingest + on wake; a lightweight
**reachability poll** (~60 s) detects wake transitions.

---

## 7. Configuration (`config.toml`)
```toml
[frame]
host = "192.168.1.XXX"
port = 8002
token_file = ".token"
connect_timeout = 3.0

[paths]
inbox       = "/srv/frame/inbox"        # SMB share mount
originals   = "/srv/frame/originals"    # preserved archive
derivatives = "/srv/frame/derivatives"  # rendered 4K JPEGs
database    = "/srv/frame/state.db"

[image]
width = 3840              # panel size — the *maximum* output size in v2, not a target
height = 2160
jpeg_quality = 92
default_fit = "cover"     # v1: cover | contain.  v2: "auto" (§12.2)
default_matte = "none"    # matte for 16:9 (FILL) photos
pipeline_version = 1      # bump to re-render + swap every derivative (v2 pipeline = 2)
# --- v2 keys (§12.6), ignored while default_fit is cover/contain ---
crop_tolerance = 0.16     # trim ≤ this → FILL (crop to 16:9); else FIT. 3:2 trims 15.6 %
auto_matte = true         # false = FIT photos upload with matte "none" (the TV then crops them)
fit_matte_type = "flexible"   # initial matte type for FIT photos; must be in ODD_ALLOWED
fit_matte_color = "black"     # one of the TV's 16 colors (API notes §7.7)
low_res_long_edge = 1280  # gallery badge only; never blocks an upload

[schedule]
default_interval_minutes = 15   # must be in {3,15,60,720,1440,10080}
shuffle = true
set_size = 40
no_repeat_days = 30

[[schedule.period]]             # coarse in v1 (set-swap cost); refine later
name = "daily"
collections = ["family", "landscapes"]
```

Rules (periods, collections, intervals, sizes) live here. Only **runtime overrides**
(pin/suppress/weight, collection membership edits) live in the DB.

---

## 8. Operational concerns
- **Sleep/wake:** all device work gated on reachability; unreachable = normal. The Pi
  cannot wake the room (can't connect to a sleeping TV). Wake re-applies the current set.
- **Errors:** catch `ResponseError` (e.g. `-7` invalid interval); bounded calls guard
  hangs; failed uploads leave `pending` placements for the reconciler.
- **Logging:** structured, per-task; log flagged orphans and reconcile deltas.
- **Migrations:** `user_version`-gated, forward-only SQL.
- **Backups:** copy `state.db` (WAL-checkpointed). The **originals archive is the crown
  jewel** — back it up independently; everything else (derivatives, placements) is
  rebuildable, though history/collections/weights are not.
- **Deploy:** `systemd` unit (Restart=on-failure), SMB mount via `/etc/fstab` or
  `systemd.mount`, `.token` provisioned once (interactive Allow on the TV).

---

## 9. Platform constraints (from `SAMSUNG_FRAME_API.md`)
- Slideshow interval is the discrete enum above; store category uses a different,
  API-inaccessible scale.
- **No custom title** on personal photos today (only `date`); overlay shows date twice.
  Title kept in the model for a future path. **Not feasible** (no API/app/filename path;
  and **tested 2026-09-23**: embedding EXIF `ImageDescription`/`XPTitle`/XMP `dc:title`
  did not surface a title). Same test confirmed the overlay **date = the `upload(date=…)`
  param, not embedded EXIF** — pass it explicitly.
- Art getters can hang → bound all calls, avoid `get_auto_rotation_status`.
- Slideshow is **category-scoped** → manually-added `MY-C0002` photos *will* appear in
  rotation (accepted per "leave & flag").
- TV reachable only while awake; wakes showing the last-selected art.
- **Native sizes are accepted; matte `none` makes the TV center-crop non-16:9 photos**
  (identical to our `cover`). A whole portrait/4:3/square needs a `flexible`/`shadowbox`
  matte **set at upload** — `change_matte` fails with `-7` (API notes §7.2, §7.5).
- **A matte type unsuited to the image shape crashes the TV on display** (error 40000,
  hard restart) although the upload succeeds — validate before upload (API notes §7.6).
- **Matte edits made in the TV UI are readable** (`available()` → `matte_id`), so the TV
  is the matte editor and we harvest its choices (API notes §7.4).
- **The art API can wedge while the TV is awake and showing art.** Port open, REST
  `PowerState=on`, even a visibly running slideshow — yet every art call hangs (no
  `ms.channel.ready`). Powering the TV off doesn't help (it only turns off the panel);
  power-cycling the external media box did (API notes §8). Reachability ≠ availability:
  bound every call and treat a timeout as "not now".

---

## 10. Phased delivery
- **Phase 0 — Feasibility & core (DONE):** API proven on the unit; `frame_client`,
  `images`, `fao` (ping/info/current/list/push/show/delete); upload + slideshow verified.
- **Phase 1 — Library core (mostly done):** SQLite schema + migrations; ingest core
  (dedupe, archive, EXIF, render); uploader/placement (two-phase + reconcile);
  `date`-from-EXIF on upload. *(Ingest entry point currently CLI/folder.)*
- **Phase 2 — Rotation:** scheduler (set composition, slideshow config, wake-trigger);
  runtime overrides; reconciler loop; `systemd` packaging.
- **Phase 3 — Web UI (FastAPI + HTMX/Jinja, LAN-only) — BUILT 2026-09-24:** HTTP upload →
  `ingest_file`; gallery + lazy thumbnails; delete (soft-delete + device placement removal);
  categorize. `fao serve`. (Renders via a plain `jinja2.Environment`; plain uvicorn.)
- **Phase 4 — Polish:** smart collections (dates/seasons), richer weighting, in-UI TV
  control (show-now/pin).
- **Phase 5 — Upload pipeline v2 (PLANNED 2026-10-01):** native-resolution, never-upscale
  FILL/FIT rendering; TV-owned mattes with harvest + per-asset persistence; guarded
  auto-matte for non-16:9 photos. Evidence: `SAMSUNG_FRAME_API.md` §7. Plan and migration
  from the current state: **§12**.

## 11. Future / revisit triggers
- **Titles** — no known path (API/app/filename all absent; EXIF+XMP embed tested & failed
  2026-09-23). Revisit only if a future firmware/API adds one.
- **Favourites (`MY-C0004`) split** — if excluding manual adds / fine-grained time-of-day
  themes becomes a real need (removes set-swap churn).
- **Focal-point / crop editor** — for FILL crops that cut a subject. `ImageOps.fit` already
  takes `centering=(x, y)`, so a one-click "set focus" is the cheap form; it needs a
  per-asset value that changes the *pixels* (so a derivative key) and a re-upload — which
  is only safe once matte persistence (§12.4) exists.
- **Auto matte color** — choose the nearest of the TV's 16 colors (RGB table in API notes
  §7.7) to the photo's edge/dominant color instead of one fixed `fit_matte_color`.
- **Matte push after upload** — only if a firmware makes `change_matte` work (API notes §7.5).
- Face/people tagging; multi-device; managing the Frame's motion/night/brightness via the
  bonus setters (`set_motion_timer`, `set_brightness`, …).

---

## 12. Upload pipeline v2 & migration plan

*Status: **planned** (2026-10-01); **M0 (attended TV verification) completed 2026-10-02**.
Evidence: `SAMSUNG_FRAME_API.md` §7 (experiments on the live unit). Nothing in this section
is built yet.*

### 12.1 Why, and what changes
Measured on the current pipeline (v1, `cover` to 3840×2160): an 800×600 photo is upscaled
×4.8; a 2000×3000 portrait keeps 38 % of its pixels; a 5000×5000 square keeps 56 %. The TV
applies the *same* centered crop itself when the matte is `none` (API notes §7.2), so the
Pi's crop adds nothing but a lossy upscale. v2:

1. **Never upscale.** Output ≤ 3840×2160, downscale only; the TV scales for the panel.
2. **Near-16:9 photos → FILL:** a small centered crop to exact 16:9, full-bleed, matte `none`.
3. **Everything else → FIT:** native aspect, no crop, no padding, uploaded with an initial
   `flexible`/`shadowbox` matte so the **whole photo shows** (API notes §7.3).
4. **The TV is the matte editor.** We don't rebuild its UX; we *read back* the user's
   choice, store it per asset, and re-apply it whenever that photo is uploaded again.
5. **Never send a matte the TV may crash on** (API notes §7.6).

*Non-goals (→ §11):* crop/focal-point editor, AI upscaling, automatic matte color, pushing
a matte after upload (not possible: `change_matte` → `-7`).

### 12.2 Render plan (pure function: dimensions → plan)
After EXIF orientation: `ar = w/h`, and `trim = 1 − min(ar, 16/9) / max(ar, 16/9)` — the
fraction of the photo a crop to 16:9 would remove.

```
FILL  if trim ≤ crop_tolerance
        crop to exactly 16:9, centered (rows trimmed if too tall, columns if too wide)
FIT   otherwise
        no crop, no padding
scale = min(1, 3840/w′, 2160/h′)          # w′×h′ = size after the crop; never > 1
resize (LANCZOS) only when scale < 1; encode JPEG at jpeg_quality
shape = 'wide' if the output is 16:9 (±1 %) else 'odd'      # every FILL output is 'wide'
```
Computed from a prototype of this function:

| Source            | trim   | Plan                        | Output                             | Shape |
|-------------------|--------|-----------------------------|------------------------------------|-------|
| 1920×1080 (16:9)  | 0 %    | FILL                        | 1920×1080 (unchanged)              | wide  |
| 1280×720 (16:9)   | 0 %    | FILL                        | 1280×720 (unchanged)               | wide  |
| 1920×1200 (16:10) | 10.0 % | FILL                        | 1920×1080                          | wide  |
| 4928×3264 (3:2)   | 15.1 % | FILL                        | 3840×2160                          | wide  |
| 6000×4000 (3:2)   | 15.6 % | FILL                        | 3840×2160                          | wide  |
| 4032×3024 (4:3)   | 25.0 % | FIT                         | 2880×2160                          | odd   |
| 1600×1200 (4:3)   | 25.0 % | FIT                         | 1600×1200 (unchanged)              | odd   |
| 800×600 (4:3)     | 25.0 % | FIT                         | 800×600 (unchanged; low-res badge) | odd   |
| 5000×5000 (1:1)   | 43.8 % | FIT                         | 2160×2160                          | odd   |
| 2000×3000 (2:3)   | 62.5 % | FIT                         | 1440×2160                          | odd   |
| 2160×3840 (9:16)  | 68.4 % | FIT                         | 1215×2160                          | odd   |
| 2560×1080 (21:9) | 25.0 % | FIT | 2560×1080 (unchanged) | odd |
| 6000×2000 (3:1) | 40.7 % | FIT | 3840×1280 | odd |

**`crop_tolerance = 0.16`, not 0.15:** you asked for "~15 %" so that a DSLR 3:2 counts as
near, and a true 3:2 trims 15.6 % — a literal 0.15 would send every 3:2 photo to matte-fit
(a one-line change if that's what you want). **Wider-than-16:9 photos follow the same rule**
— verified 2026-10-02: with `none` the TV crops them left/right, and with `flexible` /
`shadowbox` it shows them whole (API notes §7.2, §7.3) — so there is no ultrawide special case.

### 12.3 Matte policy
The app has **no matte picker** (the TV's UI is the editor). It chooses only an *initial*
matte at upload:

```python
def matte_for(policy, derivative, cfg) -> str:       # pure; unit-tested. policy may be absent → NULLs
    shape = shape_class(derivative.width, derivative.height)        # 'wide' | 'odd'
    if policy.matte and policy.matte_shape == shape:
        return policy.matte                          # the TV itself offered it for this shape
    if shape == "wide":
        return cfg.default_matte                     # "none"
    return f"{cfg.fit_matte_type}_{cfg.fit_matte_color}" if cfg.auto_matte else "none"
```
- **Shape-scoped preferences.** A harvested matte is applied only to a derivative of the
  *same shape class* it was chosen under. That is what stops a `modern_*` chosen on a 16:9
  item from reaching a 4:3 derivative (the §7.6 crash) if a later policy change flips the
  photo's shape; the preference is ignored (and logged) and the default is used instead.
- **Config defaults are validated at load:** `default_matte` is `none` or a known
  `{type}_{color}`; `fit_matte_type ∈ ODD_ALLOWED = {flexible, shadowbox}` (verified on
  2:3, 4:3, 1:1 and 21:9; widen only after verifying, API notes §7.8); `fit_matte_color` is
  one of the TV's 16 colors.
- **`portrait_matte` is always `none`:** the TV UI never writes it, and a portrait
  displayed correctly with it `none` (API notes §7.4).
- **Default `flexible_black`** — verified via the API on 2:3, 4:3, 1:1 and 21:9 (API notes
  §7.11). The color is your choice; in photos it reads as a dark slate, not black, so it's
  one config line to change (and any photo can be changed on the TV).
- **Kill switch:** `auto_matte = false` → FIT photos upload with `none` (the TV then crops
  them — the observed, safe fallback) unless the user has a stored preference.
- **Defense in depth:** `FrameClient.upload_jpeg` re-validates the matte string (`none` or
  `<known type>_<known color>`) and raises before sending; `fao push --matte` refuses a
  non-`none` matte outside `ODD_ALLOWED` for a non-16:9 source unless `--force-matte`.

### 12.4 Matte harvest (TV → DB)
Source: the `available("MY-C0002")` listing — `reconcile` already fetches it and discards
the matte today.
```
for p in present_placements:                   # ours, resident on the TV
    item = listing.get(p.content_id);  if item is None: continue     # reconcile handles vanished
    tv = norm(item.matte_id)                   # None / '' → 'none'
    if tv != norm(p.matte):                    # the user changed it on the TV
        set_asset_matte(p.asset_id, tv, shape_class(p.derivative));  p.matte = tv
```
- **Only `matte_id`** is read; `portrait_matte_id` is ignored.
- **TV wins.** The DB never pushes a matte to a resident item (`change_matte` is `-7`); a
  stored preference takes effect the next time that asset is *uploaded*.
- **Where it runs:** (1) the **top of `scheduler.refresh()`**, before any delete — *no
  harvest, no evict*. If the TV can't answer (`FrameAsleep`/`FrameTimeout`) the **whole
  refresh aborts** and the daemon retries, exactly as before — nothing is evicted. (Swallowing
  that and returning normally would make the daemon record the refresh as done and wait 24 h.)
  Any *other* harvest failure skips only the evictions; adds proceed, so a harvest bug can't
  stall rotation. `uploader.sync` never evicts, so it doesn't harvest itself — the CLI runs
  `reconcile` right after it; (2) `reconcile`, from the listing it already fetches; (3) a slow
  **daemon tick** (`[daemon] harvest_minutes`, default 15 min while awake, one bounded call,
  attempt-based so a failing TV backs off a full interval) so a tweak is captured even if the
  photo is later evicted by something other than the scheduler; (4) `fao harvest [--dry-run]`.
- Orphans (content we didn't upload) are ignored.

### 12.5 Schema — migration 3 (additive, forward-only)
```sql
ALTER TABLE asset_policy ADD COLUMN matte TEXT;         -- NULL = no preference
ALTER TABLE asset_policy ADD COLUMN matte_shape TEXT;   -- 'wide' | 'odd'
```
No change to `derivative` (v2 uses `fit_mode='auto'` and real `width/height`) or
`placement`. v2 derivatives are new rows (`pipeline_version = 2`); v1 rows/files stay until
purged (M4), which is also the rollback path. An `asset_policy` row may not exist yet →
`INSERT OR IGNORE`, then `UPDATE` (as `store.set_policy` already does).

### 12.6 Config
See §7 `[image]`. New keys: `crop_tolerance`, `auto_matte`, `fit_matte_type`,
`fit_matte_color`, `low_res_long_edge`; `default_fit` gains `"auto"`. All are ignored while
`default_fit` is `cover`/`contain`, so M2 can ship dormant.

### 12.7 Code changes by file
| File | Change |
|---|---|
| `images.py` | Pure `plan_render(w, h, cfg) → RenderPlan`, `shape_class(w, h)`, `render_derivative(src, plan, quality)` (downscale-only); `render_to_file` returns the *real* size. `normalize_to_frame` stays for `fao push` (legacy `--fit`). |
| `mattes.py` *(new)* | The TV's type/color lists with RGB, `ODD_ALLOWED`, `matte_for`, `valid_matte_string`, config validation. Pure. |
| `ingest.py` | `render_pending` renders via the plan when `default_fit == "auto"`; stores actual `width/height`. |
| `store.py` | `present_placements` also returns `derivative_id`, `matte`, derivative size; `compose_working_set` / `derivatives_to_upload` also return `ap.matte`, `ap.matte_shape`, `d.width/height`; new `harvest_mattes(...)` / `set_asset_matte(...)`. |
| `scheduler.py` | `plan()` diffs by **derivative** (`to_add` = desired derivatives not resident; `to_remove` = resident ones not desired), so a `pipeline_version` bump swaps stale for current using the existing add-before-remove order. `refresh()` harvests first and skips removes if that failed, uses `matte_for`, and takes an optional `limit` for canaries. *(Switching the display away before deleting the displayed id is deferred to M3 — it only matters once the mass swap runs.)* |
| `uploader.py` | `sync` uses `matte_for`; `reconcile` calls harvest. |
| `frame_client.py` | `upload_jpeg` always sends `portrait_matte="none"` and validates the matte string; treat `ConnectionFailure` as transient (retry once or twice — another client's `clientDisconnect` can arrive first, API notes §8); on a bounded-call timeout close the websocket instead of abandoning the worker (the bounded call is itself the probe). **Found and fixed in M2:** `close()` closed only the remote socket — `tv.art()` builds a *separate* socket that was never closed (and a worker blocked in `recv()` kept it alive), a suspect for the wedged API (API notes §8). |
| `daemon.py` | Call `render_pending` at the top of each due refresh (today only the web upload and `fao ingest` render); slow harvest tick; drop the single global `self.matte`; treat "awake but the art API doesn't answer" (art call times out) like asleep → skip and retry. |
| `web.py` / templates | Gallery badge per photo (“cropped 15 %”, “fit with matte”, “low-res 800×600”) from stored dimensions — non-blocking, no warning dialogs; details view shows plan + matte read-only. Thumbnails already come from the derivative, so FIT photos preview whole. |
| `cli.py` | `fao harvest`; `fao plan-report` (dry run: per asset → trim, plan, output size, matte); `schedule-refresh --limit N`; `push` uses the plan + matte guard. |
| `db.py` | `_V3` migration. |
| `tests/` *(new)* | See §12.10; add a `dev` extra with `pytest`. |
| `README.md`, `config.toml` | Document the new keys; update the `[image]` text. |

### 12.8 Migration plan — from where we are
**Current state (2026-10-02).** The production DB and daemon are on the Pi (this Mac's
`data/state.db` is a dev copy with no placements). `pipeline_version = 1`,
`default_fit = "cover"`, `default_matte = "none"`; DB schema version 2. The TV holds 41
photos in `MY-C0002`, all 3840×2160 — consistent with Pi uploads. **Three have matte edits
made in the TV UI** that exist nowhere else: `MY_F0083` and `MY_F0071` → `modernthin_black`,
`MY_F0079` → `shadowbox_sage` (it was `modern_seafoam` on 10-01). Between the 10-01 and
10-02 baselines the daemon swapped 9 photos without reading mattes, so edits on any evicted
photo are already gone — exactly the case "no harvest, no evict" prevents. The daemon is
**stopped** for attended sessions (restart with `sudo systemctl start frame-art-organizer`).
`get_slideshow_status` reads `off`, but the displayed photo changed on its own, so the
getter misreports — the user saw a slideshow running (API notes §8).

Each step is independently shippable and leaves the system working; M1 and M2 change no
rendering.

| Step | What | Exit criteria |
|---|---|---|
| **M0 — Attended TV verification** *(no code)* — **DONE 2026-10-02** | Ran the matte × shape matrix with the §7.10 protocol, TV-UI-first with a polling watcher, then API-set (API notes §7.11): `flexible` and `shadowbox` offered and displayed whole on **2:3, 4:3, 1:1 and 21:9**; `flexible_black` set via the API displayed correctly on all four; wider-than-16:9 behaves like the other non-16:9 shapes. | **Cleared:** `ODD_ALLOWED = {flexible, shadowbox}` and the default `flexible_black` are verified; the ultrawide special case is gone. **Slideshow:** `get_slideshow_status` reads `off` while a slideshow visibly runs, so it is a getter misreport, not a state (§12.11); the M3 canary still watches the wall rotate. |
| **M1 — Harvest only** — **BUILT** on branch `m1-matte-harvest` (58 tests; ships when merged and deployed to the Pi) | Migration 3, `store.harvest_mattes`, wired into `reconcile`, the top of `refresh`, and a daemon tick; `fao harvest`. Rendering untouched. | On the Pi, `fao harvest` records exactly the edits present at that time — as of 2026-10-02 three: `MY_F0083` and `MY_F0071` → `modernthin_black`, `MY_F0079` → `shadowbox_sage` (all shape `wide`); a second run reports none. TV unchanged (read-only). |
| **M2 — v2 code, dormant** — **BUILT** on branch `m2-render-plan` (188 tests; ships when merged and deployed) | `images.plan_render`, `mattes.py`, scheduler/uploader changes, tests. Config stays `cover` / `pipeline_version = 1`. `matte_for` is live: v1 derivatives are all 16:9 (`wide`) → preference-or-`none`, i.e. today's behavior **plus** honoring harvested preferences and `portrait_matte = none`. | Tests green; `fao plan-report` on the real library shows the FILL/FIT split with no surprises; an attended `schedule-refresh` behaves exactly as today, except that any newly uploaded photo uses its harvested matte preference. |
| **M3 — Flip to v2** — tooling + rehearsal **BUILT** on branch `m3-migration`; the live canary is attended: runbook §12.12 | Set `default_fit = "auto"`, `pipeline_version = 2`. Render all derivatives (daemon or `fao ingest`) **before** the first v2 refresh — selection only sees `(fit_mode, pipeline_version)` matches, so an unrendered library would look empty and the empty-set guard would skip. Then an **attended canary**: `schedule-refresh --limit 3` (include one FIT and one FILL photo), look at the wall, then run it unlimited. The derivative-based diff replaces each v1 placement with its v2 derivative, adds first, so the set never shrinks — about `set_size` uploads, once. | Canary photos look right on the TV; a baseline-style diff shows the same photo count with v2 sizes/mattes; the harvested edits (e.g. `MY_F0079` → `shadowbox_sage`) are re-applied. |
| **M4 — UI + cleanup** | Gallery badges, details view, README. After a soak period (a week or two), purge v1 derivative rows/files. | Badges match `plan-report`; v1 purged. |

**Operational prerequisites (learned 2026-10-02).**
- **Wedged API:** if art calls hang while the TV is awake and showing art, power-cycle the
  external media box — the TV's own power button only turns off the panel (API notes §8).
  Probe with a bounded `get_artmode` before any attended session.
- **Pi access:** before restarting the Pi's daemon, check the TV's access mode and allowed
  list. Under "allow, prompting first time" the Pi's client (`FrameArtOrganizer`) must be on
  the list (pair once with `fao info` on the Pi and press Allow) or it hangs on every
  connect and rotation silently stops.
- **Stop the daemon during attended experiments** (it re-asserts the slideshow on every TV
  wake and competes for the art channel), then restart it.

**Rollback.** Set `default_fit = "cover"` and `pipeline_version = 1`; the next refresh swaps
back to the retained v1 derivatives. Stored matte preferences are harmless (shape-scoped).
If auto-matte misbehaves, set `auto_matte = false` without rolling pixels back. If the TV
shows the 40000 dialog: power-cycle, then `fao delete <content_id>` for the offender (the
guards should prevent it). Don't purge v1 until M4's soak ends.

### 12.9 Risks & mitigations
| Risk | Mitigation |
|---|---|
| A matte/shape mismatch crashes the TV (known) | Shape-scoped preferences; config validation against `ODD_ALLOWED`; string validation in `upload_jpeg`; M0 gate; `auto_matte` kill switch; attended canary |
| Art API wedged while the TV looks fine (calls hang; only a media-box power-cycle fixes it); another client's `clientDisconnect` can abort a connect | Bounded calls (already `FrameTimeout`, treated like asleep); close sockets on timeout; retry `ConnectionFailure` once or twice; probe `artmode` first |
| Pi can't connect after the TV's access mode is changed to prompting | Verify `FrameArtOrganizer` is on the allowed list before restarting the daemon (§12.8 prerequisites) |
| User's TV edit lost to eviction | Harvest-first; *no harvest, no evict*; slow daemon harvest tick |
| A matte edit lost when a photo is re-uploaded in the same run it was edited | Preferences are read **after** the harvest (a bug the M3 rehearsal found and fixed: the plan had copied stale ones) |
| Harvest false positives (firmware reports `''`/`None` for none) | Normalize to `none` before comparing; M1's second run must be a no-op |
| `available()` hangs | Bounded call; skip harvest **and removes** that run; adds may proceed |
| Native-size photos look softer than a Lanczos 4K | Judge on the wall during the M3 canary; rollback is one config change |
| FIT photos show smaller (matte around them) | Intended; `flexible` fills the panel height for portraits (API notes §7.3) |
| One-time churn at M3 (~40 uploads); an unlimited swap peaks at **2× the set** (add-before-remove) | Batches via `--limit` (a batch of 10 peaks at set + 10); canary first; attended; TV awake only (§12.12) |
| Deleting the displayed item flickers | Switch the display to a surviving item first (§12.7 `scheduler.py`) |
| New daemon harvest tick vs the single DB writer | Same single-writer discipline as today (§6) |

### 12.10 Test plan
- **Unit (no TV, `pytest`):** `plan_render` over every ratio in §12.2 (property: output ≤
  3840×2160 and never larger than the source crop; FIT preserves aspect); `shape_class`;
  `matte_for` matrix (preference same/different shape, default per shape, `auto_matte` off,
  `portrait_matte` always `none`); config validation rejects `modern` / unknown colors for
  FIT; harvest diff against a fake listing (changed, unchanged, vanished, orphan,
  `''`/`None`); `plan()` derivative-diff after a version bump; migration 2→3 on a copy of
  the real DB.
- **Attended integration (the live TV):** the §7.10 protocol with labeled synthetic images —
  baseline snapshot, recorded ids, canary swaps, exact-id cleanup, baseline diff.
- **Dry run:** `fao plan-report` over the real library before M3.

### 12.11 Decisions (confirmed 2026-10-02)
1. **`crop_tolerance = 0.16`** — decided (a true 3:2 trims 15.6 % and gets cropped).
2. **Default matte `flexible_black`** — decided; black is fine. `flexible` sizes the photo to
   the panel's long edge (shows larger) where `shadowbox` insets it; `*_black` reads dark
   slate on the panel, and any photo can be changed on the TV.
3. **Eager migration** — decided: at the first v2 refresh the derivative-based diff swaps
   every stale derivative, canaried with `schedule-refresh --limit N` (§12.8 M3).
4. **Daemon harvest tick: 15 min** — decided.
5. **Ultrawide** — resolved by M0: same rule as other non-16:9 shapes; no special case.
6. **Slideshow reads `off`** — resolved enough: the user saw a slideshow running while the
   getter read `off`, so it misreports; never gate logic on it. The M3 canary still watches
   the wall actually rotate.

**Still to do before M1 ships (operational):** restart the Pi's daemon with the TV's access
mode and allowed list checked (§12.8 prerequisites); decide whether the TV's motion sensor
stays off.


### 12.12 Runbook — the live migration (attended)
Everything the code can do ahead of time is built and rehearsed (`tests/test_migration_rehearsal.py`
runs this exact sequence against a model of the TV, including the crash rule). What remains
needs **you at the TV**. Do it only after M1–M4 are deployed to the Pi and `fao harvest` has run
clean (M1's exit criteria).

**Before you start**
- The Pi's daemon is **stopped** — `sudo systemctl stop frame-art-organizer` — for the whole
  procedure. If it is running when the config flips, its first refresh is an *unlimited* swap with
  no canary.
- The TV is awake in Art Mode and the art API answers (`fao info`). If it hangs, power-cycle the
  media box first (API notes §8).
- Back up `state.db`. Take a snapshot to diff against later: `fao list | sort > before.txt`.

**1. Review (no TV, no risk)** — `fao plan-report`. Read the FILL/FIT split and the matte column.
Anything surprising (a photo you expected to stay whole being cropped, a wrong low-res flag)?
Adjust `crop_tolerance` (try `fao plan-report --crop-tolerance 0.15`) or stop here.

**2. Flip the config** (daemon still stopped) — in `config.toml` `[image]` set
`default_fit = "auto"` and `pipeline_version = 2`; leave the rest at their defaults. Then
`fao schedule-show` should now report `resident on it: 0/N, N stale` on its `pipeline` line. A
bad value fails right here with the key named, before the TV is touched.

**3. Canary** — `fao schedule-refresh --limit 3`. The first run renders the whole library
(`rendered N pending derivative(s)`; minutes on a Pi) and then uploads 3 photos, evicting the 3
old versions of those same photos. It lists each upload:
`+ name  WxH  matte=…  -> MY_Fxxxx`, and `--limit reached: K more swap(s) pending`.
Look at the wall with `fao show MY_Fxxxx`, picking at least **one whole (FIT) photo and one
full-bleed (FILL) photo** (`fao plan-report` says which is which):
- FIT: the whole photo, nothing cropped, inside a dark-slate `flexible_black` matte.
- FILL: full-bleed 16:9; a near-16:9 photo trimmed only slightly.
- No error dialog, and the slideshow still cycling (watch the wall rotate — `get_slideshow_status`
  is unreliable, API notes §8).

**4. Continue in batches** — `fao schedule-refresh --limit 10`, repeated, until a run reports
`added=0 removed=0` and `fao schedule-show` says `resident on it: N/N`. Use batches rather than
one unlimited run: the TV briefly holds the old *and* new copy of each batch (add-before-remove),
so an unlimited swap peaks at **2× the set** and a batch of 10 at set + 10.

**5. Verify** — `fao list | sort > after.txt; diff before.txt after.txt`: the same number of
photos, with native sizes (no more 3840×2160 for small photos) and the mattes the policy gives.
`fao harvest --dry-run` should report no edits.

**6. Resume** — `sudo systemctl start frame-art-organizer`. Its first refresh is now a no-op.

**What to expect**
- A matte you chose on the TV carries over **only if the photo stays 16:9**. For a photo that
  becomes whole (FIT) the choice was made on the cropped 16:9 version, so it is dropped and the
  default is used — by design: the TV offers different mattes per shape, and a mismatched one can
  crash it. You can re-pick on the TV; the harvest remembers.
- The v1 derivative files stay on disk until M4 purges them — that is what makes rollback instant.
- **Unverified on the real TV:** before deleting the photo that is on the wall, the refresh shows a
  surviving one with `select(show=False)` (only in Art Mode). If the wall jumps to another photo
  when the displayed one is replaced, that is it working; if it flickers or blanks briefly instead,
  note it — it is cosmetic and doesn't affect anything else.

**Abort / roll back** (any time): set `default_fit = "cover"` and `pipeline_version = 1`, then
`fao schedule-refresh --limit 10` repeatedly (the same batches, in reverse; preferences re-apply).
- To stop the matte side only: `auto_matte = false` — whole photos upload with matte `none` and
  the TV crops them, no rollback needed.
- TV shows the *40000* dialog or the art API hangs: power-cycle the **media box** (the TV's own
  power button only turns off the panel), then `fao delete <content_id>` for the offender. The
  guards should make this impossible; if it happens, stop and report which photo and matte.
- Whatever happens, a refresh that cannot read the TV's mattes aborts without evicting anything.

**Exit (M3 done)** — canary photos looked right; the post-migration diff shows the same photo
count with native sizes and policy mattes; the harvested edits that should carry over did; the
daemon is running and its refresh is a no-op.
