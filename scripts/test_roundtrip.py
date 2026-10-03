#!/usr/bin/env python3
"""Upload round-trip test for the Samsung Frame Art API.

Flow (self-cleaning):
  1. record the currently displayed artwork
  2. generate a clearly-labeled 3840x2160 JPEG in-memory
  3. upload it to My Photos -> capture the TV-assigned content_id
  4. select it (display on the wall) and verify get_current() matches
  5. pause so a human can confirm the wall changed
  6. restore the original artwork
  7. delete the test upload and verify it's gone

Leaves the TV exactly as it started.
"""
import io
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from samsungtvws import SamsungTVWS

from _common import HOST, TOKEN_FILE
DISPLAY_SECONDS = 12

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNS.ttf",
]


def load_font(size):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def make_test_jpeg():
    """A 4K image no one could mistake for real art."""
    w, h = 3840, 2160
    img = Image.new("RGB", (w, h), (18, 52, 86))  # deep blue
    d = ImageDraw.Draw(img)
    # diagonal accent bars so it's obviously synthetic
    for i in range(-h, w, 240):
        d.line([(i, 0), (i + h, h)], fill=(30, 78, 120), width=40)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        ("FRAME ART ORGANIZER", load_font(220), (255, 255, 255)),
        ("upload round-trip test", load_font(120), (180, 210, 235)),
        (stamp, load_font(110), (150, 190, 220)),
        ("(this will auto-delete)", load_font(90), (120, 160, 195)),
    ]
    total = sum(f.getbbox(t)[3] - f.getbbox(t)[1] + 60 for t, f, _ in lines)
    y = (h - total) // 2
    for text, font, color in lines:
        bbox = font.getbbox(text)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        d.text(((w - tw) // 2 - bbox[0], y - bbox[1]), text, font=font, fill=color)
        y += th + 60
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def find(available, content_id):
    for item in available or []:
        if item.get("content_id") == content_id:
            return item
    return None


def main():
    tv = SamsungTVWS(host=HOST, port=8002, token_file=TOKEN_FILE,
                     name="FrameArtProbe", timeout=60)
    art = tv.art()

    # 1. record current
    current = art.get_current()
    orig_id = current.get("content_id")
    orig_category = current.get("category_id")
    print(f"[1] Current artwork: {orig_id} (category={orig_category}, "
          f"type={current.get('content_type')})")

    # 2 + 3. generate + upload
    data = make_test_jpeg()
    print(f"[2] Generated test JPEG: {len(data):,} bytes (3840x2160)")
    print("[3] Uploading to My Photos ...")
    new_id = art.upload(data, matte="none", portrait_matte="none", file_type="JPEG")
    print(f"    -> TV assigned content_id: {new_id}")

    # verify it landed in the library
    time.sleep(1.5)
    my_photos = art.available("MY-C0002")
    meta = find(my_photos, new_id) or find(art.available(), new_id)
    print(f"[3b] Appears in library: {'YES' if meta else 'NO'}")
    if meta:
        print(f"     metadata: {meta}")

    # 4. display it
    print(f"[4] Selecting {new_id} for display (show=True) ...")
    art.select_image(new_id, show=True)
    time.sleep(2.0)
    now = art.get_current()
    print(f"    get_current() -> {now.get('content_id')} "
          f"({'MATCH' if now.get('content_id') == new_id else 'MISMATCH'})")

    # 5. let a human confirm
    print(f"[5] *** LOOK AT THE WALL *** showing test image for {DISPLAY_SECONDS}s ...")
    time.sleep(DISPLAY_SECONDS)

    # 6. restore original
    print(f"[6] Restoring original artwork {orig_id} ...")
    try:
        art.select_image(orig_id, show=True)
        time.sleep(2.0)
        back = art.get_current().get("content_id")
        print(f"    get_current() -> {back} "
              f"({'RESTORED' if back == orig_id else 'DID NOT RESTORE — restore via remote'})")
    except Exception as e:  # noqa: BLE001
        print(f"    restore failed: {type(e).__name__}: {e} "
              f"(Art Store items may need the remote to reselect)")

    # 7. delete the test upload
    print(f"[7] Deleting test upload {new_id} ...")
    ok = art.delete(new_id)
    time.sleep(1.0)
    gone = find(art.available("MY-C0002"), new_id) is None
    print(f"    delete() returned {ok}; still present: {'NO (clean)' if gone else 'YES — remove manually'}")

    print("\nDONE. TV should be back to its original artwork.")


if __name__ == "__main__":
    main()
