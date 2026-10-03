#!/usr/bin/env python3
"""Title experiment: upload a photo with an embedded title (EXIF ImageDescription,
EXIF XPTitle, XMP dc:title) + two distinct dates, display it, and let a human read
the Art-Mode info overlay to see whether ANY of it surfaces as a title."""
import io
import json
import socket
import warnings

import piexif
import urllib3
from PIL import Image, ImageDraw, ImageFont
from samsungtvws import SamsungTVWS

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from _common import HOST, TOKEN_FILE, SCRATCH as SP

TITLE = "TAHOE SUNSET TITLE TEST"
UPLOAD_DATE = "2019:07:04 18:30:00"   # passed to upload(date=...)
EXIF_DATE = "2005:12:25 09:00:00"     # embedded EXIF DateTimeOriginal (different!)

XMP = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
    f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{TITLE}</rdf:li></rdf:Alt></dc:title>'
    '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
).encode("utf-8")


def font(size):
    for p in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def build_jpeg():
    img = Image.new("RGB", (3840, 2160), (200, 90, 40))  # warm sunset-ish
    d = ImageDraw.Draw(img)
    lines = [("TITLE METADATA TEST", font(170), 300),
             (f'embedded title: "{TITLE}"', font(110), 900),
             ("upload date 2019-07-04  |  exif date 2005-12-25", font(85), 1250),
             ("open the info overlay ->", font(90), 1600)]
    for text, f, y in lines:
        b = f.getbbox(text)
        d.text(((3840 - (b[2] - b[0])) // 2 - b[0], y), text, font=f, fill=(255, 250, 245))

    exif_dict = {
        "0th": {
            piexif.ImageIFD.ImageDescription: TITLE.encode("ascii"),
            piexif.ImageIFD.XPTitle: TITLE.encode("utf-16-le") + b"\x00\x00",
            piexif.ImageIFD.DateTime: EXIF_DATE.encode("ascii"),
        },
        "Exif": {piexif.ExifIFD.DateTimeOriginal: EXIF_DATE.encode("ascii")},
        "1st": {}, "GPS": {}, "thumbnail": None,
    }
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, exif=piexif.dump(exif_dict), xmp=XMP)
    return buf.getvalue()


def main():
    try:
        socket.create_connection((HOST, 8002), timeout=3).close()
    except OSError:
        print("TV asleep/unreachable — wake it (Art Mode) and re-run.")
        return
    art = SamsungTVWS(host=HOST, port=8002, token_file=TOKEN_FILE,
                      name="FrameArtProbe", timeout=10).art()

    data = build_jpeg()
    print(f"Uploading title-test image ({len(data):,} bytes)")
    print(f"  embedded title : {TITLE}  (EXIF ImageDescription + XPTitle + XMP dc:title)")
    print(f"  upload date    : {UPLOAD_DATE}")
    print(f"  embedded EXIF  : {EXIF_DATE}")
    cid = art.upload(data, matte="none", portrait_matte="none",
                     file_type="JPEG", date=UPLOAD_DATE)
    print(f"  -> content_id  : {cid}")
    with open(f"{SP}/title_test_id.json", "w") as fh:
        json.dump(cid, fh)

    art.select_image(cid, show=True)
    print("\nDISPLAYING NOW. On the Frame remote, press the UP/▲ or info button to open")
    print("the artwork info overlay, then tell me:")
    print("  1) TITLE line: does it say 'TAHOE SUNSET TITLE TEST', or a date, or blank?")
    print("  2) DATE line : 07/04/2019 (upload param), 12/25/2005 (embedded EXIF), or today?")


if __name__ == "__main__":
    main()
