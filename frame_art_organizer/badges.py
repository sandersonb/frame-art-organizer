"""What each photo looks like on the Frame, in a few words — the gallery badges (SPEC.md §12.7).

Pure: no DB, no TV. The look is derived from the derivative that is *actually rendered*, so it is
truthful under the v1 pipeline (everything is cropped to 16:9) as well as under v2 (near-16:9
photos cropped, the rest kept whole and matted by the TV). Under `auto` it agrees with
`fao plan-report` by construction: both come from the same render geometry and `matte_for`.

Informational only — nothing here blocks an upload or raises a warning dialog.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import images, mattes
from .config import ImageSettings


@dataclass(frozen=True)
class Badge:
    kind: str      # crop | fit | tvcrop | bars | lowres | pending  (the template colors by kind)
    text: str
    title: str     # tooltip


@dataclass(frozen=True)
class Look:
    render: str                      # none | full | crop | fit | tvcrop | bars
    crop_pct: int | None = None      # how much of the photo is trimmed (crop / tvcrop)
    matte: str | None = None         # the matte an upload of this derivative would use
    low_res: bool = False
    badges: list[Badge] = field(default_factory=list)


def describe(src_w, src_h, d_w, d_h, d_fit, pref_matte, pref_shape, img: ImageSettings) -> Look:
    """The look of one photo: source size, the rendered derivative's size and fit mode, and the
    matte preference harvested from the TV (if any)."""
    badges: list[Badge] = []
    low = images.low_res(src_w, src_h, img.low_res_long_edge)
    render, pct, matte = "none", None, None

    if not (d_w and d_h):
        badges.append(Badge("pending", "not rendered yet",
                            "No derivative exists yet for the current settings; the next render creates it."))
    elif src_w and src_h:
        matte = mattes.matte_for(pref_matte, pref_shape, d_w, d_h, img.matte)
        src_wide = images.shape_class(src_w, src_h) == "wide"
        if images.shape_class(d_w, d_h) == "odd":
            if matte == "none":
                # No matte on a non-16:9 image: the TV center-crops it to fill the panel.
                render, pct = "tvcrop", round(images.trim_fraction(d_w, d_h) * 100)
                badges.append(Badge("tvcrop", f"cropped {pct} % by the TV",
                                    "Shown without a matte, so the TV center-crops it to 16:9. "
                                    "Pick a matte on the TV to show the whole photo."))
            else:
                render = "fit"
                badges.append(Badge("fit", f"fit · {matte}",
                                    f"Shown whole inside a {matte} matte. Change the matte on the TV; "
                                    "the app reads it back."))
        elif src_wide:
            render = "full"
        elif d_fit == "contain":
            render = "bars"
            badges.append(Badge("bars", "letterboxed",
                                "Scaled to fit with neutral bars baked into the image."))
        else:
            render, pct = "crop", round(images.trim_fraction(src_w, src_h) * 100)
            badges.append(Badge("crop", f"cropped {pct} %",
                                f"Cropped to 16:9 (centered): {pct} % of the photo is trimmed."))
    if low:
        badges.append(Badge("lowres", f"low-res {src_w}×{src_h}",
                            f"Under {img.low_res_long_edge} px on the long edge: the TV will scale it up "
                            "and it may look soft. It is still uploaded at its own size."))
    return Look(render=render, crop_pct=pct, matte=matte, low_res=low, badges=badges)
