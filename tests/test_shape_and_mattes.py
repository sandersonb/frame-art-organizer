"""Pure helpers: shape classification and matte normalization."""
import pytest

from frame_art_organizer import images, mattes


@pytest.mark.parametrize("w,h,expected", [
    (3840, 2160, "wide"),    # the v1 pipeline's output — every derivative today
    (1920, 1080, "wide"),
    (1280, 720, "wide"),
    (2133, 1200, "wide"),    # a 2:1 photo cropped to 16:9
    (3840, 2161, "wide"),    # off by a pixel — still 16:9
    (1600, 1200, "odd"),     # 4:3
    (2000, 2000, "odd"),     # square
    (1500, 2250, "odd"),     # portrait
    (2560, 1080, "odd"),     # 21:9
    (1920, 1200, "odd"),     # 16:10 (10 % off)
])
def test_shape_class(w, h, expected):
    assert images.shape_class(w, h) == expected


@pytest.mark.parametrize("w,h", [(None, 2160), (3840, None), (0, 0), (None, None)])
def test_shape_class_unknown_size_is_none(w, h):
    assert images.shape_class(w, h) is None


@pytest.mark.parametrize("raw,expected", [
    (None, "none"), ("", "none"), ("  ", "none"), ("none", "none"), ("NONE", "none"),
    ("modern_seafoam", "modern_seafoam"), ("  Shadowbox_Sage ", "shadowbox_sage"),
    ("flexible_black", "flexible_black"),
])
def test_normalize(raw, expected):
    assert mattes.normalize(raw) == expected
