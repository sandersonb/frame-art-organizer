# Samsung The Frame — Local Art API Reference

This reference summarizes observed protocol behavior and reproducible validation
methods, not a particular installation. Treat the API as reverse-engineered and
firmware-fragile; confirm behavior on the target firmware before expanding support.

| | |
|---|---|
| Scope of observations | Art API **`5.0.1.0`** (`art().get_api_version()`); not a guarantee for every TV |
| Client library | `samsungtvws` **3.0.6** (xchwarze, PyPI), sync API |
| Transport | WebSocket over TLS on **port 8002** + REST on 8001/8002 |

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
- **Pairing depends on the TV's access mode.** An "always allow" mode can admit a new
  client without an approval prompt or returned token. Do not rely on that behavior
  for another TV or firmware, and do not treat it as an authentication guarantee.
- **Check the allowed-clients list** when using "allow, prompting first time" mode.
  An unapproved client can wait silently on the WebSocket until someone presses Allow.
  Verify the configured client is approved before enabling background operation;
  also see the wedged-API and `clientDisconnect` gotchas in §8.
- **REST device info** (no token): `GET https://<host>:8002/api/v2/` returns model,
  `PowerState`, `TokenAuthSupport`, `FrameTVSupport`, `resolution`, `wifiMac`,
  `duid` (stable TV UUID across IP changes). Handy as a cheap liveness/identity check.

```jsonc
// GET /api/v2/ → selected device fields; placeholders are not device identifiers
"modelName": "<model-name>", "model": "<internal-model>",
"PowerState": "on", "TokenAuthSupport": "true", "FrameTVSupport": "true",
"resolution": "3840x2160"
```

Device identity fields are useful locally but should be removed from shared captures.

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
  - **Also confirmed: native-size, non-16:9 JPEGs** — 1500×2250, 2000×2000,
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
  `set_brightness_sensor_setting`, `set_color_temperature`. (These imply the application could
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

**Known limitation:** setting a real title is **not feasible**
with known tooling. No title/rename method exists in `samsungtvws` or NickWaterton's
fork (verified from source); the SmartThings/Art Store add-photo flow has no naming
field; the filename is never transmitted. The Frame parses **only** EXIF *orientation*
(for auto-rotate) and shows the *date* — no evidence it renders an embedded
EXIF `ImageDescription`, EXIF `XPTitle`, IPTC Object Name/Caption, or XMP `dc:title`.
**Embedding metadata did not change this:** a JPEG with the title embedded in EXIF
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
the backbone of the "host curates, TV cycles" architecture.

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

## 7. Aspect ratio, cropping & mattes

**Method.** Synthetic images with four distinctly colored edges (red top, blue bottom,
green left, yellow right) and 10/20/30 % inset outlines, so any crop or matte is
obvious: portrait 1500×2250 (2:3), square 2000×2000, 4:3 1600×1200, 16:9 1280×720.
Each was uploaded to `MY-C0002`, displayed one at a time, and **photographed on the
wall**. Mattes were set either in the TV's own Art-Mode matte UI or at upload. A
baseline of existing content must be taken first and verified unchanged afterwards
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
| 21:9 2560×1080 | **Full height** kept; **left/right cropped** — no green or yellow edge, no vertical outlines; red top and blue bottom visible |
| 16:9 1280×720 | **Whole image**; the TV upscales ×3 to fill the panel; fine text stays smooth                                                                             |

This is exactly a centered "cover" crop (`ImageOps.fit(centering=(0.5, 0.5))`) — what
this project's `cover` mode did — but done by the TV. **"Let the Frame handle it" with
no matte is a centered crop, not letterboxing.** The same holds for shapes wider than
16:9 (verified with a synthetic 21:9 image).

### 7.3 With a matte, non-16:9 photos are shown whole (fit)
- The observed TV UI offers **`flexible` and `shadowbox`** for the non-16:9 shapes
  2:3, 4:3, 1:1 and 21:9 (§7.11), with a broader set for 16:9.
- **Every** non-16:9 shape × {`flexible`, `shadowbox`} combination tried displayed the
  **whole image** (all four colored edges and every outline visible) inside the matte,
  with no crash. `flexible` sizes the photo to the panel's long edge (portrait: nearly
  full height; 21:9: nearly full width); `shadowbox` insets it with a margin on all sides
  and a drop shadow, so it shows smaller.
- **4:3 + `shadowbox_byzantine`**: centered, inset with a margin all round;
  **2:3 + `flexible_black`**: scaled to nearly the full panel height with a thin light edge.
- The result was **identical whether the matte was set in the TV UI or via
  `upload(matte=…)`** for every combination tested through both paths (§7.11).

### 7.4 Matte read-back — what the TV writes
- `available()` and `get_current()` report `matte_id` / `portrait_matte_id` and they
  **reflect edits made in the TV UI**: a user-set `modern_seafoam` on an existing 16:9
  photo, `shadowbox_byzantine` (4:3) and `flexible_black` (portrait) all read back
  correctly. Uploading both matte parameters as `none` reads back as `none` / `none`.
- Editing the matte of a **portrait, 4:3, square or 21:9** photo in the TV UI changed
  `matte_id` only; **`portrait_matte_id` stayed `none`** in every logged state.
- An API upload of a portrait photo with `matte='flexible_black', portrait_matte='none'`
  **displayed the matte**, supporting use of `matte_id` for portrait photos on
  landscape-mounted displays while `portrait_matte_id` stays `none`. Whether
  `portrait_matte_id` matters if the TV is physically rotated: untested.
- `upload()` round-tripped these `matte/portrait_matte` pairs exactly: `none/none`,
  `modern_polar/none`, `none/modern_polar`, `shadowbox_byzantine/none`,
  `flexible_black/none`.
- The 16:9 shape has accepted `modern`, `modernthin`, and `shadowbox` TV-UI choices
  that were subsequently read back through `available()`; see the shape matrix in §7.8.

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
artwork; a post-test baseline comparison found existing content unchanged.

- **Hypothesis (not proven):** the art app crashes when asked to render a matte type it
  does not offer for that aspect ratio. Supporting: the TV UI offers only two types for
  non-16:9, and the two combinations the UI itself produced — `shadowbox_byzantine` on
  4:3 and `flexible_black` on portrait — displayed fine when set **via the API at
  upload**. Only one failing combination was observed (`modern` + 4:3); `modern_polar`
  on a portrait and on a square were uploaded but never displayed (then deleted).
  **Additional validation:** the TV UI offers only `flexible`/`shadowbox` on every
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

`black` is charcoal (34,34,33), not pure black. TV display settings and camera exposure
can affect its apparent color; judge the default on the target display rather than a screenshot.

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
  handling produce the **same crop**; pre-rendering an upscale adds no source detail.
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

### 7.11 Compatibility validation matrix
The observed TV-UI matte choices and whole-image checks for synthetic fixtures are
summarized below. Repeat these checks when qualifying another firmware version:

| Shape / image | Option 1 | Option 2 | Whole image shown? |
|---|---|---|---|
| 4:3 · 1600×1200 | `flexible_black` | `shadowbox_black` | Yes, both |
| 1:1 · 2000×2000 | `flexible_black` | `shadowbox_black` | Yes, both |
| 2:3 · 1500×2250 | `flexible_black` | `shadowbox_black` | Yes, both (`shadowbox` inset smaller) |
| 21:9 · 2560×1080 | `flexible_black` | `shadowbox_black` | Yes, both (`none` cropped left/right) |

**API-path check** (what the application does): `upload(matte='flexible_black',
portrait_matte='none')` of a 4:3, a 1:1 and a 21:9 image, each displayed once with
`select_image` — all displayed whole like the TV-UI version, no dialog, no hang; `select_image`
returned normally and `get_current()` reported `flexible_black` each time. Together with
portrait checks and a 4:3 `shadowbox_byzantine` check, this supports the guarded
`flexible` / `shadowbox` policy for these shapes. Restore and verify the pre-test baseline
after running any such validation.

### 7.12 Migration validation checklist
Exercise the v1 → v2 code (`uploader.sync`, `harvest`, `scheduler.refresh`) using a
distinct client name and synthetic photos covering 16:9, 3:2, 4:3, portrait, square,
and ultrawide shapes. Follow §7.10 and leave unrelated slideshow settings untouched.

| Step | Expected validation |
|---|---|
| v1 seed | Legacy uploads are 3840×2160 with matte `none`; non-wide originals appear cropped. |
| TV edits | Change compatible mattes on synthetic 16:9 derivatives and confirm a dry-run harvest reads them back. |
| Canary | Replace a small subset including the displayed item; switch the display away before deletion. Retain a harvested matte only when the replacement has the same shape class. |
| Shape changes | A wide-only `modernwide` choice must not carry over to a whole portrait; use a compatible FIT matte instead. |
| Remaining shapes | Native-size whole-photo derivatives display completely with a compatible matte and report their uploaded sizes. |
| Settled | An unchanged repeat refresh and harvest are no-ops. |
| Rollback | Retained legacy derivatives can be selected again; only shape-compatible TV matte preferences are reapplied. |
| Cleanup | Delete only test IDs recorded during this validation, restore the original display, and verify existing content against the baseline. |

Batch duration depends on image sizes, the network, and the target devices; use small
attended canaries and bounded operations rather than assuming a particular throughput.

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
- **The slideshow getter can report `value: 'off'` while art is visibly rotating.**
  Other fields can be empty, and firmware may add fields such as `sub_category_id`.
  Check the daemon and the display rather than gating rotation logic on this getter.
- **Do not assume `content_id` reuse.** Upload IDs can continue increasing after
  deletions. Treat them as mutable placement attributes, not photo identities (§9).
- **The art API can wedge while the TV looks healthy.** Port 8002 can be open, REST
  can report `PowerState=on`, and a slideshow can visibly run while Art requests hang.
  A TLS/WebSocket upgrade and `ms.channel.connect` do not guarantee that the Art app
  will send `ms.channel.ready`. Bound the wait and treat a timeout as unavailable.
  On hardware with a separate media box, turning off only the panel may not restart
  the API; follow the manufacturer's instructions to restart the component hosting it.
  The cause of such wedges is not established by those symptoms alone.
- **Close both client sockets and bound cleanup.** `tv.art()` creates a separate
  `SamsungTVArt` WebSocket in addition to the remote-control socket. Closing only the
  remote socket leaks the Art connection; abandoning a blocked worker can keep it alive.
  The client must close both and reap failed workers. TCP/REST reachability is not proof
  that the Art API will answer, nor proof that a failure originated in the client.
- **A transient `ConnectionFailure` can come from another client's disconnect.** The
  library can reject an unexpected first frame such as `ms.channel.clientDisconnect`
  for another client. Client names in these events are base64-encoded. A small bounded
  retry can handle this condition; do not retry an uncertain upload automatically.
- **Harvest before rotating content out.** Deleting a resident item destroys TV-side
  matte edits unless they were read back first. Background rotation must respect the
  same harvest-first safety rule as a manual refresh.
- **Unexpected reachability loss can be normal TV sleep.** Check the target display's
  motion, ambient-light, and sleep settings before diagnosing a network failure. The
  organizer does not manage those settings.

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
