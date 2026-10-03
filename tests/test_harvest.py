"""Matte harvest (TV -> DB): learn the matte the user chose in the TV's own UI. SPEC.md §12.4."""
from conftest import add_asset, add_derivative, add_device, add_placement, item, resident

from frame_art_organizer import store


def _policy(conn, asset_id):
    return conn.execute("SELECT * FROM asset_policy WHERE asset_id = ?", (asset_id,)).fetchone()


def _placement_matte(conn, placement_id):
    return conn.execute("SELECT matte FROM placement WHERE id = ?", (placement_id,)).fetchone()[0]


def test_unchanged_matte_records_nothing(conn):
    dev = add_device(conn)
    a, _, _ = resident(conn, dev, 1, matte="none")
    assert store.harvest_mattes(conn, dev, [item("MY_F0001", "none")]) == []
    assert _policy(conn, a) is None            # no preference row invented


def test_a_tv_edit_is_recorded_per_asset_and_on_the_placement(conn):
    dev = add_device(conn)
    a, _, p = resident(conn, dev, 1, matte="none", name="beach.jpg")

    changes = store.harvest_mattes(conn, dev, [item("MY_F0001", "modern_seafoam")])

    assert changes == [{
        "asset_id": a, "placement_id": p, "content_id": "MY_F0001", "original_name": "beach.jpg",
        "old": "none", "new": "modern_seafoam", "shape": "wide",
    }]
    pol = _policy(conn, a)
    assert (pol["matte"], pol["matte_shape"]) == ("modern_seafoam", "wide")
    assert _placement_matte(conn, p) == "modern_seafoam"


def test_second_run_is_a_no_op(conn):
    dev = add_device(conn)
    resident(conn, dev, 1)
    listing = [item("MY_F0001", "shadowbox_sage")]
    assert len(store.harvest_mattes(conn, dev, listing)) == 1
    assert store.harvest_mattes(conn, dev, listing) == []


def test_dry_run_reports_but_writes_nothing(conn):
    dev = add_device(conn)
    a, _, p = resident(conn, dev, 1)
    listing = [item("MY_F0001", "modern_black")]

    dry = store.harvest_mattes(conn, dev, listing, apply=False)

    assert len(dry) == 1
    assert _policy(conn, a) is None and _placement_matte(conn, p) == "none"
    assert len(store.harvest_mattes(conn, dev, listing)) == 1     # a real run still finds it


def test_placement_missing_from_the_listing_is_skipped(conn):
    dev = add_device(conn)
    resident(conn, dev, 1)
    assert store.harvest_mattes(conn, dev, [item("MY_F9999", "modern_black")]) == []
    assert store.harvest_mattes(conn, dev, []) == []
    assert store.harvest_mattes(conn, dev, None) == []


def test_listing_item_without_a_matte_key_is_unknown_not_none(conn):
    dev = add_device(conn)
    a, _, p = resident(conn, dev, 1, matte="modern_seafoam")
    entry = item("MY_F0001")
    del entry["matte_id"]                                          # firmware omitted the field
    assert store.harvest_mattes(conn, dev, [entry]) == []
    assert _policy(conn, a) is None and _placement_matte(conn, p) == "modern_seafoam"


def test_firmware_spellings_of_no_matte_are_not_edits(conn):
    dev = add_device(conn)
    resident(conn, dev, 1, matte="none")
    for spelling in (None, "", "  ", "NONE"):
        assert store.harvest_mattes(conn, dev, [item("MY_F0001", spelling)]) == []


def test_user_resetting_a_matte_back_to_none_is_an_edit(conn):
    dev = add_device(conn)
    a, _, p = resident(conn, dev, 1, matte="modern_seafoam")
    changes = store.harvest_mattes(conn, dev, [item("MY_F0001", "none")])
    assert [(c["old"], c["new"]) for c in changes] == [("modern_seafoam", "none")]
    assert _policy(conn, a)["matte"] == "none"                     # an explicit choice, not NULL


def test_only_present_placements_are_considered(conn):
    dev = add_device(conn)
    for n, state in enumerate(("pending", "deleted_on_device", "error"), start=1):
        a = add_asset(conn, n)
        add_placement(conn, dev, add_derivative(conn, a), f"MY_F{n:04d}", state=state)
    listing = [item(f"MY_F{n:04d}", "modern_black") for n in (1, 2, 3)]
    assert store.harvest_mattes(conn, dev, listing) == []


def test_content_we_did_not_upload_is_ignored(conn):
    dev = add_device(conn)
    resident(conn, dev, 1)
    listing = [item("MY_F0001", "none"), item("MY_F0500", "modern_black")]   # 0500 = an orphan
    assert store.harvest_mattes(conn, dev, listing) == []


def test_other_devices_placements_are_not_touched(conn):
    dev = add_device(conn)
    other = store.ensure_device(conn, name="Other", host="192.0.2.99", duid="uuid:other")
    assert other != dev                                            # genuinely two devices
    resident(conn, other, 1)
    assert store.harvest_mattes(conn, dev, [item("MY_F0001", "modern_black")]) == []
    assert len(store.harvest_mattes(conn, other, [item("MY_F0001", "modern_black")])) == 1


def test_portrait_matte_id_is_ignored(conn):
    dev = add_device(conn)
    resident(conn, dev, 1, matte="none")
    assert store.harvest_mattes(conn, dev, [item("MY_F0001", "none", portrait_matte_id="modern_x")]) == []


def test_existing_policy_is_preserved(conn):
    dev = add_device(conn)
    a, _, _ = resident(conn, dev, 1)
    store.set_policy(conn, a, pinned=True, suppressed=False, weight=3.0)

    store.harvest_mattes(conn, dev, [item("MY_F0001", "flexible_black")])

    pol = _policy(conn, a)
    assert (pol["pinned"], pol["suppressed"], pol["weight"]) == (1, 0, 3.0)
    assert pol["matte"] == "flexible_black"


def test_shape_class_comes_from_the_derivative_size(conn):
    dev = add_device(conn)
    odd, _, _ = resident(conn, dev, 1, width=1600, height=1200)    # a native-aspect 4:3 (v2)
    unk, _, _ = resident(conn, dev, 2, width=None, height=None)    # size unknown
    store.harvest_mattes(conn, dev, [item("MY_F0001", "flexible_black"), item("MY_F0002", "shadowbox_black")])
    assert _policy(conn, odd)["matte_shape"] == "odd"
    assert _policy(conn, unk)["matte_shape"] is None               # never matched to any shape later


def test_the_three_edits_found_on_the_tv_on_2026_10_02(conn):
    """Mirrors the real TV: two photos none -> modernthin_black, one modern_seafoam -> shadowbox_sage."""
    dev = add_device(conn)
    resident(conn, dev, 83, content_id="MY_F0083", matte="none")
    resident(conn, dev, 71, content_id="MY_F0071", matte="none")
    resident(conn, dev, 79, content_id="MY_F0079", matte="modern_seafoam")
    resident(conn, dev, 82, content_id="MY_F0082", matte="none")             # untouched
    listing = [item("MY_F0083", "modernthin_black"), item("MY_F0071", "modernthin_black"),
               item("MY_F0079", "shadowbox_sage"), item("MY_F0082", "none")]

    changes = store.harvest_mattes(conn, dev, listing)

    assert sorted((c["content_id"], c["old"], c["new"], c["shape"]) for c in changes) == [
        ("MY_F0071", "none", "modernthin_black", "wide"),
        ("MY_F0079", "modern_seafoam", "shadowbox_sage", "wide"),
        ("MY_F0083", "none", "modernthin_black", "wide"),
    ]
    assert store.harvest_mattes(conn, dev, listing) == []                    # and then it's quiet


def test_present_placements_exposes_what_harvest_and_later_steps_need(conn):
    dev = add_device(conn)
    a, d, p = resident(conn, dev, 1, matte="modern_seafoam", name="x.jpg")
    (row,) = store.present_placements(conn, dev)
    assert (row["id"], row["asset_id"], row["derivative_id"], row["matte"]) == (p, a, d, "modern_seafoam")
    assert (row["width"], row["height"], row["original_name"]) == (3840, 2160, "x.jpg")
