"""Harvest wired into scheduler.refresh and uploader.reconcile — "no harvest, no evict".

Uses a fake TV client that records every call, so we can assert ORDER (the listing is read
before anything is deleted) and what happens when the listing fails.
"""
import pytest
from conftest import add_asset, add_derivative, add_device, add_placement, item, resident

from frame_art_organizer import scheduler, store, uploader
from frame_art_organizer.frame_client import FrameAsleep, FrameTimeout
from frame_art_organizer.mattes import MatteConfig

PERIOD = {"name": "all-day", "collections": [], "interval": 15, "shuffle": True,
          "set_size": 1, "no_repeat_days": 30}


class FakeClient:
    """Stands in for FrameClient. `calls` is the ordered log of what was asked of the TV."""

    def __init__(self, listing=(), list_exc=None):
        self.listing, self.list_exc, self.calls, self._n = list(listing), list_exc, [], 100

    def list_my_photos(self):
        self.calls.append("list")
        if self.list_exc:
            raise self.list_exc
        return list(self.listing)

    def upload_jpeg(self, data, matte="none", date=None):
        self.n = self._n = self._n + 1
        self.calls.append(("upload", matte))
        return f"MY_F{self._n:04d}"

    def delete(self, content_id):
        self.calls.append(("delete", content_id))
        return True

    def set_slideshow(self, duration, shuffle=True, category_id="MY-C0002"):
        self.calls.append(("slideshow", duration))


def _refresh(client, conn, dev, **kw):
    return scheduler.refresh(client, conn, device_id=dev, period=PERIOD, fit_mode="cover",
                             pipeline_version=1, matte_cfg=MatteConfig(), **kw)


@pytest.fixture
def rotation(conn, tmp_path):
    """Asset 1 is resident (placement MY_F0001); asset 2 is pinned so the refresh wants it
    instead — i.e. asset 1 is scheduled for eviction and asset 2 for upload."""
    dev = add_device(conn)
    a1, d1, p1 = resident(conn, dev, 1, matte="none")
    a2 = add_asset(conn, 2)
    f = tmp_path / "two.jpg"
    f.write_bytes(b"jpeg")
    add_derivative(conn, a2, path=str(f))
    store.set_policy(conn, a2, pinned=True)
    return dev, a1, p1, a2


def _state(conn, placement_id):
    return conn.execute("SELECT state FROM placement WHERE id = ?", (placement_id,)).fetchone()[0]


# --- scheduler.refresh ---------------------------------------------------------------------
def test_refresh_reads_the_tv_before_it_evicts_and_keeps_the_users_edit(conn, rotation):
    dev, a1, p1, a2 = rotation
    client = FakeClient([item("MY_F0001", "modern_seafoam")])        # the user edited photo 1

    res = _refresh(client, conn, dev)

    assert client.calls.index("list") < client.calls.index(("delete", "MY_F0001"))   # harvest first
    pol = conn.execute("SELECT matte, matte_shape FROM asset_policy WHERE asset_id = ?", (a1,)).fetchone()
    assert (pol["matte"], pol["matte_shape"]) == ("modern_seafoam", "wide")           # edit survived
    assert _state(conn, p1) == "deleted_on_device"                                    # ...then evicted
    assert (res["harvested"], res["added"], res["removed"], res["removals_skipped"]) == (1, 1, 1, False)


def test_refresh_aborts_without_evicting_when_the_tv_cannot_answer(conn, rotation):
    dev, a1, p1, a2 = rotation
    client = FakeClient(list_exc=FrameTimeout("art call exceeded 20s"))

    with pytest.raises(FrameAsleep):                       # FrameTimeout is a FrameAsleep: "not now"
        _refresh(client, conn, dev)

    assert client.calls == ["list"]                        # nothing uploaded, nothing deleted
    assert _state(conn, p1) == "present"                   # still ours and resident


def test_refresh_skips_evictions_but_still_adds_if_harvest_itself_breaks(conn, rotation, monkeypatch):
    dev, a1, p1, a2 = rotation

    def boom(*a, **k):
        raise RuntimeError("bug in harvest")
    monkeypatch.setattr(store, "harvest_mattes", boom)
    client = FakeClient([item("MY_F0001")])

    res = _refresh(client, conn, dev)

    assert ("upload", "none") in client.calls              # rotation keeps flowing
    assert not any(c[0] == "delete" for c in client.calls if isinstance(c, tuple))
    assert _state(conn, p1) == "present"                   # nothing evicted without a harvest
    assert (res["added"], res["removed"], res["removals_skipped"]) == (1, 0, True)


def test_refresh_with_nothing_eligible_does_not_even_list(conn):
    dev = add_device(conn)                                 # empty library → "leave the Frame as-is"
    client = FakeClient()
    res = _refresh(client, conn, dev)
    assert res["skipped"] == "empty-set" and client.calls == []


# --- uploader.reconcile --------------------------------------------------------------------
def test_reconcile_harvests_from_the_listing_it_already_fetched(conn):
    dev = add_device(conn)
    a, _, p = resident(conn, dev, 1, matte="none")
    client = FakeClient([item("MY_F0001", "shadowbox_sage"), item("MY_F0500", "none")])   # 0500 = orphan

    res = uploader.reconcile(client, conn, dev)

    assert client.calls == ["list"]                        # one listing serves both jobs
    assert (res["harvested"], res["vanished"], res["orphans"], res["harvest_error"]) == (1, 0, ["MY_F0500"], None)
    assert conn.execute("SELECT matte FROM asset_policy WHERE asset_id = ?", (a,)).fetchone()[0] == "shadowbox_sage"


def test_reconcile_still_marks_vanished_placements(conn):
    dev = add_device(conn)
    _, _, p = resident(conn, dev, 1)
    res = uploader.reconcile(FakeClient([]), conn, dev)    # photo 1 is no longer on the TV
    assert (res["vanished"], res["harvested"]) == (1, 0) and _state(conn, p) == "deleted_on_device"


def test_a_harvest_failure_never_breaks_reconcile(conn, monkeypatch):
    dev = add_device(conn)
    _, _, p = resident(conn, dev, 1)
    monkeypatch.setattr(store, "harvest_mattes", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))

    res = uploader.reconcile(FakeClient([]), conn, dev)    # reconcile's own work still done

    assert res["harvest_error"] == "ValueError" and res["harvested"] == 0
    assert res["vanished"] == 1 and _state(conn, p) == "deleted_on_device"
