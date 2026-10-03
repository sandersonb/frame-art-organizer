# Samsung The Frame — Local Art API (Field Notes)

Everything below was **empirically verified against a real unit**, not taken from
docs (there are none). Treat it as reverse-engineered and firmware-fragile.

| | |
|---|---|
| Unit | **QN65LS03FAFXZA** (65" The Frame, 2025), internal model `25_PTM_FTV` |
| Firmware art-API version | **`5.0.1.0`** (`art().get_api_version()`) |
| Client library | `samsungtvws` **3.0.6** (xchwarze, PyPI), sync API |
| Transport | WebSocket over TLS on **port 8002** + REST on 8001/8002 |
| Verified | 2026-09-23 (core API) · 2026-10-01 and 2026-10-02 (aspect-ratio / matte experiments, §7) |

> ⚠️ **Unofficial.** Samsung does not document or support this. It has broken
> across firmware generations before (2024 handshake change) and can again. The
> `5.0.1.0` behaviors here may not hold on other models/firmware.

---

## 1. Connectivity & authentication

- **Ports.** `8001` = plain WS/REST, `8002` = **TLS** WS/REST. This firmware issues
  the token on **8002**; use it. The 8002 certificate is **self-signed** → disable
  TLS verification (the client does; you'll see `InsecureRequestWarning`).
- **Token pairing.** The *first* connection pops an **Allow / Deny prompt on the TV
  screen**; accept it once with the remote. The returned token must be persisted
  (`token_file`) or every connect re-prompts. Toggling some Art settings on the TV
  can re-trigger the trust prompt.
- **A second client did not need pairing (observed 2026-10-01).** A different client
  name (`FrameArtExperiment`) with a `token_file` that did not exist connected and ran
  `available`, `get_current`, `upload`, `select_image` and `delete` with **no Allow
  prompt**, and no token was returned or written. So pairing is not strictly required on
  this unit. Why is not investigated (likely a TV-side connection-approval setting); don't
  rely on it for other TVs or firmware.
- **The TV has an access-mode setting** ("always allow" vs "allow, prompting first time")
  and an allowed-clients list. On 2026-10-01 the TV was on *always allow*: clients were let
  in silently and **never appeared in the list** (only the Sep 23 `FrameArtProbe` pairing
  did). On 2026-10-02 the user switched it to prompting and cleared the list; a client then
  triggered an on-screen Allow popup (the user pressed Allow). Whichever mode is active, a
  client that is not yet approved can sit silently on the websocket — see the wedged-API and
  `clientDisconnect` gotchas in §8. If the Pi's client (`FrameArtOrganizer`) is not on the
  list, switching to the prompting mode would make the Pi hang on every connect until
  someone presses Allow.
- **REST device info** (no token): `GET https://<host>:8002/api/v2/` returns model,
  `PowerState`, `TokenAuthSupport`, `FrameTVSupport`, `resolution`, `wifiMac`,
  `duid` (stable TV UUID across IP changes). Handy as a cheap liveness/identity check.

```jsonc
// GET /api/v2/  → device{}
"modelName": "QN65LS03FAFXZA", "model": "25_PTM_FTV",
"PowerState": "on", "TokenAuthSupport": "true", "FrameTVSupport": "true",
"resolution": "3840x2160", "wifiMac": "AA:BB:CC:DD:EE:FF",          // redacted
"duid": "uuid:00000000-0000-0000-0000-000000000000"                // redacted
```

---

## 2. Power states & network behavior (critical)

The API is only reachable when the TV is **awake**. Observed over a full sleep/wake
cycle (polling port 8002 every 20 s):

```
AWAKE           →  port 8002 OPEN,  PowerState "on"
going to sleep  →  brief "on" → "standby" (port still OPEN ~1 sample, panel dark)
ASLEEP / OFF    →  port 8002 CLOSED (TCP refused), no ICMP; ARP entry persists
waking          →  CLOSED → OPEN, PowerState "on" within ~20 s
```

Consequences that drive the whole app design:
- **A sleeping TV cannot be reached or driven** — you can't select/upload/slideshow.
  Treat "port closed" as *asleep, retry later*, never an error. This also means an
  automated scheduler **cannot accidentally wake the room** — you can't wake what you
  can't connect to.
- **On wake the TV shows the last-selected artwork** (confirmed). So set the desired
  art while awake; it persists through the next sleep→wake.
- There is a **brief `standby` window** where the port is still open but the panel is
  off (`PowerState=standby`). To avoid poking a half-asleep panel, gate on *both* a
  TCP probe **and** `PowerState != standby`, not TCP alone.
- Use a fast **TCP connect to 8002** as the "is it awake?" probe before any art call
  (a sleeping TV otherwise makes the client hang on connect).
- **"Power off" only powers off the panel.** The API's web server runs on the external media
  box (the Frame's One Connect box), which keeps running. If art calls hang while the TV
  looks fine, power-cycle that box (§8); toggling the TV's own power does not restart it.

---

## 3. Request/response model & categories

Art commands go over the `com.samsung.art-app` WS channel as JSON requests with a
`request_id`; replies are matched "D2D" events. Content is organized by **category**:

| category_id | Meaning | content_id scheme |
|---|---|---|
| `MY-C0002` | **My Photos** (personal uploads) | `MY_Fxxxx` |
| `MY-C0004` | Favourites | — |
| `MY-C0008` | Store (purchased) | `SAM-Sxxxx` |
| `ARTSTREAM` | Art Store subscription stream | `SAM-Sxxxx` |

`content_type` seen: `myphoto` (personal, when current), `mobile` (personal, in the
`available()` listing right after upload), `artstore` (Art Store), `server`.

---

## 4. Operations reference (verified)

Method names/signatures are from `samsungtvws.art.SamsungTVArt` (3.0.6).

### Read / status
- `supported() -> bool` — `True` here.
- `get_api_version() -> str` — `"5.0.1.0"`.
- `get_device_info() -> dict` — sensors & capabilities:
  ```jsonc
  "support_motion_sensor": "TRUE", "support_brightness_sensor": "TRUE",
  "resolution_type": "UHD", "tv_flash_size": 16, "current_rotation_status": 1,
  "support_myshelf": "FALSE", "server_sync_state": "TRUE"
  ```
- `get_artmode() -> "on"|"off"` ; `set_artmode(...)`.
- `get_current() -> dict` — `{content_id, matte_id, portrait_matte_id, category_id, content_type}`.
- `available(category=None) -> list[dict]` — pass `"MY-C0002"` for personal photos.
  Item shape: `{content_id, category_id, slideshow, matte_id, portrait_matte_id,
  width, height, image_date, content_type}`. `width`/`height` are the **uploaded
  file's real pixel size** (native-size uploads report their native size — §7.1), and
  `matte_id`/`portrait_matte_id` **reflect edits made in the TV's own matte UI** (§7.4).
- `get_matte_list() -> {matte_types[], matte_colors[]}` — types: `none, modernthin,
  modern, modernwide, flexible, shadowbox, panoramic, triptych, mix, squares`;
  16 colors (black, neutral, antique, warm, polar, sand, seafoam, sage, burgandy,
  navy, apricot, byzantine, lavender, redorange, skyblue, turquoise). Matte value
  format is **`{type}_{color}`**, e.g. `shadowbox_polar`; `none` = fill the panel.
  Entries are objects (`{"matte_type": …}`, `{"color", "R", "G", "B"}`) — RGB table in
  §7.7. **Not every type is valid for every image shape; some combinations crash the
  TV (§7.6).**
- `get_thumbnail(content_id_list=None, as_dict=False)`, `get_thumbnail_list(...)` —
  fetch JPEG thumbnails (useful for a gallery UI).

### Write
- `upload(file, matte='shadowbox_polar', portrait_matte='shadowbox_polar',
  file_type='png', date=None) -> content_id`
  - **Confirmed working:** JPEG bytes, `file_type='JPEG'`, `matte='none'`, `3840x2160`.
    Returns a TV-assigned id like `MY_F0001`; lands in `MY-C0002`.
  - **Also confirmed (2026-10-01): native-size, non-16:9 JPEGs** — 1500×2250, 2000×2000,
    1600×1200 and 1280×720 are accepted as-is (no resize/reject). What the TV then
    *shows* depends on the matte — see §7.
  - **`matte` / `portrait_matte`** are stored exactly as sent (read back via
    `available()`), but the matte **type must suit the image shape or displaying the
    item can crash the TV** (§7.6). Upload is the **only** API way to set a matte (§7.5).
  - **`date`** is the *only* settable metadata (format `YYYY:MM:DD HH:MM:SS`, matching
    the `image_date` field). Set it from EXIF `DateTimeOriginal` — see §5 titles.
- `select_image(content_id, category=None, show=True)` — display an item. Works for
  personal and Art Store ids. `show=True` powers the panel on if needed.
- `delete(content_id) -> bool` ; `delete_list(content_ids) -> bool` — confirmed clean.
- `set_slideshow_status(duration=0, type=True, category=2, category_id=None)` — see §6.
- **`change_matte` does not work** on this firmware (`-7`, §7.5).
- Untested but present (for future use): `set_favourite`,
  `set_photo_filter`, `set_motion_timer`, `set_motion_sensitivity`, `set_brightness`,
  `set_brightness_sensor_setting`, `set_color_temperature`. (These imply the Pi could
  later manage the Frame's Sleep-After timer, motion sensitivity, and brightness.)

---

## 5. Limitation: no custom titles for personal photos

There is **no API to set a human title/caption** on a personal photo — the whole
method surface has no rename/title/metadata call. The Art-Mode info overlay has a
**title line and a date line**; for personal photos the title falls back to the date,
so the overlay shows the **date twice** (confirmed by photographing the overlay).

**The only lever is `date`.** If you upload without one, the TV stamps the *import*
date. **Always pass `date` = EXIF capture date** so the overlay reads as when the
photo was taken. Title/artist info cards are an Art-Store-only feature.

**Researched 2026-09-23 (conclusive-ish):** setting a real title is **not feasible**
with known tooling. No title/rename method exists in `samsungtvws` or NickWaterton's
fork (verified from source); the SmartThings/Art Store add-photo flow has no naming
field; the filename is never transmitted. The Frame parses **only** EXIF *orientation*
(for auto-rotate) and shows the *date* — no evidence it renders an embedded
EXIF `ImageDescription`, EXIF `XPTitle`, IPTC Object Name/Caption, or XMP `dc:title`.
**Tested 2026-09-23 — it failed:** a JPEG with the title embedded in EXIF
`ImageDescription` + EXIF `XPTitle` + XMP `dc:title` still showed the **date twice**,
no title. Titles are **conclusively unsupported** on personal photos. The same test
also proved the overlay **date comes from the `upload(date=…)` param, not embedded
EXIF** — a distinct embedded `DateTimeOriginal` (2005) was ignored while the upload
param (2019) was shown. **So pass `date` explicitly on upload; embedding it in the
file does nothing.** (Burning a caption into the pixels is the only guaranteed
alternative — not recommended.)

---

## 6. Slideshow control (the "let the TV cycle it" mechanism)

The TV runs the slideshow itself; you just curate the set and set the config. This is
the backbone of the "Pi curates, TV cycles" architecture.

- **Read:** `get_slideshow_status() -> {value, category_id, type, current_content_id,
  content_list}`. `content_list` is a **JSON string** and can be **empty `""`** —
  guard the parse.
- **Set:** `set_slideshow_status(duration, type, category_id=...)`
  - `duration` = **minutes**, `0` = off.
  - `type` = **bool**: `True` → `shuffleslideshow`, `False` → `slideshow` (ordered).
  - `category_id` (preferred) = e.g. **`"MY-C0002"`** to cycle *your* photos; or
    `category` int builds `MY-C000{n}` (2=my pics, 4=favourites, 8=store).
  - **Proven:** `set_slideshow_status(duration=3, type=True, category_id="MY-C0002")`
    → TV shuffle-cycles the personal-photo set. The resident set *is* the slideshow.

### ⚠️ The interval is a discrete enum
Only these `duration` values are accepted for `MY-C0002` (verified by probing):

**`{3, 15, 60, 720, 1440, 10080}` minutes** = 3 min · 15 min · 1 hr · 12 hr · 1 day · 7 days.
**Floor is 3 minutes.** Any other value (1, 2, 5, 10, …) → `ResponseError` **error `-7`**.

The Art Store stream reported `value=5`, but `duration=5` is **rejected even for
ARTSTREAM** via this API — so the store category's interval is set on a different
scale by the TV's own UI and is *not* controllable through the local API. Only the
enum above is drivable.

---

## 7. Aspect ratio, cropping & mattes (experiments, 2026-10-01 and 2026-10-02)

**Method.** Synthetic images with four distinctly colored edges (red top, blue bottom,
green left, yellow right) and 10/20/30 % inset outlines, so any crop or matte is
obvious: portrait 1500×2250 (2:3), square 2000×2000, 4:3 1600×1200, 16:9 1280×720.
Each was uploaded to `MY-C0002`, displayed one at a time, and **photographed on the
wall**. Mattes were set either in the TV's own Art-Mode matte UI or at upload. A
baseline of the TV's 41 existing photos was taken first and verified identical afterwards
(protocol in §7.10).

### 7.1 Native-size uploads are accepted
`available()` reported each file's own size — the TV neither requires nor forces
3840×2160 (before this, only 3840×2160 had been confirmed):

| Uploaded   | `available()` width×height |
|------------|----------------------------|
| portrait   | 1500×2250                  |
| square     | 2000×2000                  |
| 4:3        | 1600×1200                  |
| 16:9 small | 1280×720                   |

### 7.2 Matte `none`: the TV center-crops to fill
| Image         | What the wall showed                                                                                                                                      |
|---------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------|
| 2:3 portrait  | Full width kept; only rows ≈ **31–69 %** visible (~62 % of the photo cut off). No red top / blue bottom edge; the 30 % outline's label clipped at the top |
| 1:1 square    | Full width; rows ≈ **22–78 %** (top/bottom edges gone; "20 %" label clipped, 30 % outline fully visible)                                                  |
| 4:3           | Full width; rows ≈ **12.5–87.5 %** (10 % outline gone, 20 % outline visible)                                                                              |
| 21:9 2560×1080 (2026-10-02) | **Full height** kept; **left/right cropped** — no green or yellow edge, no vertical outlines; red top and blue bottom visible |
| 16:9 1280×720 | **Whole image**; the TV upscales ×3 to fill the panel; fine text stays smooth                                                                             |

This is exactly a centered "cover" crop (`ImageOps.fit(centering=(0.5, 0.5))`) — what
this project's `cover` mode did — but done by the TV. **"Let the Frame handle it" with
no matte is a centered crop, not letterboxing.** The same holds for shapes wider than
16:9 (verified 2026-10-02 with a 21:9 image).

### 7.3 With a matte, non-16:9 photos are shown whole (fit)
- The TV UI offers **`flexible` and `shadowbox`** for every non-16:9 shape tried — 2:3,
  4:3, 1:1 and 21:9 (the 2026-10-02 watcher log, §7.11, shows `flexible_black` as option 1
  and `shadowbox_black` as option 2 every time) — versus ~7 types for 16:9. The user
  reported exactly two options for 2:3 and 4:3; the count was not recorded for 1:1 and 21:9.
- **Every** non-16:9 shape × {`flexible`, `shadowbox`} combination tried displayed the
  **whole image** (all four colored edges and every outline visible) inside the matte,
  with no crash. `flexible` sizes the photo to the panel's long edge (portrait: nearly
  full height; 21:9: nearly full width); `shadowbox` insets it with a margin on all sides
  and a drop shadow, so it shows smaller.
- **4:3 + `shadowbox_byzantine`** (2026-10-01): centered, inset with a margin all round;
  **2:3 + `flexible_black`**: scaled to nearly the full panel height with a thin light edge.
- The result was **identical whether the matte was set in the TV UI or via
  `upload(matte=…)`** for every combination tested through both paths (§7.11).

### 7.4 Matte read-back — what the TV writes
- `available()` and `get_current()` report `matte_id` / `portrait_matte_id` and they
  **reflect edits made in the TV UI**: a user-set `modern_seafoam` on an existing 16:9
  photo, `shadowbox_byzantine` (4:3) and `flexible_black` (portrait) all read back
  correctly. (Pre-existing photos uploaded by this project read `none` / `none`.)
- Editing the matte of a **portrait, 4:3, square or 21:9** photo in the TV UI changed
  `matte_id` only; **`portrait_matte_id` stayed `none`** in every logged state.
- An API upload of a portrait photo with `matte='flexible_black', portrait_matte='none'`
  **displayed the matte**, so `matte_id` is what applies to portrait photos on this
  landscape-mounted panel and `portrait_matte_id` can stay `none`. Whether
  `portrait_matte_id` matters if the TV is physically rotated: untested.
- `upload()` round-tripped these `matte/portrait_matte` pairs exactly: `none/none`,
  `modern_polar/none`, `none/modern_polar`, `shadowbox_byzantine/none`,
  `flexible_black/none`.
- **Real-world edits already on the TV (2026-10-02).** Between the 2026-10-01 baseline and
  the next day the user changed three existing 16:9 photos in the TV UI: `MY_F0083` and
  `MY_F0071` `none` → `modernthin_black`; `MY_F0079` `modern_seafoam` → `shadowbox_sage`.
  All read back via `available()`. So 16:9 photos accept at least `modern`, `modernthin`
  and `shadowbox`.

### 7.5 `change_matte` does not work on this firmware
`art.change_matte(content_id, matte_id, portrait_matte=…)` → `ResponseError`
**`-7`** in every variant tried: matte only; with `portrait_matte='none'`; with both set;
on the displayed item and on a non-displayed one — although `modern_polar` is a valid
type/color. **A matte can be set only at upload time**; afterwards only the TV UI can
change it. A re-upload is the only API way to "change" one.

### 7.6 ⚠️ Crash: a matte type that doesn't suit the image shape
Uploading a **4:3 (1600×1200) image with `modern_polar`** succeeded and listed normally,
but **`select_image` of it** made the TV show *"An unexpected problem has occurred.
Please turn off and on and then try again (40000)"* and **hard-restart**. The
`select_image` call never returned. The TV came back on the previously displayed
artwork; nothing was lost (all 41 photos, mattes and dimensions identical afterwards).

- **Hypothesis (not proven):** the art app crashes when asked to render a matte type it
  does not offer for that aspect ratio. Supporting: the TV UI offers only two types for
  non-16:9, and the two combinations the UI itself produced — `shadowbox_byzantine` on
  4:3 and `flexible_black` on portrait — displayed fine when set **via the API at
  upload**. Only one failing combination was observed (`modern` + 4:3); `modern_polar`
  on a portrait and on a square were uploaded but never displayed (then deleted).
  **Strengthened 2026-10-02:** the TV UI offers only `flexible`/`shadowbox` on every
  non-16:9 shape tried, and `flexible_black` set via the API displayed correctly on all
  four of them (§7.8, §7.11) — still no counter-example.
- **Hazard:** the bad combination is accepted at upload and sits in `MY-C0002` until
  something displays it — including a slideshow cycling the category. **Never upload a
  matte type the TV does not offer for that shape.**
- **Recovery:** power-cycle with the remote, then delete the offender by `content_id`.

### 7.7 Matte palette
`get_matte_list()` returns `matte_types` as `[{"matte_type": "none"}, …]` and
`matte_colors` as `[{"color": "black", "R": 34, "G": 34, "B": 33}, …]`. The TV UI
shows a color selector with all 16:

| color   | RGB           | color          | RGB           |
|---------|---------------|----------------|---------------|
| black   | 34, 34, 33    | burgandy (sic) | 98, 39, 46    |
| neutral | 137, 136, 134 | navy           | 39, 53, 74    |
| antique | 224, 219, 210 | apricot        | 239, 188, 96  |
| warm    | 231, 231, 223 | byzantine      | 136, 86, 137  |
| polar   | 232, 230, 231 | lavender       | 182, 171, 177 |
| sand    | 164, 145, 113 | redorange      | 219, 103, 66  |
| seafoam | 90, 104, 101  | skyblue        | 105, 192, 211 |
| sage    | 170, 176, 141 | turquoise      | 46, 150, 141  |

`black` is charcoal (34,34,33), not pure black; in phone photos of the wall — across four
photo sets on 2026-10-01/02 — a `*_black` matte consistently read as a dark slate /
blue-grey (camera exposure may exaggerate it). Pick the default color by eye on the panel.

### 7.8 Known matte × shape results
OK = displayed correctly · CRASH = hard-restarted the TV · — = untested. "TV UI" = set in
the TV's own matte menu; "API" = set via `upload(matte=…)`.

| Type | 16:9 | 4:3 | 2:3 portrait | 1:1 square | 21:9 (wider) |
|---|---|---|---|---|---|
| `none` | OK (fills) | OK (crops) | OK (crops) | OK (crops) | OK (crops left/right) |
| `flexible` | — | OK (TV UI + API) | OK (TV UI + API) | OK (TV UI + API) | OK (TV UI + API) |
| `shadowbox` | OK (`shadowbox_sage`, TV UI) | OK (TV UI + API) | OK (TV UI) | OK (TV UI) | OK (TV UI) |
| `modern` | OK (`modern_seafoam`, TV UI) | **CRASH** (`modern_polar`, API) | — | — | — |
| `modernthin` | OK (`modernthin_black`, TV UI) | — | — | — | — |
| `modernwide` `panoramic` `triptych` `mix` `squares` | — | — | — | — | — |

- **Verified-safe set for non-16:9 uploads: `flexible` and `shadowbox`.** Colors verified
  on non-16:9: `black` (all four shapes) and `byzantine` (`shadowbox`, 4:3); other colors
  are untested there.
- `modern` is the only type seen to crash, and only on 4:3; `modern_polar` on a portrait
  and a square were uploaded but never displayed (then deleted).

### 7.9 Implications for this project
- Pre-rendering a 3840×2160 `cover` crop (what the app did) and the TV's own `none`
  handling produce the **same crop**; the Pi's contribution was only a lossy upscale.
- A non-16:9 photo is shown **whole** only with a `flexible` / `shadowbox` matte, so an
  app that wants whole portraits must **set that matte at upload** (the only API
  path) and **must never send a type outside what the shape allows** (§7.6).
- Matte edits made in the TV UI are readable (§7.4), so the TV can be the matte editor
  and the app can learn the user's choice. Plan: SPEC.md §12.
- Wider-than-16:9 photos behave exactly like taller ones (§7.2, §7.3): cropped with `none`,
  shown whole with `flexible`/`shadowbox` — so no special case is needed for ultrawide.

### 7.10 Experiment protocol (reuse for any future probing of a live TV)
1. Use a **separate client `name`** so the production client's registration is untouched.
2. Take a read-only **baseline**: current art, art mode, and the full `available()`
   listing including matte, size and date for every item.
3. **Record every uploaded `content_id` to disk immediately.** Give test images a
   distinctive `date` (`2000:01:01 00:00:00`) so strays are recognizable on the TV.
4. Display only combinations you have verified or can recover from, **attended** — a
   person watching the TV and photographing each state. No unattended timed tours.
5. Cleanup restores the original artwork, deletes **only the recorded ids** (and refuses
   any id present in the baseline), then diffs baseline vs now (ids, matte, size, date).
6. **Confirm the art API answers first** (a bounded `get_artmode`): the TV can be awake and
   showing art while the API is wedged, and then every art call hangs (§8).
7. **Matte watcher.** To learn which matte types the TV UI offers for a shape without a
   read-back round trip per click, keep one connection open and poll `get_current()` every
   ~2 s, logging each change of `matte_id`/`portrait_matte_id` with a timestamp while the
   human clicks through the options; match photos to states by file timestamp.
8. Give every probe script a **hard alarm** (`signal.alarm`) and write progress to a file —
   never pipe it through a buffering filter such as `grep`/`tail`, which hides where it stalled.

### 7.11 M0 session log (2026-10-02)
Matte watcher output (TV-reported `matte_id` as the user clicked through the TV UI), and
whether the **whole** image showed (user observation + photos):

| Shape / image | Option 1 | Option 2 | Whole image shown? |
|---|---|---|---|
| 4:3 · 1600×1200 | `flexible_black` | `shadowbox_black` | Yes, both |
| 1:1 · 2000×2000 | `flexible_black` | `shadowbox_black` | Yes, both |
| 2:3 · 1500×2250 | `flexible_black` | `shadowbox_black` | Yes, both (`shadowbox` inset smaller) |
| 21:9 · 2560×1080 | `flexible_black` | `shadowbox_black` | Yes, both (`none` cropped left/right) |

**API-path check** (what the Pi actually does): `upload(matte='flexible_black',
portrait_matte='none')` of a 4:3, a 1:1 and a 21:9 image, each displayed once with
`select_image` — all displayed whole like the TV-UI version, no dialog, no hang; `select_image`
returned normally and `get_current()` reported `flexible_black` each time (observed live by
the user; not photographed). With 2026-10-01's API-set portrait `flexible_black` and 4:3
`shadowbox_byzantine`, `flexible_black` is now verified via the API on **all four non-16:9
shapes**. The TV was restored and verified identical to its pre-test baseline afterwards.

---

## 8. Gotchas & hard-won caveats

- **Getters can hang forever.** `get_auto_rotation_status()` never returns on this
  firmware (the TV emits no matching response event); the client blocks until it's
  killed. As a class, any art getter can hang. **Bound every art call** with a hard
  timeout (e.g. `SIGALRM` or a worker thread) and avoid known-hangers. Use the
  `slideshow` API, **not** `auto_rotation`.
- **Setters are effectively fire-and-forget** and may not echo state reliably →
  **treat your own database as the source of truth for *intended* config**, not the TV.
- **`-7` = invalid parameter** (e.g. bad slideshow duration). Catch
  `samsungtvws.exceptions.ResponseError`.
- **Buffered stdout hides hangs** — when scripting probes, run unbuffered (`python -u`)
  or you'll think it's stuck when it's just not flushed.
- **A rejected `set_slideshow_status` can leave slideshow `value=off`** — re-assert the
  desired config rather than assuming it stuck.
- Deleting the content_id that's currently displayed is asking for a flicker — switch
  the display away first, then delete.
- **A matte type that doesn't suit the image shape can crash the TV at display time**
  (error 40000, hard restart) even though the upload succeeded — §7.6. Validate before
  uploading; never display-test an unverified combination unattended.
- **`change_matte` → `-7`** on this firmware; mattes are upload-time only (§7.5).
- **`get_slideshow_status` read `value: 'off'`** (all other fields empty, plus a
  `sub_category_id` key not seen before) on 2026-10-01 with 41 photos resident — both
  before and after the experiment, which never touched the slideshow. Unexplained: the
  Pi was expected to be running one. Check the daemon/TV rather than assuming a
  slideshow is active.
- **`content_id`s are never reused.** After deleting `MY_F0107`–`MY_F0114` the next
  upload was `MY_F0115` (reinforces §9 point 4).
- **The art API can wedge while the TV looks perfectly fine (2026-10-02).** With the TV
  awake — port 8002 open, REST `PowerState=on`, and (user-reported) the **Art Mode slideshow
  visibly running** — every art call hung: the TLS websocket upgrade and `ms.channel.connect`
  arrived within ~0.2 s, but the art app never sent `ms.channel.ready`, so each call blocked
  until its timeout (the library's `open()` waits for that event, `art.py:83`). **Powering
  the TV off did not help — it only turns off the panel.** The web server/API runs on the
  external media box (the Frame's One Connect box), and **power-cycling that box recovered
  it** (user-reported, ~99 % sure). The trigger is unknown. Unproven suspects: the TV's
  hard restart the day before and/or accumulated half-open sessions from killed or
  timed-out clients. **A client-side leak was found and fixed (2026-10-02):** the project's
  `FrameClient.close()` closed only the remote socket, but `tv.art()` builds a *separate*
  `SamsungTVArt` with its own websocket that was never closed — and `_bounded()` abandoned
  a hung worker thread blocked in `recv()`, keeping it alive forever. It now closes both. **TCP/REST reachability is not proof the art API will answer.** Keep
  every art call bounded, treat a timeout as "not available now" (the project's
  `FrameTimeout`), and close clients promptly. *(An earlier version of this note guessed
  the TV was on another input / not in Art Mode; that was wrong.)*
- **A transient `ConnectionFailure` can come from another client's disconnect.** The
  library fails on any first frame other than `ms.channel.connect`/`ready`. On 2026-10-02
  the first frame was `ms.channel.clientDisconnect` (names are base64) for a *different*
  client (`FrameArtOrganizer`, the Pi's daemon, which connects and disconnects each tick)
  leaving at that moment. Retry once or twice rather than failing.
- **`get_slideshow_status` still read `off` (empty fields) on 2026-10-02**, yet the displayed
  item changed on its own between 10-01 and 10-02 (`MY_F0079` → `MY_F0082`) and the Pi
  swapped 9 photos. The user also saw the Art Mode slideshow running on screen while it read
  `off`, so the getter misreports on this firmware — don't gate logic on it.
- **The Pi's daemon churns the set between sessions:** between the 2026-10-01 and 10-02
  baselines 9 photos were added (`MY_F0117`–`F0125`) and 9 removed — evictions that never
  read the TV's matte, so any matte edit on an evicted photo would have been lost.
- **Physical (user-reported):** the TV was put inside a picture frame that can block the
  motion sensor; the sensor was switched off on 2026-10-02. A blocked sensor can make the
  TV sleep, closing port 8002 (it was unreachable at 17:06 that day and woke ~17:10).

---

## 9. Practical patterns (that this project uses)

1. **Reachability gate.** TCP-probe 8002 (and check `PowerState`) before any art call;
   if closed/standby → `FrameAsleep`, skip & retry. Prevents hangs and room-waking.
2. **Bounded calls.** Every art request wrapped in a hard timeout.
3. **DB-as-intent.** Because getters are unreliable and setters fire-and-forget, the
   local store owns intended state; the TV is reconciled toward it, best-effort.
4. **content_id is not identity.** It's TV-assigned, churny across factory reset, and
   category-scoped. Key your data on a content hash of the source, not `content_id`.
5. **Validate the matte against the shape before every upload** — the TV accepts bad
   combinations and crashes later (§7.6). Refuse unknown ones.
6. **The TV owns the matte; harvest before you evict.** The matte lives on the TV item
   and is edited in the TV UI (§7.4–7.5). Read `available()` *before* deleting an item so
   a user's choice can be re-applied when that photo is uploaded again.

---

## 10. References

- `samsungtvws` (xchwarze), PyPI 3.0.6 — the client used here.
- `NickWaterton/samsung-tv-ws-api` — Frame-focused fork, async art client, 2024
  handshake fixes; good cross-reference.
