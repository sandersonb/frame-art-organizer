"""v2 render plan (SPEC.md §12.2): native resolution, downscale only, FILL vs FIT."""
import itertools
from pathlib import Path

import pytest
from PIL import Image

from frame_art_organizer import images, ingest, store
from frame_art_organizer.images import FRAME_H, FRAME_W, low_res, plan_render, shape_class, trim_fraction


# --- pure plan: the SPEC §12.2 table --------------------------------------------------------
@pytest.mark.parametrize("w,h,kind,crop,size,shape", [
    (1920, 1080, "fill", None, (1920, 1080), "wide"),             # 16:9 — untouched, TV scales
    (1280, 720, "fill", None, (1280, 720), "wide"),
    (3840, 2160, "fill", None, (3840, 2160), "wide"),
    (7680, 4320, "fill", None, (3840, 2160), "wide"),             # 8K: downscaled only
    (1920, 1200, "fill", (0, 60, 1920, 1140), (1920, 1080), "wide"),      # 16:10, 10 % trim
    (4928, 3264, "fill", (0, 246, 4928, 3018), (3840, 2160), "wide"),     # DSLR 3:2
    (6000, 4000, "fill", (0, 312, 6000, 3687), (3840, 2160), "wide"),     # exact 3:2 (15.6 %)
    (3000, 2000, "fill", (0, 156, 3000, 1844), (3000, 1688), "wide"),     # 3:2 below 4K: NOT upscaled
    (2400, 1200, "fill", (133, 0, 2266, 1200), (2133, 1200), "wide"),     # 2:1, trims columns
    (4032, 3024, "fit", None, (2880, 2160), "odd"),               # 4:3 phone
    (1600, 1200, "fit", None, (1600, 1200), "odd"),               # 4:3, unchanged
    (800, 600, "fit", None, (800, 600), "odd"),                   # tiny: never upscaled
    (5000, 5000, "fit", None, (2160, 2160), "odd"),               # square
    (2000, 3000, "fit", None, (1440, 2160), "odd"),               # portrait
    (2160, 3840, "fit", None, (1215, 2160), "odd"),               # 9:16
    (2560, 1080, "fit", None, (2560, 1080), "odd"),               # 21:9: no special case
    (6000, 2000, "fit", None, (3840, 1280), "odd"),               # 3:1 panorama
])
def test_plan_table(w, h, kind, crop, size, shape):
    p = plan_render(w, h)
    assert (p.kind, p.crop, p.size, p.shape) == (kind, crop, size, shape)
    assert p.source == (w, h)


def test_trim_fraction():
    assert trim_fraction(1920, 1080) == pytest.approx(0)
    assert trim_fraction(1920, 1200) == pytest.approx(0.10)
    assert trim_fraction(3, 2) == pytest.approx(0.15625)
    assert trim_fraction(4, 3) == pytest.approx(0.25)
    assert trim_fraction(1, 1) == pytest.approx(0.4375)
    assert trim_fraction(2, 3) == pytest.approx(0.625)


def test_the_tolerance_decides_whether_3_2_is_cropped():
    assert plan_render(4928, 3264, crop_tolerance=0.16).kind == "fill"    # the default: 3:2 counts as near
    assert plan_render(4928, 3264, crop_tolerance=0.15).kind == "fit"     # a literal 15 % would matte it
    assert plan_render(1600, 1200, crop_tolerance=0.30).kind == "fill"    # 4:3 trims 25 %
    assert plan_render(1920, 1200, crop_tolerance=0.0).kind == "fit"


@pytest.mark.parametrize("w,h", [(0, 10), (10, 0), (-1, 5)])
def test_non_positive_size_is_an_error(w, h):
    with pytest.raises(ValueError):
        plan_render(w, h)


def test_property_over_a_grid_of_sizes_and_tolerances():
    """Invariants that must hold for ANY input: never upscale, never exceed the panel, crops are
    centered 16:9 windows, FIT keeps the aspect ratio, FILL (non-tiny) is 'wide'."""
    dims = [1, 2, 3, 17, 100, 320, 800, 1280, 1600, 1920, 3000, 4032, 6000, 8000]
    for w, h, tol in itertools.product(dims, dims, (0.0, 0.16, 0.3)):
        p = plan_render(w, h, crop_tolerance=tol)
        ow, oh = p.size
        cw, ch = (p.crop[2] - p.crop[0], p.crop[3] - p.crop[1]) if p.crop else (w, h)
        ctx = (w, h, tol, p)

        assert ow <= FRAME_W and oh <= FRAME_H, ctx                       # never exceeds the panel
        assert p.scale <= 1.0 and ow <= cw and oh <= ch, ctx              # NEVER upscales
        assert 0 <= p.trim < 1, ctx
        if p.crop:
            left, top, right, bottom = p.crop
            assert 0 <= left < right <= w and 0 <= top < bottom <= h, ctx             # inside the source
            assert abs(left - (w - right)) <= 1 and abs(top - (h - bottom)) <= 1, ctx  # centered
            assert abs(cw / ch - 16 / 9) <= 2 / min(cw, ch) * (16 / 9), ctx            # a 16:9 window
        if p.kind == "fit":
            assert p.crop is None, ctx
            if min(ow, oh) >= 50:                                                      # aspect preserved
                assert abs(ow / oh - w / h) / (w / h) <= 1 / min(ow, oh), ctx
        if p.kind == "fill" and min(ow, oh) >= 100:
            assert p.shape == "wide", ctx
        assert p.shape == shape_class(ow, oh), ctx


@pytest.mark.parametrize("w,h,edge,expected", [
    (800, 600, 1280, True), (1280, 720, 1280, False), (1279, 2000, 1280, False),   # long edge counts
    (None, 600, 1280, False), (800, 600, 0, False),
])
def test_low_res(w, h, edge, expected):
    assert low_res(w, h, edge) is expected


# --- real rendering ------------------------------------------------------------------------
def _jpeg(path, w, h, color=(120, 130, 140), **save):
    Image.new("RGB", (w, h), color).save(path, "JPEG", quality=95, **save)
    return path


def _size(data_or_path):
    import io
    src = io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else data_or_path
    with Image.open(src) as im:
        return im.size


def test_a_small_photo_is_not_upscaled(tmp_path):
    data, plan = images.render_derivative(_jpeg(tmp_path / "a.jpg", 800, 600))
    assert _size(data) == (800, 600) and plan.kind == "fit" and plan.scale == 1.0


def test_a_big_near_16_9_photo_is_cropped_and_downscaled_to_the_panel(tmp_path):
    data, plan = images.render_derivative(_jpeg(tmp_path / "b.jpg", 4800, 3200))   # 3:2
    assert _size(data) == (3840, 2160) and plan.kind == "fill" and plan.scale == pytest.approx(0.8)


def test_fill_crops_the_center_not_the_edges(tmp_path):
    im = Image.new("RGB", (3000, 2000), (0, 200, 0))                 # green...
    im.paste((255, 0, 0), (0, 0, 3000, 100))                         # ...red top band
    im.paste((0, 0, 255), (0, 1900, 3000, 2000))                     # ...blue bottom band
    path = tmp_path / "bands.jpg"
    im.save(path, "JPEG", quality=95)

    data, plan = images.render_derivative(path)                      # 3:2 -> FILL -> rows 156..1844

    assert plan.kind == "fill" and plan.crop == (0, 156, 3000, 1844)
    import io
    out = Image.open(io.BytesIO(data))
    for y in (0, out.height // 2, out.height - 1):
        r, g, b = out.getpixel((out.width // 2, y))
        assert g > 150 and r < 80 and b < 80                         # only green: both bands cropped away


def test_a_fit_photo_keeps_every_edge(tmp_path):
    im = Image.new("RGB", (1500, 2250), (10, 10, 10))
    im.paste((255, 0, 0), (0, 0, 1500, 60))
    im.paste((0, 0, 255), (0, 2190, 1500, 2250))
    path = tmp_path / "portrait.jpg"
    im.save(path, "JPEG", quality=95)
    import io
    data, plan = images.render_derivative(path)
    out = Image.open(io.BytesIO(data))
    assert plan.kind == "fit" and out.size == (1440, 2160)
    assert out.getpixel((out.width // 2, 5))[0] > 200                # red top edge survives
    assert out.getpixel((out.width // 2, out.height - 5))[2] > 200   # blue bottom edge survives


def test_exif_orientation_is_applied_before_planning(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6                                                  # rotate 90° CW when displayed
    path = tmp_path / "rot.jpg"
    Image.new("RGB", (600, 400), (50, 60, 70)).save(path, "JPEG", exif=exif)
    data, plan = images.render_derivative(path)
    assert plan.source == (400, 600) and _size(data) == (400, 600) and plan.kind == "fit"


# --- render_to_file: auto is new, the v1 modes are unchanged ---------------------------------
def test_legacy_modes_still_force_3840x2160(tmp_path):
    src = _jpeg(tmp_path / "s.jpg", 800, 600)
    for mode in ("cover", "contain"):
        info = images.render_to_file(src, tmp_path / f"{mode}.jpg", mode=mode)
        assert (info["width"], info["height"], info["plan"]) == (3840, 2160, None)
        assert _size(tmp_path / f"{mode}.jpg") == (3840, 2160)          # the old upscale, preserved


def test_auto_mode_reports_the_real_size(tmp_path):
    src = _jpeg(tmp_path / "s.jpg", 800, 600)
    info = images.render_to_file(src, tmp_path / "auto.jpg", mode="auto")
    assert (info["width"], info["height"], info["plan"]) == (800, 600, "fit")
    assert _size(tmp_path / "auto.jpg") == (800, 600)


# --- ingest: derivatives get the real size and fit_mode 'auto' -------------------------------
def test_render_pending_in_auto_mode_records_real_dimensions(conn, tmp_path):
    originals = {"fit": (1600, 1200), "same": (1920, 1080), "crop": (3000, 2000)}
    for n, (name, (w, h)) in enumerate(originals.items(), start=1):
        path = _jpeg(tmp_path / f"{name}.jpg", w, h)
        store.insert_asset(conn, sha256=f"{n:064x}", original_path=str(path), original_name=path.name,
                           bytes_=1, mime="image/jpeg", width=w, height=h, captured_at=None)
    paths = {"derivatives": tmp_path / "derivs"}

    assert ingest.render_pending(conn, paths, "auto", 2, 90, crop_tolerance=0.16) == 3

    rows = {r["asset_id"]: r for r in conn.execute("SELECT * FROM derivative")}
    assert [(r["fit_mode"], r["pipeline_version"]) for r in rows.values()] == [("auto", 2)] * 3
    assert [(r["width"], r["height"]) for r in rows.values()] == [(1600, 1200), (1920, 1080), (3000, 1688)]
    for r in rows.values():                                          # the DB matches the file on disk
        assert _size(Path(r["path"])) == (r["width"], r["height"])
    assert ingest.render_pending(conn, paths, "auto", 2, 90) == 0     # idempotent
