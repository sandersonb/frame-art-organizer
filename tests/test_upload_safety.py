"""Ambiguous uploads stop rotation and require explicit, local operator resolution."""
from types import SimpleNamespace

import pytest
from conftest import add_asset, add_derivative, add_device, item
from test_wiring import FakeClient, _refresh, rotation  # noqa: F401

from frame_art_organizer import cli, db, scheduler, store, uploader
from frame_art_organizer.frame_client import FrameUploadUncertain
from frame_art_organizer.mattes import MatteConfig


def test_lost_upload_reply_quarantines_placement_and_never_evicts(conn, rotation):
    dev, _, old, _ = rotation

    class Ambiguous(FakeClient):
        def upload_jpeg(self, *args, **kw):
            self.calls.append("upload")
            raise FrameUploadUncertain("lost ack")

    client = Ambiguous([item("MY_F0001")])
    with pytest.raises(FrameUploadUncertain): _refresh(client, conn, dev)
    row = store.unresolved_uploads(conn, dev)[0]
    assert row["state"] == "pending" and row["upload_outcome"] == "uncertain"
    assert conn.execute("SELECT state FROM placement WHERE id=?", (old,)).fetchone()[0] == "present"
    calls = list(client.calls)
    with pytest.raises(store.UploadResolutionRequired): _refresh(client, conn, dev)
    assert client.calls == calls  # No harvest, upload, select, delete or slideshow call.


def test_sync_also_blocks_automatic_replay(conn, rotation):
    dev, _, _, _ = rotation

    class Ambiguous(FakeClient):
        def upload_jpeg(self, *a, **kw): raise FrameUploadUncertain("lost ack")

    client = Ambiguous()
    with pytest.raises(FrameUploadUncertain):
        uploader.sync(client, conn, device_id=dev, fit_mode="cover", pipeline_version=1,
                      matte_cfg=MatteConfig())
    with pytest.raises(store.UploadResolutionRequired):
        uploader.sync(client, conn, device_id=dev, fit_mode="cover", pipeline_version=1,
                      matte_cfg=MatteConfig())


def test_atomic_claim_refuses_another_upload_while_one_is_unfinished(conn):
    dev = add_device(conn)
    d = add_derivative(conn, add_asset(conn))
    pid = store.create_pending_placement(conn, dev, d, "none")
    with pytest.raises(store.UploadResolutionRequired): store.create_pending_placement(conn, dev, d, "none")
    assert conn.execute("SELECT count(*) FROM placement").fetchone()[0] == 1
    store.set_placement_error(conn, pid)
    assert store.create_pending_placement(conn, dev, d, "none") != pid


def test_quarantined_legacy_error_is_not_purgeable(conn):
    dev = add_device(conn)
    asset = add_asset(conn)
    old = add_derivative(conn, asset)
    add_derivative(conn, asset, fit="auto", pv=2)
    pid = store.create_pending_placement(conn, dev, old, "none")
    store.set_placement_error(conn, pid)
    store.set_placement_uncertain(conn, pid)  # Legacy errors retain their state during migration.
    assert store.superseded_derivatives(conn, "auto", 2) == []
    assert store.stale_resident_count(conn, "auto", 2) == 1
    with pytest.raises(ValueError): store.purge_derivative(conn, old)


def test_migration_quarantines_legacy_errors_without_changing_history(tmp_path):
    c = db.connect(tmp_path / "legacy.db")
    c.executescript(db._V1 + db._V2 + db._V3)
    c.execute("PRAGMA user_version=3")
    dev = add_device(c)
    d = add_derivative(c, add_asset(c))
    for state, cid in [("error", None), ("pending", None), ("present", "MY_F0001"),
                       ("deleted_on_device", "MY_F0002")]:
        c.execute("INSERT INTO placement(device_id,derivative_id,state,content_id) VALUES (?,?,?,?)",
                  (dev, d, state, cid))
    invalid = add_derivative(c, add_asset(c, 2))
    c.execute("INSERT INTO placement(device_id,derivative_id,state) VALUES (?,?,'present')", (dev, invalid))
    c.commit()
    before = c.execute("SELECT id,state,content_id FROM placement").fetchall()
    assert db.migrate(c) == 4
    assert c.execute("SELECT id,state,content_id FROM placement").fetchall() == before
    assert len(store.unresolved_uploads(c, dev)) == 3
    with pytest.raises(store.UploadResolutionRequired): store.require_resolved_uploads(c, dev)
    c.close()


@pytest.fixture
def unresolved_cli(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    c = db.open_db(path)
    dev = add_device(c)
    asset = add_asset(c)
    derivative = add_derivative(c, asset)
    pid = store.create_pending_placement(c, dev, derivative, "none")
    store.set_placement_uncertain(c, pid)
    c.close()
    monkeypatch.setattr(cli, "_library", lambda args: (db.open_db(path), {}, {}))
    return path, pid


def test_review_without_id_is_local_only(unresolved_cli, monkeypatch, capsys):
    path, pid = unresolved_cli
    monkeypatch.setattr(cli, "_client", lambda a: pytest.fail("local review must not contact TV"))
    assert cli.main(["upload-resolve"]) == 0
    assert "photo1.jpg" in capsys.readouterr().out
    c = db.open_db(path)
    assert store.unresolved_uploads(c)[0]["id"] == pid
    c.close()


def test_explicit_not_uploaded_clears_block_without_contacting_tv(unresolved_cli, monkeypatch):
    path, pid = unresolved_cli
    monkeypatch.setattr(cli, "_client", lambda a: pytest.fail("must not contact TV"))
    assert cli.main(["upload-resolve", str(pid), "--not-uploaded"]) == 0
    c = db.open_db(path)
    assert store.unresolved_uploads(c) == []
    assert c.execute("SELECT state,upload_outcome FROM placement WHERE id=?", (pid,)).fetchone()[:] == ("error", "not_uploaded")
    c.close()


def test_operator_can_attach_existing_matching_photo_without_tv_mutation(unresolved_cli, monkeypatch):
    path, pid = unresolved_cli

    class ReadOnly:
        cfg = SimpleNamespace(host="192.0.2.10")
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def device_info(self): return {"device": {"duid": "uuid:test-device"}}
        def list_my_photos(self): return [item("MY_F0999")]

    monkeypatch.setattr(cli, "_client", lambda a: (ReadOnly(), {}))
    assert cli.main(["upload-resolve", str(pid), "--content-id", "MY_F0999"]) == 0
    c = db.open_db(path)
    assert not store.unresolved_uploads(c)
    assert c.execute("SELECT content_id,state FROM placement WHERE id=?", (pid,)).fetchone()[:] == ("MY_F0999", "present")
    assert c.execute("SELECT count(*) FROM rotation_event").fetchone()[0] == 1
    c.close()


def test_failed_delete_is_retained_for_retry(conn, rotation):
    dev, _, old, _ = rotation

    class Refusing(FakeClient):
        def delete(self, cid): return False

    result = _refresh(Refusing([item("MY_F0001")]), conn, dev)
    assert result["removed"] == 0 and result["errors"] == 1
    assert conn.execute("SELECT state FROM placement WHERE id=?", (old,)).fetchone()[0] == "present"


def test_delete_exception_is_retained_for_retry(conn, rotation):
    dev, _, old, _ = rotation

    class Refusing(FakeClient):
        def delete(self, cid): raise RuntimeError("failed")

    result = _refresh(Refusing([item("MY_F0001")]), conn, dev)
    assert result["removed"] == 0 and result["errors"] == 1
    assert conn.execute("SELECT state FROM placement WHERE id=?", (old,)).fetchone()[0] == "present"


def test_cosmetic_transport_failure_aborts_before_deletion(conn, rotation):
    from frame_art_organizer.frame_client import FrameTimeout
    dev, _, old, _ = rotation
    client = FakeClient([item("MY_F0001")], shown="MY_F0001", select_exc=FrameTimeout("unavailable"))
    with pytest.raises(FrameTimeout): _refresh(client, conn, dev)
    assert not any(isinstance(call, tuple) and call[0] == "delete" for call in client.calls)
    assert conn.execute("SELECT state FROM placement WHERE id=?", (old,)).fetchone()[0] == "present"
