"""Crop/matte editor over HTTP. All rendering is local; no TV access is allowed."""
import io
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("fastapi")
from test_web_gallery import V2, library, make_client  # noqa: E402,F401
from conftest import add_asset, add_device, add_placement  # noqa: E402
from frame_art_organizer import images, store, web  # noqa: E402


@pytest.fixture
def photo(tmp_path, library, monkeypatch):
    def no_tv(*a, **k):
        pytest.fail("editing must never open a TV connection")
    monkeypatch.setattr(web, "FrameClient", no_tv)
    client = make_client(tmp_path, V2)
    im = Image.new("RGB", (600, 800), "green")
    im.paste("red", (0, 0, 600, 300))
    im.paste("blue", (0, 500, 600, 800))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    data = buf.getvalue()
    r = client.post("/upload", files={"files": ("portrait.jpg", data, "image/jpeg")})
    assert r.status_code == 200
    a = library.execute("SELECT id FROM asset").fetchone()[0]
    return client, a, data


def save(client, asset_id, mode, position=500):
    r = client.post(f"/asset/{asset_id}/presentation", data={"mode": mode, "position": position})
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_still_auto_detects_and_editor_has_whole_original_geometry(photo, library):
    client, a, _ = photo
    d = store.get_any_derivative(library, a, "auto", 2)
    assert (d["fit_mode"], d["width"], d["height"]) == ("auto", 600, 800)
    e = client.get(f"/asset/{a}/presentation").json()
    assert e == {"id": a, "name": "portrait.jpg", "width": 600, "height": 800,
                 "mode": "auto", "position": 500, "crop_width": 600, "crop_height": 338,
                 "trim_pct": 58, "auto_kind": "fit"}


def test_saving_regenerates_selected_pixels_without_mutating_original_or_old_render(photo, library):
    client, a, original = photo
    asset = store.get_asset(library, a)
    old = store.get_any_derivative(library, a, "auto", 2)
    old_pixels = Path(old["path"]).read_bytes()
    results = []
    for pos, channel in [(0, 0), (1000, 2)]:
        result = save(client, a, "crop", pos)
        results.append(result["derivative_id"])
        d = store.get_any_derivative(library, a, "auto", 2)
        assert d["id"] == result["derivative_id"] and d["fit_mode"] == f"crop-{pos}"
        assert (d["width"], d["height"]) == (600, 338)
        with Image.open(d["path"]) as im:
            assert im.getpixel((300, 169))[channel] > 230
        details = client.get(f"/asset/{a}").json()
        assert details["render"]["current"] and details["render"]["crop_pct"] == 58
        assert details["matte"]["policy"] == "none" and details["plan"]["kind"] == "fill"
        assert details["presentation"] == {"mode": "crop", "position": pos}
        assert f"/thumb/{a}?v={d['id']}" in client.get("/gallery").text
    assert len(set(results + [old["id"]])) == 3
    assert Path(old["path"]).read_bytes() == old_pixels
    assert Path(asset["original_path"]).read_bytes() == original


def test_switching_back_to_matte_restores_whole_original_and_keeps_tv_choice(photo, library):
    client, a, _ = photo
    store.set_asset_matte(library, a, "shadowbox_sage", "odd")
    library.commit()
    save(client, a, "crop", 0)
    store.set_asset_matte(library, a, "modern_black", "wide")
    library.commit()  # a TV edit on the cropped version must not erase the earlier whole-photo choice
    save(client, a, "matte")
    d = client.get(f"/asset/{a}").json()
    assert (d["render"]["width"], d["render"]["height"]) == (600, 800)
    assert d["render"]["current"] and d["matte"]["policy"] == "shadowbox_sage"
    assert d["matte"]["text"] == "shadowbox_sage — chosen on the TV"
    assert "fit · shadowbox_sage" in client.get("/gallery").text


def test_save_is_idempotent_and_auto_returns_to_existing_derivative(photo, library):
    client, a, _ = photo
    original_id = store.get_any_derivative(library, a, "auto", 2)["id"]
    first = save(client, a, "crop", 237)["derivative_id"]
    assert save(client, a, "crop", 237)["derivative_id"] == first
    assert save(client, a, "matte")["derivative_id"] != first
    assert save(client, a, "crop", 237)["derivative_id"] == first
    assert save(client, a, "auto")["derivative_id"] == original_id
    assert library.execute("SELECT COUNT(*) FROM derivative WHERE asset_id=?", (a,)).fetchone()[0] == 3


def test_original_preview_is_not_the_cropped_thumbnail(photo):
    client, a, _ = photo
    save(client, a, "crop", 0)
    with Image.open(io.BytesIO(client.get(f"/thumb/{a}").content)) as thumb:
        assert thumb.width / thumb.height == pytest.approx(16 / 9, rel=.01)
    with Image.open(io.BytesIO(client.get(f"/asset/{a}/preview").content)) as preview:
        assert preview.size == (600, 800)
        assert preview.getpixel((300, 100))[0] > 230
        assert preview.getpixel((300, 700))[2] > 230


@pytest.mark.parametrize("data", [{"mode": "zoom"}, {"mode": "crop", "position": -1},
                                  {"mode": "crop", "position": 1001}, {"mode": "crop", "position": "2.5"}, {}])
def test_invalid_settings_do_not_change_anything(photo, library, data):
    client, a, _ = photo
    assert client.post(f"/asset/{a}/presentation", data=data).status_code == 422
    assert store.get_presentation(library, a) == ("auto", 500)
    assert library.execute("SELECT COUNT(*) FROM derivative").fetchone()[0] == 1


def test_failed_render_leaves_previous_settings_and_no_partial_file(photo, library, tmp_path, monkeypatch):
    client, a, _ = photo
    before = set((tmp_path / "d").iterdir())
    def broken_render(src, dest, **kw):
        Path(dest).write_bytes(b"partial jpeg")
        raise OSError("disk full")
    monkeypatch.setattr(images, "render_to_file", broken_render)
    r = client.post(f"/asset/{a}/presentation", data={"mode": "crop", "position": 0})
    assert r.status_code == 422 and "settings were not changed" in r.json()["detail"]
    assert store.get_presentation(library, a) == ("auto", 500)
    assert set((tmp_path / "d").iterdir()) == before
    assert library.execute("SELECT COUNT(*) FROM derivative").fetchone()[0] == 1


def test_reupload_deduplicates_without_resetting_manual_crop(photo, library):
    client, a, original = photo
    selected = save(client, a, "crop", 237)["derivative_id"]
    assert client.post("/upload", files={"files": ("again.jpg", original, "image/jpeg")}).status_code == 200
    assert library.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 1
    assert store.get_presentation(library, a) == ("crop", 237)
    assert store.get_any_derivative(library, a, "auto", 2)["id"] == selected


@pytest.mark.parametrize("suffix", ["presentation", "preview"])
def test_missing_deleted_and_broken_photos_cannot_be_edited(photo, library, suffix):
    client, a, _ = photo
    assert client.get(f"/asset/999/{suffix}").status_code == 404
    broken = add_asset(library, 99, status="broken", width=None, height=None)
    assert client.get(f"/asset/{broken}/{suffix}").status_code == 409
    store.mark_asset_deleted(library, a)
    assert client.get(f"/asset/{a}/{suffix}").status_code == 404
    assert client.post(f"/asset/{a}/presentation", data={"mode": "crop"}).status_code == 404
    assert client.post(f"/asset/{broken}/presentation", data={"mode": "crop"}).status_code == 409


def test_details_tell_the_truth_while_tv_still_holds_old_render(photo, library):
    client, a, _ = photo
    dev = add_device(library)
    old = store.get_any_derivative(library, a, "auto", 2)
    add_placement(library, dev, old["id"], "MY_F0001", matte="shadowbox_sage")
    save(client, a, "crop", 0)
    d = client.get(f"/asset/{a}").json()
    assert d["render"]["current"]
    assert d["on_tv"][0]["current"] is False and d["on_tv"][0]["matte"] == "shadowbox_sage"
    assert d["on_frame"] == ["MY_F0001"]


def test_editor_markup_accessible_dialog_cancel_and_both_entry_points(photo):
    client, _, _ = photo
    html = client.get("/").text
    assert html.count("Adjust crop / matte") >= 3
    assert '<dialog class="modal presentation"' in html and 'aria-labelledby="presentation-title"' in html
    assert '@cancel.prevent="closePresentation()"' in html
    assert 'type="range" min="0" max="1000"' in html
    assert "['Left', 'Center', 'Right']" in html and "['Top', 'Middle', 'Bottom']" in html
    assert "stroke=\"#facc15\"" in html
    assert ':view-box.camel="' in html  # SVG's case-sensitive viewBox must survive HTML parsing
    assert "change_matte" not in html


def test_manual_modes_work_without_changing_legacy_global_settings(photo, library, tmp_path):
    _, a, _ = photo
    client = make_client(tmp_path)  # legacy cover v1 remains the configured default
    save(client, a, "crop", 0)
    d = store.get_any_derivative(library, a, "cover", 1)
    assert (d["fit_mode"], d["pipeline_version"], d["width"], d["height"]) == ("crop-0", 1, 600, 338)
    assert client.get(f"/asset/{a}").json()["render"]["current"]
    save(client, a, "matte")
    d = store.get_any_derivative(library, a, "cover", 1)
    assert (d["fit_mode"], d["width"], d["height"]) == ("matte", 600, 800)


def test_manual_matte_does_not_label_a_replaced_none_preference_as_the_tv_choice(photo, library):
    client, a, _ = photo
    store.set_asset_matte(library, a, "none", "odd")
    library.commit()
    save(client, a, "matte")
    d = client.get(f"/asset/{a}").json()
    assert d["matte"]["policy"] == "flexible_black"
    assert "— default" in d["matte"]["text"] and "chosen on the TV" not in d["matte"]["text"]
