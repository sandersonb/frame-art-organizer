#!/usr/bin/env python3
"""Read-only probe of a Samsung The Frame local Art API.

Does NOT upload or change artwork. It only:
  - reads REST device info (model, token support)
  - opens the art websocket (triggers the one-time "Allow" prompt on the TV)
  - reports art-API support + version + current art-mode on/off state
  - lists My Photos content and the currently displayed art
The token is saved to ./.token so future connects don't re-prompt.
"""
import json
import ssl
import sys
import traceback

from samsungtvws import SamsungTVWS

from _common import HOST, TOKEN_FILE


def show(label, fn):
    """Run fn(), pretty-print result or the exception, never abort the probe."""
    try:
        result = fn()
        try:
            printable = json.dumps(result, indent=2, default=str)
        except (TypeError, ValueError):
            printable = repr(result)
        print(f"\n### {label}\nOK: {printable}")
        return result
    except Exception as e:  # noqa: BLE001 - probe wants every failure surfaced
        print(f"\n### {label}\nFAILED: {type(e).__name__}: {e}")
        return None


def main():
    print(f"Connecting to Samsung TV at {HOST} (port 8002 TLS)...")
    print("=> WATCH THE TV: accept the Allow prompt(s) with the remote.\n")

    # Long timeout so there's time to accept the on-screen prompt.
    tv = SamsungTVWS(
        host=HOST,
        port=8002,
        token_file=TOKEN_FILE,
        name="FrameArtProbe",
        timeout=60,
    )

    show("REST device info (no auth)", tv.rest_device_info)

    art = tv.art()

    show("art.supported()", art.supported)
    show("art.get_api_version()", art.get_api_version)
    show("art.get_device_info()", lambda: getattr(art, "get_device_info")())
    show("art.get_artmode()  (art mode on/off)", art.get_artmode)
    current = show("art.get_current()  (currently displayed)", art.get_current)

    # List My Photos (category MY-C0002) and everything available.
    my_photos = show(
        "art.available('MY-C0002')  (your uploaded photos)",
        lambda: art.available("MY-C0002"),
    )
    all_art = show("art.available()  (all art incl. store)", art.available)

    # Matte options (useful later for portrait handling).
    show("art.get_matte_list()", lambda: getattr(art, "get_matte_list")())

    # Compact summary.
    def _count(x):
        return len(x) if isinstance(x, list) else "n/a"

    print("\n================ SUMMARY ================")
    print(f"My Photos count : {_count(my_photos)}")
    print(f"Total art count : {_count(all_art)}")
    print(f"Current art     : {current if current is not None else 'n/a'}")
    print("Token saved to  :", TOKEN_FILE)
    print("========================================")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\nFATAL during probe:")
        traceback.print_exc()
        sys.exit(1)
