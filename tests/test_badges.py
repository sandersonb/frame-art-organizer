"""Gallery badges (SPEC.md §12.7): what each photo looks like on the Frame, from the derivative
that is actually rendered — and, under the v2 pipeline, identical to `fao plan-report`."""
import pytest
from PIL import Image

from frame_art_organizer import badges, cli, config, ingest, store
from frame_art_organizer.badges import describe

IMG = config.ImageSettings()          # defaults: low_res 1280, fit matte flexible_black, auto_matte on


def look(src, deriv, fit="auto", pref=(None, None), img=IMG):
    return describe(*src, *deriv, fit, *pref, img)


def texts(lk):
    return [b.text for b in lk.badges]


# --- what the badge says ----------------------------------------------------------------------
def test_a_16x9_photo_has_no_badge_at_all():
    lk = look((1920, 1080), (1920, 1080))
    assert lk.render == "full" and lk.badges == [] and lk.crop_pct is None


def test_a_near_16x9_photo_is_cropped_and_says_by_how_much():
    lk = look((1500, 1000), (1500, 844))                        # a 3:2, trimmed 15.6 %
    assert (lk.render, lk.crop_pct, texts(lk)) == ("crop", 16, ["cropped 16 %"])
    assert lk.matte == "none"                                   # full-bleed 16:9 -> no matte


def test_a_photo_kept_whole_shows_the_matte_it_will_wear():
    lk = look((2000, 1500), (2000, 1500))                         # 4:3 -> FIT
    assert (lk.render, lk.matte, texts(lk)) == ("fit", "flexible_black", ["fit · flexible_black"])
    assert lk.crop_pct is None


def test_a_matte_chosen_on_the_tv_for_that_shape_is_what_the_badge_shows():
    lk = look((2000, 1500), (2000, 1500), pref=("shadowbox_sage", "odd"))
    assert lk.matte == "shadowbox_sage" and texts(lk) == ["fit · shadowbox_sage"]


def test_a_matte_chosen_for_the_other_shape_is_ignored_exactly_like_the_upload_ignores_it():
    """Chosen on the cropped 16:9 version of a portrait (shape 'wide'); the portrait is now whole."""
    lk = look((800, 1200), (800, 1200), pref=("modernwide_black", "wide"))
    assert lk.matte == "flexible_black"                         # never the crash-prone combination


def test_with_auto_matte_off_the_tv_crops_a_whole_photo_and_the_badge_says_so():
    img = config.ImageSettings(auto_matte=False)
    lk = look((2000, 1500), (2000, 1500), img=img)                # 4:3, matte 'none'
    assert (lk.render, lk.crop_pct, texts(lk)) == ("tvcrop", 25, ["cropped 25 % by the TV"])


def test_the_v1_pipeline_is_described_truthfully():
    """Under cover (v1) every photo is cropped to 16:9 — so a portrait says so."""
    assert texts(look((2000, 1500), (3840, 2160), fit="cover")) == ["cropped 25 %"]
    assert texts(look((1600, 2400), (3840, 2160), fit="cover")) == ["cropped 62 %"]
    assert look((1920, 1080), (3840, 2160), fit="cover").badges == []
    assert texts(look((2000, 1500), (3840, 2160), fit="contain")) == ["letterboxed"]


def test_low_res_is_flagged_but_never_blocks_and_uses_the_source_size():
    lk = look((800, 600), (800, 600))
    assert lk.low_res and texts(lk) == ["fit · flexible_black", "low-res 800×600"]
    assert not look((1280, 720), (1280, 720)).low_res           # exactly at the threshold is fine


def test_a_photo_with_no_derivative_says_so_instead_of_guessing():
    lk = look((2000, 1500), (None, None))
    assert lk.render == "none" and texts(lk) == ["not rendered yet"]
    assert texts(look((800, 600), (None, None))) == ["not rendered yet", "low-res 800×600"]


def test_unknown_source_dimensions_never_crash():
    assert look((None, None), (3840, 2160)).badges == []


# --- the exit criterion: badges match `fao plan-report` ----------------------------------------
SOURCES = {"A": (1920, 1080), "B": (1500, 1000), "C": (1200, 900), "D": (800, 1200),
           "E": (1000, 1000), "F": (2400, 1000), "G": (800, 600), "H": (4000, 3000),
           "I": (1600, 1050), "J": (3000, 3000)}


def test_badges_match_the_plan_report_for_every_shape(conn, tmp_path):
    for n, (name, (w, h)) in enumerate(SOURCES.items(), start=1):
        p = tmp_path / f"{name}.jpg"
        Image.new("RGB", (w, h)).save(p, "JPEG")
        store.insert_asset(conn, sha256=f"{n:064x}", original_path=str(p), original_name=p.name,
                           bytes_=1, mime="image/jpeg", width=w, height=h, captured_at=None)
    ids = {a["original_name"][0]: a["id"] for a in store.list_assets(conn)}
    store.set_asset_matte(conn, ids["A"], "modernthin_black", "wide")       # TV edits, harvested earlier
    store.set_asset_matte(conn, ids["C"], "shadowbox_sage", "odd")
    store.set_asset_matte(conn, ids["D"], "modernwide_black", "wide")       # wrong shape for a whole portrait
    assert ingest.render_pending(conn, {"derivatives": tmp_path / "d"}, "auto", 2, 90,
                                 crop_tolerance=IMG.crop_tolerance) == len(SOURCES)

    rows = {r["name"][0]: r for r in cli._plan_rows(conn, IMG, IMG.crop_tolerance)}
    prefs = store.asset_matte_prefs(conn)
    assert len(rows) == len(SOURCES)
    for name, (w, h) in SOURCES.items():
        d = store.get_any_derivative(conn, ids[name], "auto", 2)
        lk = describe(w, h, d["width"], d["height"], d["fit_mode"], *prefs.get(ids[name], (None, None)), IMG)
        r = rows[name]
        assert (d["width"], d["height"]) == r["size"], name                  # the render IS the plan
        assert (lk.render in ("full", "crop")) == (r["kind"] == "fill"), name
        assert (lk.render == "fit") == (r["kind"] == "fit"), name
        assert lk.matte == r["matte"], name
        assert lk.low_res == r["low_res"], name
        if r["kind"] == "fill" and r["trim"] > 0.005:
            assert lk.crop_pct == round(r["trim"] * 100), name               # same number, rounded
    assert describe(1920, 1080, 1920, 1080, "auto", "modernthin_black", "wide", IMG).matte == "modernthin_black"
    d_row = rows["D"]
    assert d_row["matte"] == "flexible_black" and not d_row["tv_choice"]     # the dangerous carry-over is refused in both
