#!/usr/bin/env python3
"""Model-B live demo, step 1: upload a 4-card set, show each, then let the TV's
native slideshow cycle MY-C0002. Saves original slideshow config + uploaded ids
to the scratchpad so step 2 can restore precisely and clean up."""
import io
import json
import signal
import socket
import time
import warnings

import urllib3
from PIL import Image, ImageDraw, ImageFont
from samsungtvws import SamsungTVWS

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from _common import HOST, TOKEN_FILE, SCRATCH as SP

CARDS = [(1, (170, 30, 45)), (2, (30, 110, 60)), (3, (30, 70, 150)), (4, (95, 45, 130))]


class Timeout(Exception):
    pass


signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(Timeout()))


def bounded(label, fn, secs=8):
    signal.alarm(secs)
    try:
        r = fn()
        print(f"  {label}: OK")
        return r
    except Timeout:
        print(f"  {label}: (no response within {secs}s — setters are fire-and-forget, continuing)")
        return None
    finally:
        signal.alarm(0)


def font(size):
    for p in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def card(n, rgb):
    img = Image.new("RGB", (3840, 2160), rgb)
    d = ImageDraw.Draw(img)
    for text, f, y in [(str(n), font(900), 560), ("MODEL B DEMO", font(150), 200),
                       (f"photo {n} of 4", font(110), 1650)]:
        b = f.getbbox(text)
        d.text(((3840 - (b[2] - b[0])) // 2 - b[0], y), text, font=f, fill=(245, 245, 245))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def main():
    try:
        socket.create_connection((HOST, 8002), timeout=3).close()
    except OSError:
        print("TV asleep/unreachable — wake it (Art Mode) and re-run.")
        return
    tv = SamsungTVWS(host=HOST, port=8002, token_file=TOKEN_FILE, name="FrameArtProbe", timeout=8)
    art = tv.art()

    print("[1] Capturing current slideshow config (for restore) ...")
    orig = bounded("get_slideshow_status", art.get_slideshow_status)
    if orig:
        with open(f"{SP}/original_slideshow.json", "w") as fh:
            json.dump({k: orig.get(k) for k in ("value", "category_id", "type")}, fh)
        print(f"    original: value={orig.get('value')} category={orig.get('category_id')} type={orig.get('type')}")

    print("[2] Uploading 4 demo cards to MY-C0002 ...")
    ids = []
    for n, rgb in CARDS:
        cid = art.upload(card(n, rgb), matte="none", portrait_matte="none", file_type="JPEG")
        ids.append(cid)
        print(f"    card {n} -> {cid}")
    with open(f"{SP}/demo_ids.json", "w") as fh:
        json.dump(ids, fh)

    n_photos = len(art.available("MY-C0002"))
    print(f"[3] MY-C0002 now holds {n_photos} photo(s)")

    print("[4] Quick manual cycle — WATCH THE WALL (5s each) ...")
    for cid in ids:
        art.select_image(cid, show=True)
        print(f"    showing {cid}")
        time.sleep(5)

    print("[5] Handing off to TV native slideshow: MY-C0002, shuffle, duration=1 ...")
    bounded("set_slideshow_status", lambda: art.set_slideshow_status(
        duration=1, type=True, category_id="MY-C0002"))
    time.sleep(2)

    print("[6] Confirming the TV is now cycling OUR set ...")
    st = bounded("get_slideshow_status", art.get_slideshow_status)
    if st:
        try:
            n_in_show = len(json.loads(st.get("content_list", "[]")))
        except Exception:
            n_in_show = "?"
        print(f"    slideshow: value={st.get('value')} category={st.get('category_id')} "
              f"type={st.get('type')} items={n_in_show} current={st.get('current_content_id')}")

    print("\nDEMO LIVE. The TV is now running a shuffle slideshow over your 4 cards.")
    print("Watch as long as you like; say the word and step 2 restores your Art Store")
    print("slideshow and deletes the demo cards.")


if __name__ == "__main__":
    main()
