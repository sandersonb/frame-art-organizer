# Organizer UI Spec

The detailed design for the **Phase 3 web UI** (see `SPEC.md` §5.3 / Phase 3), based on
the proof-of-concept mockup. This supersedes the minimal v1 gallery already built in
`web.py` / `templates/index.html`.

**Guiding constraint:** reuse the existing model and store. The POC needs **one schema
column** (`collection.color`), a handful of thin endpoints (mostly loops over logic we
already have), and a substantial but self-contained **front-end** rebuild. No changes to
`asset` / `derivative` / `placement` / `ingest` / `scheduler` / `frame_client`.

> **Frame reality check:** `Rename` and per-photo display names are **local library
> labels only** — the Frame cannot show custom titles (proven in `SAMSUNG_FRAME_API.md`).
> Nothing in this UI changes what text appears on the TV; it only organizes our library.

---

## 1. Layout

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ HEADER   Frame Art Organizer · 12 photos        [Upload Photos] [▦|≣] [Sort ▾] │
├─────────────────────────────────────────────────────────────────────────────┤
│ FILTER   (All Photos 12) (⌗ No Collection 3) (● family 4) (● drew 1) …  (+ New)│
├─────────────────────────────────────────────────────────────────────────────┤
│ GALLERY  ┌ card ┐ ┌ card ┐ ┌ card ┐ ┌ card ┐ ┌ card ┐                          │
│          │ ☐  ⋯ │ …            (responsive grid, ~5 cols desktop)              │
│          └──────┘                                                              │
│  (BULK BAR appears here when ≥1 card selected)                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

Three regions: a sticky **header**, a **filter bar**, and a scrollable **gallery**. A
**bulk-action bar** overlays the bottom when a selection is active. Dark theme throughout.

---

## 2. Component inventory

Each component lists **behavior**, the **backend** it needs, and whether that backend is
**reuse** or **new**.

### 2.1 Header
- **Title + count** — "Frame Art Organizer" + live count of the *current filter* result.
  *Backend: reuse (`list_gallery` length / counts).*
- **Upload Photos button** — opens the **Upload modal** (2.7). *Backend: reuse `POST /upload`.*
- **View toggle (grid / list)** — switches the gallery layout. Client-only state, persisted
  in `localStorage`; re-renders the gallery fragment in the chosen layout. *Backend: the
  gallery fragment renders both layouts based on a `view` param; no data change.*
- **Sort dropdown** — Newest, Oldest, Name A–Z, Date taken. Changes gallery order.
  *Backend: **new** `sort` param on the gallery fragment (ORDER BY variants).*

### 2.2 Filter bar (collections)
A horizontal, scrollable row of **filter chips**. Two kinds:
- **System filters (virtual, always present):** `All Photos` (all non-deleted),
  `No Collection` (assets with zero collections), `broken` (assets `status='broken'`).
  Fixed styling; counts computed.
- **Collection chips (one per row in `collection`):** colored dot + name + count. Color
  from `collection.color`.
- **+ New Collection** — opens the **New Collection popover** (2.8).
- **Active chip** is outlined/highlighted; clicking a chip filters the gallery.
- *Backend: **new** `GET /gallery?filter=…` (filter = `all|none|broken|col:<id>`), and a
  **new** counts query (`counts_by_filter`) driving the chip badges. Reuses
  `list_collections` for names/colors.*

### 2.3 Photo card (grid)
- **Thumbnail** — `GET /thumb/{id}` *(reuse)*. Broken assets show a placeholder.
- **Select checkbox** (top-left overlay) — toggles the card into the selection set
  (client state). *No backend.*
- **Kebab menu button** (top-right overlay) — opens the **card context menu** (2.4).
- **Display name** — `title` if set, else `original_name`.
- **Date** — `captured_at` or "No date".
- **Collection tags** — color-coded pills; click a tag's ✕ to remove membership.
  *Backend for removal: **new** `POST /asset/{id}/collection/remove`.*

### 2.4 Card context menu (kebab)
Popover anchored to the button:
- **Add to collection ▸** — submenu listing collections (colored dots) + "New collection…".
  Selecting assigns. *Backend: reuse `POST /asset/{id}/collection`.*
- **Rename** — inline edit / small dialog → sets `asset.title`. *Backend: **new**
  `POST /asset/{id}/rename`. (Local label only.)*
- **View details** — opens the **Details modal** (2.6). *Backend: **new** `GET /asset/{id}`
  (fragment).* 
- **Download** — downloads the original. *Backend: **new** `GET /asset/{id}/download`
  (FileResponse of `original_path`, filename = `original_name`).*
- **Delete** — confirm → soft-delete. *Backend: reuse `POST /asset/{id}/delete`.*

### 2.5 Multi-select + bulk-action bar
- Selecting ≥1 card reveals a floating bar: "N selected · Add to collection · Delete ·
  Clear". A header "select all (in filter)" affordance selects the visible set.
- *Backend: **new** `POST /bulk/collection` and `POST /bulk/delete` (JSON/form list of
  `asset_ids`); both loop over the existing single-item logic.*

### 2.6 Details modal (View details)
Larger preview + metadata: display name, `original_name`, dimensions, byte size, mime,
`captured_at`, `imported_at`, `sha256`, collections, and **On-Frame status** (from
`present_placements_for_asset`: "On Frame (`MY_Fxxxx`)" or "Not resident"). *Backend:
**new** `GET /asset/{id}` fragment; all data via existing store helpers.*

### 2.7 Upload modal
Drag-and-drop zone + file picker (multiple, `image/*,.heic,.heif`), optional collection
selection, and per-file progress. Submits to the existing `POST /upload`. *Backend: reuse
(optionally add JSON progress later).* 

### 2.8 New Collection popover
Name field + **color swatch picker** (fixed palette). Creates the collection. *Backend:
**new** `POST /collection` (name + color) — wraps `get_or_create_collection` + sets color.*

---

## 3. Backend & data deltas

### Schema (migration 2 — additive, safe)
```sql
ALTER TABLE collection ADD COLUMN color TEXT;   -- palette key or hex; NULL = auto-assign
```
`ALTER TABLE … ADD COLUMN` is fully supported (unlike CHECK changes), so this is a clean
forward migration — no table rebuild.

### New / changed endpoints (all thin; reuse store logic)
| Method | Path | Purpose | Backing |
|---|---|---|---|
| GET | `/gallery` | filtered + sorted + view gallery **fragment** | new `store.list_gallery(filter, sort)` |
| GET | `/filterbar` | chip counts **fragment** (or embedded in `/gallery`) | new `store.counts_by_filter()` |
| POST | `/collection` | create collection (name, color) | `get_or_create_collection` + color |
| POST | `/asset/{id}/rename` | set `title` | new `store.rename_asset` |
| POST | `/asset/{id}/collection` | assign (existing) | reuse |
| POST | `/asset/{id}/collection/remove` | unassign | new `store.remove_from_collection` |
| GET | `/asset/{id}` | details fragment | reuse getters |
| GET | `/asset/{id}/download` | original file | `FileResponse` |
| POST | `/asset/{id}/delete` | soft-delete (existing) | reuse |
| POST | `/bulk/collection` | assign many | loop reuse |
| POST | `/bulk/delete` | delete many | loop reuse |

### New store helpers
`list_gallery(filter, sort)`, `counts_by_filter()`, `rename_asset(id, title)`,
`remove_from_collection(asset_id, collection_id)`, `create_collection(name, color)` (or
extend `get_or_create_collection` with color). Existing helpers otherwise cover it.

---

## 4. Front-end architecture

**Stack:** keep server-rendered Jinja + **HTMX** for server round-trips; add **Alpine.js**
(CDN, no build) for the *client-only* widgets — selection state, menus, submenus, modals,
view-toggle. This keeps hand-written JS minimal and declarative. *(Decision — see §6.)*

**Templates (split the current monolith):**
```
templates/
  index.html      shell: header, filter bar, gallery container, modals mount
  _filterbar.html chips + counts        (swapped on collection create / count change)
  _gallery.html   grid|list of cards     (swapped on filter / sort / view change)
  _card.html      one card               (swapped after assign/rename)
  _details.html   details modal body     (loaded into modal on demand)
  _menu.html      (optional) card menu markup
```

**HTMX interactions:** filter chip → `hx-get /gallery?filter=…` (swap `#gallery`); sort/view
→ `hx-get /gallery` with params; assign/remove collection → `hx-post` → swap `#card-{id}`;
delete → `hx-post` → remove card + refresh counts (`hx-trigger` the filter bar); create
collection → `hx-post` → swap `#filterbar`; upload → form post (or `hx-post` for progress).

**Alpine state:** `selection` (Set of ids → bulk bar), `menuOpenFor`, `modal`, `view`
(persisted). Context-menu positioning + outside-click close handled in Alpine.

**Design tokens (CSS variables):**
- Surfaces `#16181d` / `#1d2028`, border `#2a2e37`, text `#e6e8ec` / muted `#8a90a0`,
  accent `#3b82f6`; radius 12px; card grid `minmax(220px, 1fr)`.
- **Collection color palette** (assignable, `collection.color` stores the key):
  blue `#3b82f6`, purple `#8b5cf6`, green `#10b981`, red `#ef4444`, amber `#f59e0b`,
  teal `#14b8a6`, pink `#ec4899`, indigo `#6366f1`. Auto-assign the next unused on create.

---

## 5. Reuse vs. new (summary)

**Reused as-is:** `asset`/`derivative`/`placement`/`collection` schema (minus the color
column), `ingest_file`, thumbnails, `/upload`, `/thumb`, delete, assign-collection,
`get_asset`/`get_any_derivative`/`present_placements_for_asset`/`list_collections`.

**New (small):** `collection.color` column; ~6 thin endpoints (fragments + download + bulk +
rename + remove-from-collection); ~5 store helpers (filter/sort/counts/rename/remove).

**New (substantial, front-end only):** the header toolbar, filter-chip bar, redesigned card
(checkbox + kebab), context menu + submenu, multi-select bulk bar, details modal, upload
modal, new-collection popover, grid/list views, sort — split into partials + Alpine/HTMX.

---

## 6. Open decisions
1. **Client lib:** Alpine.js for menus/modals/selection (recommended) vs. hand-rolled
   vanilla JS. Alpine keeps it declarative and tiny; vanilla avoids one more dependency.
2. **Collection color:** user-picked from the palette (recommended) vs. purely auto-assigned.
3. **List view scope:** ship both grid + list in the first pass, or grid-first and add list
   later.
4. **Rename semantics:** confirm `Rename` sets a **local** `title` shown only in the UI
   (never on the Frame). (Recommended — it's the only option the platform allows.)
5. **"broken" chip:** keep broken as a system filter (recommended) vs. let users clear/hide
   broken assets.
6. **Bulk "remove from collection":** include in the first pass or defer.

---

## 7. Suggested build order
1. **Migration 2 + store helpers** (color, filter/sort/counts/rename/remove) + the new
   endpoints. Thin, testable via curl.
2. **Template split + design tokens**, HTMX wiring for filter/sort/view (server side proven
   first).
3. **Card redesign** (checkbox + kebab + tags) and the **context menu** (Alpine).
4. **Multi-select + bulk bar.**
5. **Modals:** details, upload, new-collection popover.
6. Polish: list view, empty states, mobile breakpoints.
