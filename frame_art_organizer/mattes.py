"""Matte helpers — pure functions, no TV or DB access.

The Frame's matte is a string: ``none`` (fill the panel) or ``{type}_{color}``, e.g.
``shadowbox_polar``. The TV is the matte editor; we read its choice back (the "harvest",
SPEC.md §12.4) and re-apply it whenever a photo is uploaded again. Everything here follows
SPEC.md §12.3 and the field notes in SAMSUNG_FRAME_API.md §7.

Safety: a matte type the TV does not offer for an image's shape can crash the TV when the
photo is displayed (API notes §7.6). So a matte is only ever chosen from (a) what the user
picked on the TV for the *same shape class*, or (b) a configured default that was validated
at startup.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Types from get_matte_list() on the verified unit (API notes §7.7), minus "none".
MATTE_TYPES = (
    "modernthin", "modern", "modernwide", "flexible", "shadowbox",
    "panoramic", "triptych", "mix", "squares",
)

# The TV's 16 colors with their RGB (spelling is the TV's own, "burgandy" included).
MATTE_COLORS: dict[str, tuple[int, int, int]] = {
    "black": (34, 34, 33), "neutral": (137, 136, 134), "antique": (224, 219, 210),
    "warm": (231, 231, 223), "polar": (232, 230, 231), "sand": (164, 145, 113),
    "seafoam": (90, 104, 101), "sage": (170, 176, 141), "burgandy": (98, 39, 46),
    "navy": (39, 53, 74), "apricot": (239, 188, 96), "byzantine": (136, 86, 137),
    "lavender": (182, 171, 177), "redorange": (219, 103, 66), "skyblue": (105, 192, 211),
    "turquoise": (46, 150, 141),
}

# The only types verified (shown whole, no crash) for non-16:9 photos — 2:3, 4:3, 1:1 and 21:9
# (API notes §7.8). Widen only after verifying on the TV.
ODD_ALLOWED = ("flexible", "shadowbox")


def normalize(value) -> str:
    """Canonical form of a matte id for comparison.

    ``None``, ``''``, whitespace and ``'none'`` (any case) all mean "no matte" → ``'none'``.
    Anything else is stripped and lower-cased (the TV's ids are lower-case already).
    """
    if value is None:
        return "none"
    text = str(value).strip().lower()
    return text or "none"


def valid_matte_string(value) -> bool:
    """True for ``'none'`` or ``'<known type>_<known color>'`` (exactly the TV's vocabulary)."""
    if not isinstance(value, str):
        return False
    v = value.strip().lower()
    if v == "none":
        return True
    mtype, sep, color = v.partition("_")
    return bool(sep) and mtype in MATTE_TYPES and color in MATTE_COLORS


@dataclass(frozen=True)
class MatteConfig:
    """The matte defaults, already validated (see config.image_settings)."""
    default_matte: str = "none"          # for 16:9 ("wide") photos
    fit_matte: str = "flexible_black"    # initial matte for non-16:9 ("odd") photos
    auto_matte: bool = True              # False = odd photos upload with "none" (the TV crops)


def matte_for(pref_matte, pref_shape, width, height, cfg: MatteConfig) -> str:
    """The matte to upload this photo with. Pure.

    1. A matte the user chose on the TV is re-applied, but ONLY to a derivative of the same
       shape class it was chosen under — the TV offered it for that shape. That is what stops
       e.g. a `modern_*` chosen on a 16:9 item from reaching a 4:3 derivative.
    2. Otherwise the default for the derivative's shape: `default_matte` for 16:9 ("wide"),
       `fit_matte` for anything else ("odd") unless auto-matte is off.
    3. An unknown size gets "none" — the safest value (the TV simply crops).
    """
    from .images import shape_class  # local import: keeps this module free of Pillow at import

    shape = shape_class(width, height)
    if pref_matte and pref_shape and shape is not None and pref_shape == shape:
        if valid_matte_string(pref_matte):
            return normalize(pref_matte)
        log.warning("ignoring stored matte %r: not in the TV's vocabulary; using the default",
                    pref_matte)
    if shape == "wide":
        return cfg.default_matte
    if shape == "odd":
        return cfg.fit_matte if cfg.auto_matte else "none"
    return "none"
