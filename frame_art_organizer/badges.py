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


# --- the details view (read-only) ----------------------------------------------------------------
def _pipeline_label(fit_mode, pipeline_version) -> str:
    return f"{fit_mode} v{pipeline_version}"


def _matte_text(look: Look, pref_matte, pref_shape, d_w, d_h) -> str:
    m = look.matte
    if m is None:
        return "—"
    shape = images.shape_class(d_w, d_h)
    if pref_matte and pref_shape and pref_shape == shape and mattes.valid_matte_string(pref_matte):
        return f"{m} — chosen on the TV"
    text = f"{m} — default"
    if pref_matte and pref_shape:
        made = "16:9" if pref_shape == "wide" else "non-16:9"
        text += (f" (your TV choice {pref_matte} was made for a {made} version of this photo, "
                 "so it isn't applied to this one)")
    return text


def details(asset, deriv, pref_matte, pref_shape, placements, img: ImageSettings) -> dict:
    """Everything the details modal shows beyond the file facts: how it is rendered, what the
    configured pipeline would do with it, its matte, and what is on the TV. Read-only — the
    matte is changed on the TV and read back (SPEC.md §12.4)."""
    sw, sh = asset["width"], asset["height"]
    d_w, d_h = (deriv["width"], deriv["height"]) if deriv else (None, None)
    look = describe(sw, sh, d_w, d_h, deriv["fit_mode"] if deriv else None, pref_matte, pref_shape, img)

    render = None
    if deriv:
        render = {"width": d_w, "height": d_h, "fit_mode": deriv["fit_mode"],
                  "pipeline_version": deriv["pipeline_version"],
                  "pipeline": _pipeline_label(deriv["fit_mode"], deriv["pipeline_version"]),
                  "current": (deriv["fit_mode"], deriv["pipeline_version"]) == (img.default_fit, img.pipeline_version),
                  "kind": look.render, "crop_pct": look.crop_pct}

    plan = None
    if img.default_fit == "auto" and sw and sh:
        p = images.plan_render(sw, sh, img.crop_tolerance)
        m = mattes.matte_for(pref_matte, pref_shape, p.size[0], p.size[1], img.matte)
        if p.kind == "fill":
            how = (f"fill — {round(p.trim * 100)} % trimmed to 16:9" if p.crop else "fill — already 16:9")
            text = f"{how}, {p.size[0]} × {p.size[1]}"
        else:
            text = f"fit — shown whole, {p.size[0]} × {p.size[1]}, in a {m} matte" if m != "none" else \
                   f"fit — {p.size[0]} × {p.size[1]}; with no matte the TV crops it"
        plan = {"kind": p.kind, "size": list(p.size), "trim_pct": round(p.trim * 100), "matte": m, "text": text}

    tv = [{"content_id": p["content_id"], "matte": p["matte"], "width": p["width"], "height": p["height"],
           "pipeline": _pipeline_label(p["fit_mode"], p["pipeline_version"]),
           "current": (p["fit_mode"], p["pipeline_version"]) == (img.default_fit, img.pipeline_version)}
          for p in placements]

    return {
        "render": render,
        "plan": plan,
        "low_res": look.low_res,
        "badges": [{"kind": b.kind, "text": b.text, "title": b.title} for b in look.badges],
        "matte": {"policy": look.matte, "chosen_on_tv": pref_matte, "chosen_shape": pref_shape,
                  "text": _matte_text(look, pref_matte, pref_shape, d_w, d_h)},
        "on_tv": tv,
    }
