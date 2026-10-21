import pytest
from conftest import add_device, resident

pytest.importorskip("fastapi")
from test_web_gallery import V2, library, make_client  # noqa: E402,F401
from frame_art_organizer import web  # noqa: E402
from frame_art_organizer.frame_client import FrameTimeout  # noqa: E402


@pytest.mark.parametrize("outcome", [False, RuntimeError("rejected"), FrameTimeout("unreachable")])
def test_web_delete_does_not_forget_unconfirmed_tv_deletion(tmp_path, library, monkeypatch, outcome):
    dev = add_device(library)
    asset, _, placement = resident(library, dev, 1)

    class Client:
        def __init__(self, cfg): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def delete(self, cid):
            if isinstance(outcome, Exception): raise outcome
            return outcome

    monkeypatch.setattr(web, "FrameClient", Client)
    response = make_client(tmp_path, V2).post(f"/asset/{asset}/delete")
    assert response.status_code == 200
    assert library.execute("SELECT status FROM asset WHERE id=?", (asset,)).fetchone()[0] == "deleted"
    assert library.execute("SELECT state FROM placement WHERE id=?", (placement,)).fetchone()[0] == "present"
    assert library.execute("SELECT count(*) FROM rotation_event").fetchone()[0] == 0
