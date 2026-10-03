"""Matte policy (SPEC.md §12.3): which matte a photo is uploaded with, and the TV vocabulary."""
import itertools
import logging

import pytest

from frame_art_organizer.mattes import (
    MATTE_COLORS, MATTE_TYPES, ODD_ALLOWED, MatteConfig, matte_for, normalize, valid_matte_string,
)

CFG = MatteConfig()   # default_matte="none", fit_matte="flexible_black", auto_matte=True
WIDE = (3840, 2160)
ODD = (1600, 1200)


# --- the TV's vocabulary --------------------------------------------------------------------
@pytest.mark.parametrize("v", ["none", "NONE", " modern_seafoam ", "flexible_black",
                               "shadowbox_burgandy", "modernthin_black", "squares_polar"])
def test_valid_matte_strings(v):
    assert valid_matte_string(v)


@pytest.mark.parametrize("v", ["", "  ", None, 5, "modern", "modern_", "_black", "modern_purple",
                               "fancy_black", "none_black", "flexible-black", "modern_black_x"])
def test_invalid_matte_strings(v):
    assert not valid_matte_string(v)


def test_vocabulary_matches_the_verified_unit():
    assert len(MATTE_COLORS) == 16 and "burgandy" in MATTE_COLORS     # the TV's own spelling
    assert "none" not in MATTE_TYPES and set(ODD_ALLOWED) <= set(MATTE_TYPES)
    assert ODD_ALLOWED == ("flexible", "shadowbox")                   # what M0 verified for non-16:9
    assert MATTE_COLORS["black"] == (34, 34, 33)                      # charcoal, not (0,0,0)


# --- matte_for ------------------------------------------------------------------------------
def test_defaults_per_shape():
    assert matte_for(None, None, *WIDE, CFG) == "none"
    assert matte_for(None, None, *ODD, CFG) == "flexible_black"
    assert matte_for(None, None, 1500, 2250, CFG) == "flexible_black"      # portrait
    assert matte_for(None, None, 2560, 1080, CFG) == "flexible_black"      # 21:9 has no special case


def test_a_users_tv_choice_is_reapplied_to_the_same_shape():
    assert matte_for("modernthin_black", "wide", *WIDE, CFG) == "modernthin_black"
    assert matte_for("shadowbox_sage", "odd", *ODD, CFG) == "shadowbox_sage"
    assert matte_for("  Shadowbox_Sage ", "odd", *ODD, CFG) == "shadowbox_sage"     # normalized


def test_a_choice_never_crosses_shape_classes():
    # The 2026-10-01 crash: a `modern_*` matte reaching a 4:3 image. A preference chosen on a
    # 16:9 item must be ignored for an odd derivative — and vice versa.
    assert matte_for("modern_seafoam", "wide", *ODD, CFG) == "flexible_black"
    assert matte_for("modernthin_black", "wide", 1500, 2250, CFG) == "flexible_black"
    assert matte_for("flexible_black", "odd", *WIDE, CFG) == "none"


def test_a_preference_with_unknown_shape_is_never_applied():
    assert matte_for("modern_seafoam", None, *WIDE, CFG) == "none"
    assert matte_for("modern_seafoam", None, *ODD, CFG) == "flexible_black"


def test_unknown_size_gets_none_even_with_a_loud_default():
    loud = MatteConfig(default_matte="modern_polar")
    assert matte_for(None, None, None, None, loud) == "none"
    assert matte_for("modern_polar", "wide", 0, 0, loud) == "none"


def test_auto_matte_off_means_odd_photos_get_none_unless_the_user_chose():
    off = MatteConfig(auto_matte=False)
    assert matte_for(None, None, *ODD, off) == "none"                       # the TV crops them
    assert matte_for("shadowbox_black", "odd", *ODD, off) == "shadowbox_black"   # but TV choice wins


def test_configured_defaults_are_used():
    cfg = MatteConfig(default_matte="modern_polar", fit_matte="shadowbox_navy")
    assert matte_for(None, None, *WIDE, cfg) == "modern_polar"
    assert matte_for(None, None, *ODD, cfg) == "shadowbox_navy"


def test_a_stored_matte_outside_the_tv_vocabulary_is_ignored_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert matte_for("bogus_matte", "odd", *ODD, CFG) == "flexible_black"
    assert "not in the TV's vocabulary" in caplog.text


def test_property_odd_photos_never_get_a_type_outside_the_verified_set_unless_the_user_chose_it_there():
    """For every preference/shape combination: an odd photo's matte is 'none', or from
    ODD_ALLOWED, or exactly the user's own same-shape ('odd') choice."""
    prefs = [None, "none", "modern_seafoam", "modernthin_black", "flexible_black", "shadowbox_sage",
             "panoramic_navy", "triptych_polar"]
    for pref, pshape, size in itertools.product(prefs, (None, "wide", "odd"), [ODD, (1500, 2250), (2000, 2000), (2560, 1080)]):
        m = matte_for(pref, pshape, *size, CFG)
        users_own = pref is not None and pshape == "odd" and normalize(pref) == m
        assert m == "none" or m.split("_")[0] in ODD_ALLOWED or users_own, (pref, pshape, size, m)
