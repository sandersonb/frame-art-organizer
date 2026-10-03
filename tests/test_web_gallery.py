"""The gallery over real HTTP: badges on the cards, following the configured pipeline."""
import pytest
from conftest import add_asset, add_derivative

pytest.importorskip("fastapi")
try:                                    # Starlette >= 1.x prefers httpx2 for its test client
    import httpx2  # noqa: F401
except ModuleNotFoundError:
    pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from frame_art_organizer import db, store, web  # noqa: E402

V2 = '[image]\ndefault_fit = "auto"\npipeline_version = 2\n'


def make_client(tmp_path, image_block=""):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[frame]\nhost = "192.0.2.10"\n[paths]\ninbox = "in"\noriginals = "o"\n'
                   'derivatives = "d"\ndatabase = "state.db"\n' + image_block, encoding="utf-8")
    return TestClient(web.create_app(str(cfg), run_daemon=False))


@pytest.fixture
def library(tmp_path):
    """Opened after create_app has migrated the DB; the tests seed it, then make a client."""
    make_client(tmp_path)
    c = db.open_db(tmp_path / "state.db")
    yield c
    c.close()


def gallery(client):
    r = client.get("/gallery")
    assert r.status_code == 200
    return r.text


def test_the_v1_library_is_described_as_it_really_is(tmp_path, library):
    a = add_asset(library, 1, "family.jpg", width=2000, height=1500)         # 4:3, cover-rendered today
    add_derivative(library, a, width=3840, height=2160, fit="cover", pv=1)
    html = gallery(make_client(tmp_path))
    assert "cropped 25 %" in html and "fit ·" not in html


def test_after_the_flip_the_same_photo_shows_as_whole_with_its_matte(tmp_path, library):
    a = add_asset(library, 1, "family.jpg", width=2000, height=1500)
    add_derivative(library, a, width=3840, height=2160, fit="cover", pv=1)
    add_derivative(library, a, width=2000, height=1500, fit="auto", pv=2)
    html = gallery(make_client(tmp_path, V2))
    assert "fit · flexible_black" in html and "cropped 25 %" not in html


def test_a_rollback_shows_what_the_tv_shows_not_the_newest_render(tmp_path, library):
    """Both derivatives exist; the config decides which one the gallery describes."""
    a = add_asset(library, 1, "family.jpg", width=2000, height=1500)
    add_derivative(library, a, width=3840, height=2160, fit="cover", pv=1)
    add_derivative(library, a, width=2000, height=1500, fit="auto", pv=2)
    assert "cropped 25 %" in gallery(make_client(tmp_path))                   # config: cover v1
    assert "fit · flexible_black" in gallery(make_client(tmp_path, V2))       # config: auto v2


def test_a_matte_chosen_on_the_tv_shows_in_the_badge(tmp_path, library):
    a = add_asset(library, 1, "family.jpg", width=2000, height=1500)
    add_derivative(library, a, width=2000, height=1500, fit="auto", pv=2)
    store.set_asset_matte(library, a, "shadowbox_sage", "odd")
    library.commit()                                                          # set_asset_matte leaves that to the caller
    assert "fit · shadowbox_sage" in gallery(make_client(tmp_path, V2))


def test_a_sixteen_by_nine_photo_gets_no_render_badge(tmp_path, library):
    a = add_asset(library, 1, "wide.jpg", width=3000, height=1688)            # 16:9 +/- rounding
    add_derivative(library, a, width=3000, height=1688, fit="auto", pv=2)
    html = gallery(make_client(tmp_path, V2))
    assert 'class="tag k-' not in html


def test_unrendered_and_low_res_photos_say_so(tmp_path, library):
    add_asset(library, 1, "fresh.jpg", width=800, height=600)                 # no derivative at all
    html = gallery(make_client(tmp_path, V2))
    assert "not rendered yet" in html and "low-res 800×600" in html


def test_the_index_page_renders_the_same_badges(tmp_path, library):
    a = add_asset(library, 1, "family.jpg", width=2000, height=1500)
    add_derivative(library, a, width=2000, height=1500, fit="auto", pv=2)
    r = make_client(tmp_path, V2).get("/")
    assert r.status_code == 200 and "fit · flexible_black" in r.text


def test_deleted_photos_are_not_in_the_gallery(tmp_path, library):
    a = add_asset(library, 1, "gone.jpg", width=2000, height=1500)
    add_derivative(library, a, width=2000, height=1500, fit="auto", pv=2)
    store.mark_asset_deleted(library, a)
    assert "gone.jpg" not in gallery(make_client(tmp_path, V2))
