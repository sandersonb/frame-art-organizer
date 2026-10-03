"""config.image_settings: defaults, validation, and that the shipped template is valid."""
from pathlib import Path

import pytest

from frame_art_organizer import config
from frame_art_organizer.config import ConfigError, ImageSettings, image_settings

ROOT = Path(__file__).resolve().parent.parent


def test_no_image_section_gives_the_defaults():
    s = image_settings({})
    assert s == ImageSettings()
    assert (s.default_fit, s.pipeline_version, s.default_matte) == ("cover", 1, "none")   # today's behavior
    assert s.crop_tolerance == 0.16 and s.auto_matte is True


def test_the_shipped_example_config_is_valid_and_dormant():
    s = image_settings(config.load(ROOT / "config.example.toml"))
    assert (s.default_fit, s.pipeline_version) == ("cover", 1)       # M2 ships dormant
    assert s.matte.fit_matte == "flexible_black" and s.matte.default_matte == "none"


def test_the_matte_property_combines_type_and_color():
    s = image_settings({"image": {"fit_matte_type": "shadowbox", "fit_matte_color": "navy",
                                  "default_matte": " Modern_Polar "}})
    assert s.matte.fit_matte == "shadowbox_navy" and s.matte.default_matte == "modern_polar"


def test_numbers_accept_ints_and_integral_floats():
    s = image_settings({"image": {"crop_tolerance": 0, "pipeline_version": 2.0, "jpeg_quality": 90}})
    assert (s.crop_tolerance, s.pipeline_version, s.jpeg_quality) == (0.0, 2, 90)
    assert isinstance(s.pipeline_version, int)


@pytest.mark.parametrize("key,value", [
    ("default_fit", "stretch"),
    ("default_matte", "modern"),            # a type with no color
    ("default_matte", "fancy_black"),
    ("fit_matte_type", "modern"),           # the type that crashed the TV on 4:3 — must be rejected
    ("fit_matte_type", "panoramic"),
    ("fit_matte_color", "purple"),
    ("auto_matte", "yes"),
    ("auto_matte", 1),
    ("crop_tolerance", 0.9),
    ("crop_tolerance", -0.1),
    ("crop_tolerance", "wide"),
    ("jpeg_quality", 0),
    ("jpeg_quality", 101),
    ("pipeline_version", 0),
    ("pipeline_version", 1.5),
    ("width", True),                        # bool is not a number here
    ("low_res_long_edge", -1),
])
def test_bad_values_fail_fast_naming_the_key(key, value):
    with pytest.raises(ConfigError, match=rf"\[image\] {key}"):
        image_settings({"image": {key: value}})


def test_the_error_for_a_dangerous_fit_matte_explains_why():
    with pytest.raises(ConfigError, match="can crash the TV"):
        image_settings({"image": {"fit_matte_type": "modern"}})
