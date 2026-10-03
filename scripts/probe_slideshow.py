#!/usr/bin/env python3
"""Read-only probe of the Frame's slideshow / auto-rotation controls and the
art API surface (to see what metadata/title we can set + display for photos)."""
import inspect
import json
import socket
import warnings

import urllib3

from samsungtvws import SamsungTVWS
from samsungtvws import art as art_mod

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from _common import HOST, TOKEN_FILE


def reachable() -> bool:
    try:
        with socket.create_connection((HOST, 8002), timeout=3):
            return True
    except OSError:
        return False


def show(label, fn):
    try:
        r = fn()
        try:
            r = json.dumps(r, indent=2, default=str)
        except (TypeError, ValueError):
            r = repr(r)
        print(f"\n### {label}\nOK: {r}")
    except Exception as e:  # noqa: BLE001
        print(f"\n### {label}\nFAILED: {type(e).__name__}: {e}")


def main():
    if not reachable():
        print("TV asleep/unreachable — wake it (Art Mode) and re-run.")
        return

    tv = SamsungTVWS(host=HOST, port=8002, token_file=TOKEN_FILE,
                     name="FrameArtProbe", timeout=8)
    art = tv.art()

    # Full method surface — scan for slideshow/rotation/metadata/title/name.
    methods = sorted(
        m for m in dir(art_mod.SamsungTVArt)
        if not m.startswith("_") and callable(getattr(art_mod.SamsungTVArt, m))
    )
    print("### ALL art methods\n" + ", ".join(methods))

    print("\n### signatures of interest")
    for name in methods:
        if any(k in name for k in ("slide", "rotat", "name", "title", "info",
                                   "detail", "meta", "favorite", "upload")):
            print(f"  {name}{inspect.signature(getattr(art_mod.SamsungTVArt, name))}")

    # Read-only status getters.
    show("get_auto_rotation_status()", lambda: getattr(art, "get_auto_rotation_status")())
    show("get_slideshow_status()", lambda: getattr(art, "get_slideshow_status")())
    show("get_current()  (metadata shape)", art.get_current)
    show("available('MY-C0002')  (photo metadata shape)",
         lambda: art.available("MY-C0002"))
    show("get_artmode_settings()", lambda: getattr(art, "get_artmode_settings")())


if __name__ == "__main__":
    main()
