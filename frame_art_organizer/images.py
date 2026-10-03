"""Turn any dropped image into a Frame-ready 3840x2160 JPEG.

Accepts whatever a phone or camera produces (JPEG/PNG/WebP/TIFF/HEIC...), honors
EXIF rotation, and fits it to the panel. Two fixed fit modes (the v1 pipeline):

  cover   - scale + center-crop to fill 16:9 (default; best for landscapes)
  contain - scale to fit and pad with a neutral background (no cropping; bars)

plus `auto` (the v2 pipeline, SPEC.md §12.2): native resolution, downscale only — near-16:9
photos are cropped to exactly 16:9, everything else is kept whole and left to the TV's matte.

Portrait / non-16:9 art is better served later by the Frame's own matte feature;
for now `contain` keeps the whole image, `cover` fills the panel.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

# Extensions we attempt to ingest. Decoding is what ultimately decides validity.
SUPPORTED_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".heic", ".heif",
}

# Optional HEIC/HEIF (iPhone). If the extension isn't installed, non-HEIC still works.
try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except Exception:  # noqa: BLE001
    HEIC_SUPPORTED = False

FRAME_W, FRAME_H = 3840, 2160

_R169 = 16 / 9
_SHAPE_TOLERANCE = 0.01   # a derivative within 1 % of 16:9 counts as 'wide'


def shape_class(width, height) -> str | None:
    """Coarse shape of a rendered derivative, used to scope matte preferences.

    ``'wide'`` = 16:9 (±1 %) — the TV offers its full matte list for these;
    ``'odd'``  = any other aspect — the TV offers only ``flexible``/``shadowbox``.
    ``None`` if the size is unknown (then no preference is ever matched to it).
    A matte chosen for one class must never be applied to the other (SPEC.md §12.3).
    """
    if not width or not height:
        return None
    return "wide" if abs(width / height - _R169) <= _SHAPE_TOLERANCE * _R169 else "odd"


# --- v2 render plan (SPEC.md §12.2): native resolution, downscale only, never upscale ---------
@dataclass(frozen=True)
class RenderPlan:
    kind: str                                 # 'fill' (centered crop to 16:9) | 'fit' (no crop)
    source: tuple[int, int]                   # source size after EXIF orientation
    crop: tuple[int, int, int, int] | None    # (left, top, right, bottom) in the source; None = no crop
    size: tuple[int, int]                     # output size — never larger than the source crop
    trim: float                               # fraction a crop to 16:9 would remove
    scale: float                              # <= 1.0 by construction
    shape: str | None                         # shape_class of the OUTPUT ('wide' | 'odd')


def trim_fraction(width: int, height: int) -> float:
    """Fraction of the photo a centered crop to 16:9 would remove: 1 - min(ar,16/9)/max(ar,16/9)."""
    ar = width / height
    return 1 - min(ar, _R169) / max(ar, _R169)


def plan_render(width: int, height: int, crop_tolerance: float = 0.16) -> RenderPlan:
    """Decide how to render a photo of this (EXIF-oriented) size. Pure.

    FILL when the 16:9 crop would trim no more than `crop_tolerance` (near-16:9 photos stay
    full-bleed): crop to exactly 16:9, centered. FIT otherwise: keep the photo whole — no crop,
    no padding; the TV shows it inside a matte (API notes §7.3). Either way the result is only
    ever scaled DOWN to fit 3840x2160: upscaling adds no detail and the TV scales for us.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"image size must be positive, got {width}x{height}")
    trim = trim_fraction(width, height)
    if trim <= crop_tolerance:
        kind = "fill"
        if width / height > _R169:                                     # too wide: trim columns
            cw, ch = min(width, round(height * 16 / 9)), height
        else:                                                          # too tall: trim rows
            cw, ch = width, min(height, round(width * 9 / 16))
        left, top = (width - cw) // 2, (height - ch) // 2
        crop = (left, top, left + cw, top + ch) if (cw, ch) != (width, height) else None
    else:
        kind, cw, ch, crop = "fit", width, height, None
    scale = min(1.0, FRAME_W / cw, FRAME_H / ch)
    size = (max(1, round(cw * scale)), max(1, round(ch * scale)))
    return RenderPlan(kind=kind, source=(width, height), crop=crop, size=size, trim=trim,
                      scale=scale, shape=shape_class(*size))


def low_res(width, height, long_edge: int) -> bool:
    """True if the photo's long edge is below `long_edge` px. A flag only — never blocks."""
    return bool(width and height and long_edge and max(width, height) < long_edge)


def render_derivative(path: str | Path, *, crop_tolerance: float = 0.16,
                      quality: int = 92) -> tuple[bytes, RenderPlan]:
    """Render `path` per `plan_render`; returns (JPEG bytes, the plan used)."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)   # apply camera orientation first; plan on what's shown
        im = im.convert("RGB")
        plan = plan_render(*im.size, crop_tolerance=crop_tolerance)
        if plan.scale >= 1.0:
            out = im.crop(plan.crop) if plan.crop else im
        else:
            out = im.resize(plan.size, Image.Resampling.LANCZOS, box=plan.crop)
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), plan


def normalize_to_frame(
    path: str | Path,
    mode: str = "cover",
    background: tuple[int, int, int] = (20, 20, 20),
    quality: int = 92,
) -> bytes:
    """Load `path`, produce exactly FRAME_W x FRAME_H JPEG bytes."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)  # apply camera orientation, then drop it
        im = im.convert("RGB")

        if mode == "cover":
            fitted = ImageOps.fit(
                im, (FRAME_W, FRAME_H), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)
            )
        elif mode == "contain":
            scaled = ImageOps.contain(
                im, (FRAME_W, FRAME_H), method=Image.Resampling.LANCZOS
            )
            fitted = Image.new("RGB", (FRAME_W, FRAME_H), background)
            fitted.paste(
                scaled, ((FRAME_W - scaled.width) // 2, (FRAME_H - scaled.height) // 2)
            )
        else:
            raise ValueError(f"unknown fit mode {mode!r} (use 'cover' or 'contain')")

        buf = io.BytesIO()
        fitted.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_exif_date(value) -> str | None:
    """Normalize an EXIF date value to 'YYYY:MM:DD HH:MM:SS' (the Frame's format)."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    value = str(value).strip().strip("\x00").strip()
    return value or None


def read_metadata(path: str | Path) -> dict:
    """Dimensions (as displayed), MIME, and EXIF capture date. Raises if undecodable."""
    with Image.open(path) as im:
        fmt = im.format
        exif = im.getexif()
        # DateTimeOriginal (0x9003) lives in the Exif sub-IFD (0x8769); fall back to
        # the top-level DateTime (0x0132).
        captured_at = None
        try:
            captured_at = _decode_exif_date(exif.get_ifd(0x8769).get(0x9003))
        except Exception:
            captured_at = None
        if captured_at is None:
            captured_at = _decode_exif_date(exif.get(0x0132))

        oriented = ImageOps.exif_transpose(im)  # report post-rotation dimensions
        width, height = oriented.size
        mime = Image.MIME.get(fmt) if fmt else None
    return {"width": width, "height": height, "captured_at": captured_at, "mime": mime}


def render_to_file(src: str | Path, dest: str | Path, mode: str = "cover",
                   quality: int = 92, crop_tolerance: float = 0.16) -> dict:
    """Render `src` to a Frame-ready JPEG at `dest`; returns its hash and REAL size.

    mode "cover"/"contain" = the v1 pipeline (always exactly 3840x2160); "auto" = the v2
    native-resolution pipeline (§12.2), where the size varies per photo.
    """
    plan = None
    if mode == "auto":
        data, plan = render_derivative(src, crop_tolerance=crop_tolerance, quality=quality)
        width, height = plan.size
    else:
        data = normalize_to_frame(src, mode=mode, quality=quality)
        width, height = FRAME_W, FRAME_H
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {"sha256": sha256_bytes(data), "width": width, "height": height,
            "bytes": len(data), "plan": plan.kind if plan else None}


def make_thumbnail(src: str | Path, dest: str | Path, width: int = 480,
                   quality: int = 82) -> Path:
    """Write a small web thumbnail (EXIF-oriented, aspect-preserved) to `dest`."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size
        height = max(1, round(h * width / w))
        im = im.resize((width, height), Image.Resampling.LANCZOS)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest, "JPEG", quality=quality)
    return dest
