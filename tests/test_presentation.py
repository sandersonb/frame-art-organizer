"""Per-photo crop/matte geometry, immutable cache keys and safe TV replacement."""
import io

import pytest
from PIL import Image

from conftest import add_asset, add_derivative, add_device, add_placement, item
from frame_art_organizer import batches, cli, config, db, images, ingest, mattes, scheduler, store, uploader
from frame_art_organizer.frame_client import FrameTimeout
from test_wiring import FakeClient, PERIOD


@pytest.mark.parametrize("size", [(3000, 1000), (1600, 1200), (2000, 3000), (5000, 5000),
                                 (1920, 1080), (800, 600), (1, 100), (100, 1)])
@pytest.mark.parametrize("position", [0, 237, 500, 1000])
def test_crop_geometry_is_bounded_and_downscale_only(size, position):
    w, h = size
    p = images.plan_render(w, h, mode="crop", position=position)
    l, t, r, b = p.crop or (0, 0, w, h)
    assert p.kind == "fill"
    assert 0 <= l < r <= w and 0 <= t < b <= h
    assert l == (w - (r - l)) * position // 1000
    assert t == (h - (b - t)) * position // 1000
    assert p.size[0] <= min(r - l, 3840) and p.size[1] <= min(b - t, 2160)
    assert p.scale <= 1
    if min(size) > 100:
        assert p.size[0] / p.size[1] == pytest.approx(16 / 9, rel=.01)


@pytest.mark.parametrize("size", [(3000, 1000), (1600, 1200), (1920, 1080), (2000, 3000)])
def test_matte_never_crops_even_a_near_wide_photo(size):
    p = images.plan_render(*size, mode="matte")
    assert p.kind == "fit" and p.crop is None
    assert p.size[0] / p.size[1] == pytest.approx(size[0] / size[1], rel=.01)
    assert p.scale <= 1


@pytest.mark.parametrize("mode,position", [("zoom", 500), ("crop", -1), ("crop", 1001),
                                           ("crop", .5), ("crop", True)])
def test_invalid_presentation_rejected(mode, position):
    with pytest.raises(ValueError):
        images.plan_render(1600, 1200, mode=mode, position=position)


def test_render_crops_the_selected_edge_from_an_exif_oriented_original(tmp_path):
    # EXIF rotates this into a 900x1600 portrait with red at the top, blue at the bottom.
    src = tmp_path / "rotated.jpg"
    im = Image.new("RGB", (1600, 900), "red")
    im.paste("blue", (800, 0, 1600, 900))
    exif = Image.Exif()
    exif[274] = 6
    im.save(src, exif=exif)
    original = src.read_bytes()
    for pos, channel in [(0, 0), (1000, 2)]:
        data, plan = images.render_derivative(src, mode="crop", position=pos)
        assert plan.source == (900, 1600)
        with Image.open(io.BytesIO(data)) as crop:
            rgb = crop.getpixel((crop.width // 2, crop.height // 2))
            assert rgb[channel] > 230
            assert crop.getexif().get(274) is None
    assert src.read_bytes() == original


def test_both_shape_matte_choices_survive_switching_modes(conn):
    a = add_asset(conn)
    store.set_asset_matte(conn, a, "shadowbox_sage", "odd")
    store.set_asset_matte(conn, a, "modern_black", "wide")
    conn.commit()
    assert store.get_matte_pref(conn, a, "odd") == ("shadowbox_sage", "odd")
    assert store.get_matte_pref(conn, a, "wide") == ("modern_black", "wide")
    cfg = mattes.MatteConfig(auto_matte=False)
    assert mattes.matte_for("modern_black", "wide", 1920, 1080, cfg, fit_mode="crop-500") == "none"
    assert mattes.matte_for("none", "odd", 1200, 1600, cfg, fit_mode="matte") == "flexible_black"
    assert mattes.matte_for(None, None, 1920, 1080, cfg, fit_mode="matte") == "flexible_black"
    assert mattes.matte_for("modern_black", "wide", 1200, 1600, cfg, fit_mode="matte") == "flexible_black"
    assert mattes.matte_for("modern_black", "odd", 1200, 1600, cfg, fit_mode="matte") == "flexible_black"


def test_migration_backfills_preferences_without_changing_existing_pixels_or_placements(tmp_path):
    c = db.connect(tmp_path / "v5.db")
    for script in db.MIGRATIONS[:5]:
        c.executescript(script)
    c.execute("PRAGMA user_version=5")
    a = add_asset(c)
    dev = add_device(c)
    d = add_derivative(c, a)
    p = add_placement(c, dev, d, "MY_F0001", matte="modern_black")
    c.execute("INSERT INTO asset_policy(asset_id,pinned,matte,matte_shape) VALUES (?,1,'modern_black','wide')", (a,))
    c.commit()
    before = tuple(c.execute("SELECT * FROM placement WHERE id=?", (p,)).fetchone())
    assert db.migrate(c) == 6
    assert store.get_presentation(c, a) == ("auto", 500)
    assert store.get_matte_pref(c, a, "wide") == ("modern_black", "wide")
    assert tuple(c.execute("SELECT * FROM placement WHERE id=?", (p,)).fetchone()) == before
    assert store.get_any_derivative(c, a, "cover", 1)["id"] == d
    assert c.execute("SELECT pinned FROM asset_policy WHERE asset_id=?", (a,)).fetchone()[0] == 1
    c.close()


def set_mode(conn, asset_id, mode, position=500):
    with conn:
        store.set_presentation(conn, asset_id, mode, position)


def test_every_selection_path_uses_the_photos_pixel_key(conn):
    dev = add_device(conn)
    a = add_asset(conn)
    auto = add_derivative(conn, a, fit="auto", pv=2)
    crop = add_derivative(conn, a, fit="crop-237", pv=2)
    matte = add_derivative(conn, a, fit="matte", pv=2)
    for mode, position, wanted in [("crop", 237, crop), ("matte", 500, matte), ("auto", 500, auto)]:
        set_mode(conn, a, mode, position)
        assert store.get_any_derivative(conn, a, "auto", 2)["id"] == wanted
        assert store.list_gallery(conn, fit_mode="auto", pipeline_version=2)[0]["d_id"] == wanted
        assert store.derivatives_to_upload(conn, dev, "auto", 2)[0]["derivative_id"] == wanted
        desired = store.compose_working_set(conn, device_id=dev, collections=[], fit_mode="auto",
                                           pipeline_version=2, no_repeat_days=30, set_size=1)
        assert [r["derivative_id"] for r in desired] == [wanted]
        assert store.assets_needing_render(conn, "auto", 2) == []


def test_unrendered_override_keeps_resident_photo_and_render_pending_honors_it(conn, tmp_path):
    src = tmp_path / "source.jpg"
    Image.new("RGB", (900, 1200), "red").save(src)
    a = store.insert_asset(conn, sha256="abc", original_path=str(src), original_name=src.name,
                           bytes_=1, mime="image/jpeg", width=900, height=1200, captured_at=None)
    dev = add_device(conn)
    old = add_derivative(conn, a, fit="auto", pv=2)
    add_placement(conn, dev, old, "MY_F0001")
    set_mode(conn, a, "crop", 1000)
    assert [r["id"] for r in store.assets_needing_render(conn, "auto", 2)] == [a]
    assert scheduler.plan(conn, device_id=dev, period=PERIOD, fit_mode="auto", pipeline_version=2)[2] == []
    assert ingest.render_pending(conn, {"derivatives": tmp_path / "d"}, "auto", 2, 90) == 1
    d = store.get_any_derivative(conn, a, "auto", 2)
    assert d["fit_mode"] == "crop-1000" and d["width"] == 900 and d["height"] == 506
    assert ingest.render_pending(conn, {"derivatives": tmp_path / "d"}, "auto", 2, 90) == 0


def crop_world(conn, tmp_path):
    dev = add_device(conn)
    a = add_asset(conn, width=1600, height=1200)
    old = add_derivative(conn, a, width=1600, height=1200, fit="auto", pv=2)
    add_placement(conn, dev, old, "MY_F0001", matte="flexible_black")
    src = tmp_path / "crop.jpg"
    src.write_bytes(b"new jpeg")
    new = add_derivative(conn, a, width=1600, height=900, fit="crop-0", pv=2, path=str(src))
    set_mode(conn, a, "crop", 0)
    return dev, a, old, new


def refresh(conn, client, dev):
    return scheduler.refresh(client, conn, device_id=dev, period=PERIOD, fit_mode="auto",
                             pipeline_version=2, matte_cfg=mattes.MatteConfig())


def test_crop_swap_harvests_latest_matte_then_adds_before_removing(conn, tmp_path):
    dev, a, old, new = crop_world(conn, tmp_path)
    client = FakeClient([item("MY_F0001", "shadowbox_sage")])
    res = refresh(conn, client, dev)
    assert (res["added"], res["removed"]) == (1, 1)
    assert client.calls.index("list") < client.calls.index(("upload", "none")) < client.calls.index(("delete", "MY_F0001"))
    assert store.get_matte_pref(conn, a, "odd") == ("shadowbox_sage", "odd")
    assert [p["derivative_id"] for p in store.present_placements(conn, dev)] == [new]


def test_crop_swap_never_evicts_if_tv_cannot_be_harvested(conn, tmp_path):
    dev, _, old, _ = crop_world(conn, tmp_path)
    client = FakeClient(list_exc=FrameTimeout("sleep"))
    with pytest.raises(FrameTimeout):
        refresh(conn, client, dev)
    assert client.calls == ["list"]
    assert store.present_placements(conn, dev)[0]["derivative_id"] == old


def test_failed_replacement_upload_keeps_old_photo(conn, tmp_path):
    dev, _, old, _ = crop_world(conn, tmp_path)
    class RejectingClient(FakeClient):
        def upload_jpeg(self, *a, **k):
            raise ValueError("upload rejected")
    client = RejectingClient([item("MY_F0001")])
    res = refresh(conn, client, dev)
    assert res["errors"] == 1 and res["removed"] == 0
    assert store.present_placements(conn, dev)[0]["derivative_id"] == old


def test_a_paused_batch_is_superseded_when_crop_settings_change(conn, tmp_path):
    dev, _, old, new = crop_world(conn, tmp_path)
    batch = batches.start(conn, dev, batches.key(PERIOD, "auto", 2), [{"derivative_id": old}])
    assert batches.desired_rows(conn, batch, PERIOD) is None
    client = FakeClient([item("MY_F0001")])
    result = refresh(conn, client, dev)
    assert result["batch_id"] != batch["id"] and result["complete"]
    assert conn.execute("SELECT state FROM refresh_batch WHERE id=?", (batch["id"],)).fetchone()[0] == "superseded"
    assert store.present_placements(conn, dev)[0]["derivative_id"] == new


def test_sync_and_plan_report_honor_manual_matte_even_with_auto_matte_off(conn, tmp_path):
    dev = add_device(conn)
    a = add_asset(conn, width=1920, height=1080)
    path = tmp_path / "whole.jpg"
    path.write_bytes(b"jpeg")
    add_derivative(conn, a, fit="matte", pv=2, path=str(path), width=1920, height=1080)
    set_mode(conn, a, "matte")
    img = config.image_settings({"image": {"default_fit": "auto", "pipeline_version": 2, "auto_matte": False}})
    row = cli._plan_rows(conn, img, .16)[0]
    assert row["kind"] == "fit" and row["matte"] == "flexible_black"
    client = FakeClient()
    assert uploader.sync(client, conn, device_id=dev, fit_mode="auto", pipeline_version=2,
                         matte_cfg=img.matte)["uploaded"] == 1
    assert ("upload", "flexible_black") in client.calls


def test_purge_protects_the_selected_manual_render_and_live_old_copies(conn):
    dev = add_device(conn)
    a = add_asset(conn)
    old = add_derivative(conn, a, fit="auto", pv=2)
    current = add_derivative(conn, a, fit="crop-0", pv=2)
    set_mode(conn, a, "crop", 0)
    p = add_placement(conn, dev, old, "MY_F0001")
    assert store.stale_resident_count(conn, "auto", 2) == 1
    assert store.superseded_derivatives(conn, "auto", 2) == []
    store.set_placement_deleted(conn, p)
    add_placement(conn, dev, current, "MY_F0002")
    assert store.stale_resident_count(conn, "auto", 2) == 0
    assert [r["id"] for r in store.superseded_derivatives(conn, "auto", 2)] == [old]


def test_concurrent_renders_publish_one_complete_derivative_without_clobbering_files(conn, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from threading import Barrier

    src = tmp_path / "source.jpg"
    Image.new("RGB", (900, 1200), "red").save(src)
    a = store.insert_asset(conn, sha256="abc", original_path=str(src), original_name=src.name,
                           bytes_=1, mime="image/jpeg", width=900, height=1200, captured_at=None)
    barrier = Barrier(2)
    render = images.render_to_file
    def synchronized_render(*args, **kwargs):
        info = render(*args, **kwargs)
        barrier.wait(timeout=10)
        return info
    monkeypatch.setattr(images, "render_to_file", synchronized_render)
    paths = {"derivatives": tmp_path / "d"}
    def work():
        c = db.connect(tmp_path / "state.db")
        try:
            return ingest.ensure_derivative(c, paths, store.get_asset(c, a), "crop-237", 2, 92)["id"]
        finally:
            c.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: work(), range(2)))
    assert ids[0] == ids[1]
    rows = conn.execute("SELECT * FROM derivative WHERE asset_id=?", (a,)).fetchall()
    assert len(rows) == 1
    assert list(paths["derivatives"].iterdir()) == [Path(rows[0]["path"])]
    assert images.sha256_bytes(Path(rows[0]["path"]).read_bytes()) == rows[0]["sha256"]
