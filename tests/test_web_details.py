"""The details view (SPEC.md §12.7): render, plan and matte, read-only; what is on the TV."""
import pytest
from conftest import add_asset, add_derivative, add_device, add_placement

pytest.importorskip("fastapi")
from test_web_gallery import V2, library, make_client  # noqa: E402,F401

from frame_art_organizer import store  # noqa: E402


def details(client, asset_id):
    r = client.get(f"/asset/{asset_id}")
    assert r.status_code == 200
    return r.json()


def test_a_cropped_photo_explains_itself(tmp_path, library):
    a = add_asset(library, 1, "dslr.jpg", width=3000, height=2000)               # 3:2
    add_derivative(library, a, width=3000, height=1688, fit="auto", pv=2)
    d = details(make_client(tmp_path, V2), a)

    assert d["render"] == {"width": 3000, "height": 1688, "fit_mode": "auto", "pipeline_version": 2,
                           "pipeline": "auto v2", "current": True, "kind": "crop", "crop_pct": 16}
    assert d["plan"]["kind"] == "fill" and d["plan"]["size"] == [3000, 1688]
    assert d["plan"]["text"] == "fill — 16 % trimmed to 16:9, 3000 × 1688"
    assert [b["text"] for b in d["badges"]] == ["cropped 16 %"]
    assert d["matte"]["text"] == "none — default" and d["on_tv"] == []


def test_a_whole_photo_shows_the_plan_and_the_matte_it_will_wear(tmp_path, library):
    a = add_asset(library, 1, "family.jpg", width=2000, height=1500)
    add_derivative(library, a, width=2000, height=1500, fit="auto", pv=2)
    d = details(make_client(tmp_path, V2), a)
    assert d["plan"]["text"] == "fit — shown whole, 2000 × 1500, in a flexible_black matte"
    assert d["matte"] == {"policy": "flexible_black", "chosen_on_tv": None, "chosen_shape": None,
                          "text": "flexible_black — default"}


def test_a_matte_chosen_on_the_tv_is_shown_as_such(tmp_path, library):
    a = add_asset(library, 1, "family.jpg", width=2000, height=1500)
    add_derivative(library, a, width=2000, height=1500, fit="auto", pv=2)
    store.set_asset_matte(library, a, "shadowbox_sage", "odd")
    library.commit()
    d = details(make_client(tmp_path, V2), a)
    assert d["matte"]["text"] == "shadowbox_sage — chosen on the TV" and d["matte"]["policy"] == "shadowbox_sage"


def test_a_tv_choice_that_is_not_carried_over_says_why(tmp_path, library):
    """Chosen on the cropped 16:9 version of a portrait; the portrait is now whole."""
    a = add_asset(library, 1, "portrait.jpg", width=1600, height=2400)
    add_derivative(library, a, width=1600, height=2400, fit="auto", pv=2)
    store.set_asset_matte(library, a, "modernwide_black", "wide")
    library.commit()
    d = details(make_client(tmp_path, V2), a)
    assert d["matte"]["policy"] == "flexible_black"
    assert d["matte"]["text"] == ("flexible_black — default (your TV choice modernwide_black was made for a "
                                  "16:9 version of this photo, so it isn't applied to this one)")


def test_what_is_on_the_tv_is_listed_with_the_matte_it_really_has(tmp_path, library):
    dev = add_device(library)
    a = add_asset(library, 1, "family.jpg", width=2000, height=1500)
    d1 = add_derivative(library, a, width=3840, height=2160, fit="cover", pv=1)
    add_derivative(library, a, width=2000, height=1500, fit="auto", pv=2)
    add_placement(library, dev, d1, "MY_F0042", matte="modern_black")             # still the OLD render on the TV
    d = details(make_client(tmp_path, V2), a)
    assert d["on_tv"] == [{"content_id": "MY_F0042", "matte": "modern_black", "width": 3840,
                           "height": 2160, "pipeline": "cover v1", "current": False}]
    assert d["on_frame"] == ["MY_F0042"]                                         # the original key is still there


def test_a_photo_still_on_the_old_pipeline_is_flagged(tmp_path, library):
    a = add_asset(library, 1, "family.jpg", width=2000, height=1500)
    add_derivative(library, a, width=3840, height=2160, fit="cover", pv=1)        # config is v2, nothing re-rendered yet
    d = details(make_client(tmp_path, V2), a)
    assert d["render"]["current"] is False and d["render"]["pipeline"] == "cover v1"
    assert d["plan"]["kind"] == "fit"                                             # what v2 WILL do is still shown


def test_under_the_v1_config_there_is_no_plan_to_show(tmp_path, library):
    a = add_asset(library, 1, "family.jpg", width=2000, height=1500)
    add_derivative(library, a, width=3840, height=2160, fit="cover", pv=1)
    d = details(make_client(tmp_path), a)
    assert d["plan"] is None and d["render"]["current"] is True and d["render"]["crop_pct"] == 25


def test_an_unrendered_photo_has_no_render_and_still_answers(tmp_path, library):
    a = add_asset(library, 1, "fresh.jpg", width=800, height=600)
    d = details(make_client(tmp_path, V2), a)
    assert d["render"] is None and d["has_derivative"] is False and d["derivative_id"] is None
    assert [b["text"] for b in d["badges"]] == ["not rendered yet", "low-res 800×600"]
    assert d["plan"]["kind"] == "fit" and d["low_res"] is True


def test_the_file_facts_are_still_there_and_unknown_photos_are_404(tmp_path, library):
    a = add_asset(library, 1, "family.jpg", width=2000, height=1500)
    d = details(make_client(tmp_path, V2), a)
    for key in ("id", "original_name", "width", "height", "bytes", "sha256", "collections", "on_frame"):
        assert key in d
    assert make_client(tmp_path, V2).get("/asset/9999").status_code == 404


def test_the_modal_has_no_matte_editor(tmp_path, library):
    """The TV is the matte editor (SPEC §12.1): the page offers no way to set one."""
    html = make_client(tmp_path, V2).get("/").text
    assert "chosen on the TV itself" in html
    assert "/matte" not in html and "change_matte" not in html
