"""Before deleting the photo that is on the wall, show a surviving one (API notes §8) — but only
when the TV is in Art Mode, and never at the cost of the refresh."""
import pytest
from conftest import add_asset, add_derivative, add_device, item, resident
from test_wiring import FakeClient, PERIOD

from frame_art_organizer import scheduler, store
from frame_art_organizer.mattes import MatteConfig


@pytest.fixture
def swap(conn, tmp_path):
    """Photo 1 is resident and about to be evicted; photo 2 (pinned) gets uploaded instead."""
    dev = add_device(conn)
    resident(conn, dev, 1)                                           # MY_F0001, to be evicted
    resident(conn, dev, 3)                                           # MY_F0003, also evicted
    a2 = add_asset(conn, 2)
    f = tmp_path / "2.jpg"; f.write_bytes(b"jpeg")
    add_derivative(conn, a2, path=str(f))
    store.set_policy(conn, a2, pinned=True)
    return dev


def _refresh(client, conn, dev):
    return scheduler.refresh(client, conn, device_id=dev, period=PERIOD, fit_mode="cover",
                             pipeline_version=1, matte_cfg=MatteConfig())


def _calls(client, kind):
    return [c for c in client.calls if isinstance(c, tuple) and c[0] == kind]


def test_the_displayed_photo_is_switched_away_before_it_is_deleted(conn, swap):
    client = FakeClient([item("MY_F0001"), item("MY_F0003")], shown="MY_F0001")
    _refresh(client, conn, swap)

    sel = client.calls.index(_calls(client, "select")[0])
    first_delete = client.calls.index(_calls(client, "delete")[0])
    assert sel < first_delete                                        # switched BEFORE any delete
    assert _calls(client, "select")[0][1] == "MY_F0101"              # the photo just uploaded (newest survivor)
    assert _calls(client, "select")[0][2] is False                   # show=False: never yank the panel on


def test_nothing_is_selected_when_the_displayed_photo_is_not_being_removed(conn, swap):
    client = FakeClient([item("MY_F0001"), item("MY_F0003")], shown="MY_F0777")      # someone else's photo
    _refresh(client, conn, swap)
    assert _calls(client, "select") == [] and len(_calls(client, "delete")) == 2


def test_nothing_is_selected_when_the_tv_is_not_in_art_mode(conn, swap):
    client = FakeClient([item("MY_F0001"), item("MY_F0003")], shown="MY_F0001", mode="off")
    _refresh(client, conn, swap)
    assert _calls(client, "select") == []                           # nothing visible; don't pull anyone out of TV
    assert len(_calls(client, "delete")) == 2                        # ...and the refresh still completes


def test_a_failing_switch_never_blocks_the_refresh(conn, swap):
    client = FakeClient([item("MY_F0001"), item("MY_F0003")], shown="MY_F0001",
                        select_exc=RuntimeError("TV said no"))
    res = _refresh(client, conn, swap)
    assert (res["added"], res["removed"]) == (1, 2)                  # the deletes happened regardless


def test_no_survivor_means_no_switch(conn):
    dev = add_device(conn)
    resident(conn, dev, 1)
    a = add_asset(conn, 2)                                           # un-renderable replacement: no derivative
    client = FakeClient([item("MY_F0001")], shown="MY_F0001")
    scheduler._switch_display_away(client, {"MY_F0001"}, [])         # nothing to switch to
    assert _calls(client, "select") == [] and client.calls == []     # and it didn't even ask the TV
