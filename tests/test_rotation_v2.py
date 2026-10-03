"""Rotation under the v2 pipeline: derivative-based diff, matte_for at upload, canary limit."""
import pytest
from conftest import add_asset, add_derivative, add_device, add_placement, item, resident
from test_wiring import FakeClient, PERIOD

from frame_art_organizer import scheduler, store, uploader
from frame_art_organizer.mattes import MatteConfig

CFG = MatteConfig()   # default "none" for 16:9, "flexible_black" for everything else


def _plan(conn, dev, fit="cover", pv=1, **period):
    return scheduler.plan(conn, device_id=dev, period={**PERIOD, **period}, fit_mode=fit, pipeline_version=pv)


def _refresh(client, conn, dev, fit="cover", pv=1, period=None, **kw):
    return scheduler.refresh(client, conn, device_id=dev, period=period or PERIOD, fit_mode=fit,
                             pipeline_version=pv, matte_cfg=CFG, **kw)


def _present(conn, dev):
    return {(r["asset_id"], r["derivative_id"]) for r in store.present_placements(conn, dev)}


def _file(tmp_path, name):
    f = tmp_path / name
    f.write_bytes(b"jpeg")
    return str(f)


# --- plan(): diff by derivative -------------------------------------------------------------
def test_steady_state_diff_is_unchanged(conn, tmp_path):
    dev = add_device(conn)
    a1, d1, p1 = resident(conn, dev, 1)
    a2 = add_asset(conn, 2); d2 = add_derivative(conn, a2, path=_file(tmp_path, "2.jpg"))
    store.set_policy(conn, a2, pinned=True)                         # the refresh wants asset 2, not 1
    desired, to_add, to_remove, present = _plan(conn, dev)
    assert [r["asset_id"] for r in to_add] == [a2] and [p["asset_id"] for p in to_remove] == [a1]


def test_a_version_bump_swaps_the_stale_derivative_for_the_current_one(conn, tmp_path):
    dev = add_device(conn)
    a, d_v1, p_v1 = resident(conn, dev, 1)                          # resident under v1 (cover, pv 1)
    d_v2 = add_derivative(conn, a, width=1600, height=1200, fit="auto", pv=2, path=_file(tmp_path, "v2.jpg"))
    desired, to_add, to_remove, _ = _plan(conn, dev, fit="auto", pv=2)
    assert [r["derivative_id"] for r in to_add] == [d_v2]           # the photo is still wanted...
    assert [p["id"] for p in to_remove] == [p_v1]                   # ...but its old version is stale


def test_a_resident_photo_without_a_rendered_replacement_is_kept(conn, tmp_path):
    dev = add_device(conn)
    a, _, p_a = resident(conn, dev, 1)                               # has a v2 derivative
    b, _, p_b = resident(conn, dev, 2)                               # v2 NOT rendered yet
    add_derivative(conn, a, fit="auto", pv=2, path=_file(tmp_path, "a2.jpg"))
    desired, to_add, to_remove, _ = _plan(conn, dev, fit="auto", pv=2)
    assert [p["id"] for p in to_remove] == [p_a]                    # only the one with a replacement
    assert p_b not in [p["id"] for p in to_remove]                  # the set must not shrink mid-migration


def test_a_deleted_asset_is_still_evicted(conn):
    dev = add_device(conn)
    a, _, p = resident(conn, dev, 1)
    other, _, _ = resident(conn, dev, 2)
    store.mark_asset_deleted(conn, a)
    _, _, to_remove, _ = _plan(conn, dev, set_size=5)
    assert p in [x["id"] for x in to_remove]


# --- refresh(): matte_for at upload ---------------------------------------------------------
@pytest.fixture
def wants_one(conn, tmp_path):
    """Asset 1 resident (to be evicted); asset 2 pinned, so the refresh uploads it."""
    dev = add_device(conn)
    resident(conn, dev, 1)
    a2 = add_asset(conn, 2)
    store.set_policy(conn, a2, pinned=True)
    return dev, a2, tmp_path


def _upload_of(client):
    return [c for c in client.calls if isinstance(c, tuple) and c[0] == "upload"]


def test_no_preference_uploads_with_the_default_matte_exactly_as_before(conn, wants_one):
    dev, a2, tmp = wants_one
    add_derivative(conn, a2, path=_file(tmp, "2.jpg"))                # a v1 derivative: 3840x2160 = wide
    client = FakeClient([item("MY_F0001")])
    res = _refresh(client, conn, dev)
    assert _upload_of(client) == [("upload", "none")] and res["mattes_used"] == {"none": 1}


def test_a_harvested_preference_is_applied_on_upload(conn, wants_one):
    dev, a2, tmp = wants_one
    d2 = add_derivative(conn, a2, path=_file(tmp, "2.jpg"))
    store.set_asset_matte(conn, a2, "shadowbox_sage", "wide"); conn.commit()
    client = FakeClient([item("MY_F0001")])

    res = _refresh(client, conn, dev)

    assert _upload_of(client) == [("upload", "shadowbox_sage")] and res["mattes_used"] == {"shadowbox_sage": 1}
    assert conn.execute("SELECT matte FROM placement WHERE derivative_id = ?", (d2,)).fetchone()[0] == "shadowbox_sage"


def test_an_odd_shape_photo_gets_the_fit_matte_and_never_a_crossed_preference(conn, wants_one):
    dev, a2, tmp = wants_one
    add_derivative(conn, a2, width=1600, height=1200, path=_file(tmp, "2.jpg"))      # a native 4:3 (v2-style)
    store.set_asset_matte(conn, a2, "modern_seafoam", "wide"); conn.commit()          # chosen on a 16:9 item
    client = FakeClient([item("MY_F0001")])
    _refresh(client, conn, dev)
    assert _upload_of(client) == [("upload", "flexible_black")]       # the 2026-10-01 crash combination: blocked


def test_sync_uses_matte_for_too(conn, tmp_path):
    dev = add_device(conn)
    a = add_asset(conn, 1)
    add_derivative(conn, a, path=_file(tmp_path, "1.jpg"))
    store.set_asset_matte(conn, a, "modernthin_black", "wide"); conn.commit()
    client = FakeClient()
    res = uploader.sync(client, conn, device_id=dev, fit_mode="cover", pipeline_version=1, matte_cfg=CFG)
    assert res == {"uploaded": 1, "errors": 0} and _upload_of(client) == [("upload", "modernthin_black")]


# --- refresh(limit=N): the canary ------------------------------------------------------------
@pytest.fixture
def migration(conn, tmp_path):
    """Three photos resident under v1, each with a rendered v2 derivative; all pinned & wanted."""
    dev = add_device(conn)
    assets = []
    for n in (1, 2, 3):
        a, d1, p1 = resident(conn, dev, n)
        add_derivative(conn, a, width=1600, height=1200, fit="auto", pv=2, path=_file(tmp_path, f"{n}.jpg"))
        store.set_policy(conn, a, pinned=True)
        assets.append(a)
    return dev, assets


V2 = dict(fit="auto", pv=2, period={**PERIOD, "set_size": 3})


def test_without_a_limit_every_stale_photo_is_swapped(conn, migration):
    dev, assets = migration
    client = FakeClient([item(f"MY_F{n:04d}") for n in (1, 2, 3)])
    res = _refresh(client, conn, dev, **V2)
    assert (res["added"], res["removed"], res["pending_adds"]) == (3, 3, 0)
    assert len(store.present_placements(conn, dev)) == 3
    assert {r["matte"] for r in store.present_placements(conn, dev)} == {"flexible_black"}   # odd shape -> fit matte


def test_a_canary_swaps_one_photo_and_never_shrinks_the_set(conn, migration):
    dev, assets = migration
    client = FakeClient([item(f"MY_F{n:04d}") for n in (1, 2, 3)])

    res = _refresh(client, conn, dev, limit=1, **V2)

    assert (res["added"], res["removed"], res["pending_adds"]) == (1, 1, 2)
    assert len(store.present_placements(conn, dev)) == 3             # still three resident
    new = [r for r in store.present_placements(conn, dev) if r["width"] == 1600]
    assert len(new) == 1                                              # exactly one now on the v2 derivative
    deleted = conn.execute("SELECT d.asset_id FROM placement p JOIN derivative d ON d.id = p.derivative_id "
                           "WHERE p.state = 'deleted_on_device'").fetchall()
    assert [r[0] for r in deleted] == [new[0]["asset_id"]]            # the swap pair: new v2 replaced ITS OWN v1


def test_a_second_canary_run_continues_where_the_first_stopped(conn, migration):
    dev, assets = migration
    client = FakeClient([item(f"MY_F{n:04d}") for n in (1, 2, 3)])
    _refresh(client, conn, dev, limit=1, **V2)
    res = _refresh(client, conn, dev, limit=1, **V2)
    assert (res["added"], res["removed"], res["pending_adds"]) == (1, 1, 1)
    assert sum(1 for r in store.present_placements(conn, dev) if r["width"] == 1600) == 2


def test_limit_zero_changes_nothing(conn, migration):
    dev, assets = migration
    client = FakeClient([item(f"MY_F{n:04d}") for n in (1, 2, 3)])
    res = _refresh(client, conn, dev, limit=0, **V2)
    assert (res["added"], res["removed"], res["pending_adds"]) == (0, 0, 3)
    assert not _upload_of(client) and not [c for c in client.calls if isinstance(c, tuple) and c[0] == "delete"]


def test_failed_uploads_mean_no_evictions_under_a_limit(conn, migration):
    dev, assets = migration

    class BrokenUploads(FakeClient):
        def upload_jpeg(self, *a, **k):
            raise RuntimeError("disk full on the TV")

    res = _refresh(BrokenUploads([item(f"MY_F{n:04d}") for n in (1, 2, 3)]), conn, dev, limit=2, **V2)
    assert (res["added"], res["errors"], res["removed"]) == (0, 2, 0)       # never evict what wasn't replaced
    assert sum(1 for r in store.present_placements(conn, dev) if r["width"] == 3840) == 3
