"""Dress rehearsal for the live v1 -> v2 migration (SPEC.md §12.8 M3 / §12.12).

The real code runs end to end — real image files, real renders, real harvest, real uploads and
deletes — against a stateful MODEL of the TV. The model enforces the crash rule found on
2026-10-01: it raises if it is ever sent a matte type outside flexible/shadowbox for a
non-16:9 image. So if any code path could send that combination, these tests fail loudly.
"""
import io

import pytest
from PIL import Image

from frame_art_organizer import harvest, images, ingest, scheduler, store, uploader
from frame_art_organizer.mattes import ODD_ALLOWED, MatteConfig, valid_matte_string

# name -> source size, and what the v2 pipeline should produce from it (SPEC.md §12.2)
SOURCES = {"A": (1920, 1080), "B": (1500, 1000), "C": (1200, 900),
           "D": (800, 1200), "E": (1000, 1000), "F": (2400, 1000)}
V2_SIZE = {"A": (1920, 1080),    # 16:9 -> FILL, untouched (no upscale)
           "B": (1500, 844),     # 3:2 (15.6 %) -> FILL, centered 16:9 crop
           "C": (1200, 900),     # 4:3 -> FIT, whole
           "D": (800, 1200),     # portrait -> FIT
           "E": (1000, 1000),    # square -> FIT
           "F": (2400, 1000)}    # 2.4:1 -> FIT (no ultrawide special case)
V1 = dict(fit_mode="cover", pipeline_version=1)
V2 = dict(fit_mode="auto", pipeline_version=2)
PERIOD = {"name": "all-day", "collections": [], "interval": 15, "shuffle": True,
          "set_size": 6, "no_repeat_days": 30}


class LiveTV:
    """A stateful stand-in for the Frame: it keeps items, reports them, and CRASHES like the real one."""

    def __init__(self):
        self.items, self.n, self.shown, self.mode = {}, 0, None, "on"
        self.history, self.deleted_while_shown = [], []

    def upload_jpeg(self, data, matte="none", date=None):
        assert valid_matte_string(matte), f"client sent a matte outside the TV's vocabulary: {matte!r}"
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        if images.shape_class(w, h) == "odd" and matte != "none" and matte.split("_")[0] not in ODD_ALLOWED:
            raise AssertionError(f"TV CRASH (error 40000): {matte!r} on a {w}x{h} image")
        self.n += 1
        cid = f"MY_F{self.n:04d}"
        self.items[cid] = {"content_id": cid, "matte_id": matte, "portrait_matte_id": "none",
                           "width": w, "height": h}
        self.shown = self.shown or cid
        self.history.append(len(self.items))
        return cid

    def delete(self, cid):
        if cid == self.shown:
            self.deleted_while_shown.append(cid)
        self.items.pop(cid)
        self.history.append(len(self.items))
        return True

    def list_my_photos(self):
        return [dict(i) for i in self.items.values()]

    def artmode(self):
        return self.mode

    def current(self):
        return {"content_id": self.shown}

    def select(self, cid, show=True):
        self.shown = cid

    def set_slideshow(self, duration, shuffle=True, category_id="MY-C0002"):
        pass

    def user_edit(self, cid, matte):          # the user changes a matte in the TV's own UI
        self.items[cid]["matte_id"] = matte


def test_the_model_really_does_crash_like_the_tv():
    buf = io.BytesIO()
    Image.new("RGB", (600, 900)).save(buf, "JPEG")
    with pytest.raises(AssertionError, match="TV CRASH"):
        LiveTV().upload_jpeg(buf.getvalue(), matte="modern_polar")        # the 2026-10-01 combination
    LiveTV().upload_jpeg(buf.getvalue(), matte="flexible_black")           # the verified-safe one is fine


@pytest.fixture
def world(conn, tmp_path):
    """The library as it is today: six photos resident on the TV, rendered by the v1 pipeline."""
    dev = store.ensure_device(conn, name="Frame", host="192.0.2.10", duid="uuid:rehearsal")
    for n, (name, size) in enumerate(SOURCES.items(), start=1):
        path = tmp_path / f"{name}.jpg"
        Image.new("RGB", size, (40 * n % 256, 90, 160)).save(path, "JPEG")
        store.insert_asset(conn, sha256=f"{n:064x}", original_path=str(path), original_name=f"{name}.jpg",
                           bytes_=1, mime="image/jpeg", width=size[0], height=size[1], captured_at=None)
    paths = {"derivatives": tmp_path / "derivs"}
    assert ingest.render_pending(conn, paths, "cover", 1, 90) == 6
    tv = LiveTV()
    assert uploader.sync(tv, conn, device_id=dev, matte_cfg=MatteConfig(), **V1)["uploaded"] == 6
    assert {(i["width"], i["height"]) for i in tv.items.values()} == {(3840, 2160)}   # v1: all upscaled/cropped
    tv.history = [len(tv.items)]            # from here on, track only the migration itself
    return SimpleWorld(conn, tv, dev, paths)


class SimpleWorld:
    def __init__(self, conn, tv, dev, paths):
        self.conn, self.tv, self.dev, self.paths = conn, tv, dev, paths

    def cid(self, name):
        return next(p["content_id"] for p in store.present_placements(self.conn, self.dev)
                    if p["original_name"] == f"{name}.jpg")

    def refresh(self, cfg, **kw):
        return scheduler.refresh(self.tv, self.conn, device_id=self.dev, period=PERIOD,
                                 matte_cfg=MatteConfig(), **cfg, **kw)

    def render_v2(self):
        return ingest.render_pending(self.conn, self.paths, "auto", 2, 90, crop_tolerance=0.16)

    def on_tv(self):
        """{name: the TV's item} for everything we have resident."""
        return {p["original_name"][0]: self.tv.items[p["content_id"]]
                for p in store.present_placements(self.conn, self.dev)}

    def stale(self):
        return [p for p in store.present_placements(self.conn, self.dev) if p["pipeline_version"] == 1]


# --- the migration ------------------------------------------------------------------------------
def test_canary_then_full_swap_keeps_the_set_whole_and_applies_the_policy(world):
    w = world
    w.tv.user_edit(w.cid("A"), "modernthin_black")              # edits the user made on the TV earlier
    w.tv.user_edit(w.cid("D"), "shadowbox_sage")
    assert len(harvest.harvest(w.tv, w.conn, w.dev)) == 2       # ...which the harvest learned

    assert w.render_v2() == 6                                   # render everything BEFORE the first v2 refresh
    _, to_add, to_remove, _ = scheduler.plan(w.conn, device_id=w.dev, period=PERIOD, **V2)
    assert (len(to_add), len(to_remove)) == (6, 6)

    # --- canary: 3 photos
    r1 = w.refresh(V2, limit=3)
    assert (r1["added"], r1["removed"], r1["pending_adds"]) == (3, 3, 3)
    assert len(w.tv.items) == 6 and len(w.stale()) == 3         # still six photos; three migrated

    # --- the user edits a not-yet-migrated photo on the TV, mid-migration
    x = w.stale()[0]
    w.tv.user_edit(x["content_id"], "modern_polar")

    # --- the rest
    r2 = w.refresh(V2, limit=3)
    assert (r2["added"], r2["removed"], r2["pending_adds"], r2["harvested"]) == (3, 3, 0, 1)
    assert len(w.tv.items) == 6 and w.stale() == []

    final = w.on_tv()
    assert {n: (i["width"], i["height"]) for n, i in final.items()} == V2_SIZE      # native sizes, never upscaled
    expected = {"A": "modernthin_black",     # chosen on a 16:9 item, A is still 16:9 (FILL) -> carried over
                "B": "none", "C": "flexible_black",
                "D": "flexible_black",       # chosen as 'wide' (cropped v1) but D is now whole/odd -> NOT carried
                "E": "flexible_black", "F": "flexible_black"}
    xname = x["original_name"][0]
    if xname in ("A", "B"):                  # an edit made mid-migration is carried over only for 16:9 results
        expected[xname] = "modern_polar"
    assert {n: i["matte_id"] for n, i in final.items()} == expected

    # --- settled: nothing left to do, and nothing is re-uploaded
    r3 = w.refresh(V2)
    assert (r3["added"], r3["removed"], r3["harvested"]) == (0, 0, 0)
    assert min(w.tv.history) >= 6                               # the set NEVER shrank at any point
    assert max(w.tv.history) == 9                               # peak = set + one batch (add-before-remove)


def test_an_edit_made_on_a_cropped_portrait_never_reaches_the_whole_portrait(world):
    """The exact hazard from the experiment, end to end: the user picks `modern_polar` on the
    cropped 16:9 version of the portrait; the v2 portrait is 'odd', and the TV would crash on it."""
    w = world
    w.tv.user_edit(w.cid("D"), "modern_polar")
    assert harvest.harvest(w.tv, w.conn, w.dev)[0]["new"] == "modern_polar"
    w.render_v2()
    w.refresh(V2)                                               # LiveTV raises if modern_polar is ever sent
    assert w.on_tv()["D"]["matte_id"] == "flexible_black"


def test_an_unlimited_swap_peaks_at_twice_the_set(world):
    w = world
    w.render_v2()
    r = w.refresh(V2)
    assert (r["added"], r["removed"]) == (6, 6) and max(w.tv.history) == 12 and len(w.tv.items) == 6


def test_photos_without_a_rendered_replacement_stay_put(world):
    w = world
    w.render_v2()
    w.conn.execute("DELETE FROM derivative WHERE pipeline_version = 2 AND asset_id IN "
                   "(SELECT id FROM asset WHERE original_name IN ('C.jpg', 'E.jpg'))")
    w.conn.commit()                                             # simulate a half-finished render

    r = w.refresh(V2)

    assert (r["added"], r["removed"]) == (4, 4)
    assert len(w.tv.items) == 6 and min(w.tv.history) >= 6      # nothing was evicted for lack of a replacement
    assert {p["original_name"][0] for p in w.stale()} == {"C", "E"}


def test_the_photo_on_the_wall_is_never_deleted_while_it_is_showing(world):
    w = world
    assert w.tv.shown == w.cid("A")                             # the first upload is on the wall
    w.render_v2()
    w.refresh(V2)
    assert w.tv.deleted_while_shown == [] and w.tv.shown in w.tv.items


def test_when_the_tv_is_not_in_art_mode_the_swap_still_completes(world):
    w = world
    w.tv.mode = "off"                                           # someone is watching TV on the Frame
    selected = []
    w.tv.select = lambda cid, show=True: selected.append(cid)    # must not be called
    w.render_v2()
    r = w.refresh(V2)
    assert (r["added"], r["removed"]) == (6, 6) and selected == []


# --- rollback ---------------------------------------------------------------------------------
def test_rollback_restores_the_v1_set_and_keeps_every_preference(world):
    w = world
    w.tv.user_edit(w.cid("A"), "modernthin_black")
    w.tv.user_edit(w.cid("D"), "shadowbox_sage")
    harvest.harvest(w.tv, w.conn, w.dev)
    w.render_v2()
    w.refresh(V2)                                               # fully migrated
    assert w.on_tv()["C"]["width"] == 1200

    r = w.refresh(V1)                                           # config flipped back: cover, pipeline 1

    assert (r["added"], r["removed"]) == (6, 6) and len(w.tv.items) == 6 and min(w.tv.history) >= 6
    back = w.on_tv()
    assert {(i["width"], i["height"]) for i in back.values()} == {(3840, 2160)}      # the retained v1 derivatives
    prefs = store.asset_matte_prefs(w.conn)
    names = {row["id"]: row["original_name"][0] for row in store.list_assets(w.conn)}
    pref_by_name = {names[a]: m for a, (m, _) in prefs.items()}
    # v1 is 16:9 again, so the preferences chosen on 16:9 apply again — including D's
    assert {n: i["matte_id"] for n, i in back.items()} == {n: pref_by_name.get(n, "none") for n in back}
    assert back["D"]["matte_id"] == "shadowbox_sage" and back["A"]["matte_id"] == "modernthin_black"
