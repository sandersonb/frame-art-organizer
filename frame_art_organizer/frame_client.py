"""Reachability-aware, timeout-bounded client for a Samsung The Frame local Art API.

Every operation gates on ``ensure_awake()`` (a fast TCP probe) AND runs the underlying
websocket/REST call under a hard thread timeout via ``_bounded()`` — so a firmware hang
(some art getters never return; see SAMSUNG_FRAME_API.md) can never freeze a caller such
as the long-running daemon. A timeout surfaces as ``FrameTimeout`` (a ``FrameAsleep``
subclass), so callers already handling "asleep → skip & retry" cover it for free.

All calls here were validated against a QN65LS03FAFXZA (2025), Art API 5.0.1.0.
"""
from __future__ import annotations

import concurrent.futures
import logging
import socket
from dataclasses import dataclass
from typing import Any, Optional

from samsungtvws import SamsungTVWS

log = logging.getLogger(__name__)

# The Frame's personal-photo bucket. Uploads land here (MY_Fxxxx, content_type="mobile").
MY_PHOTOS_CATEGORY = "MY-C0002"

# Slideshow intervals the firmware accepts for MY-C0002 (minutes); anything else → -7.
VALID_SLIDESHOW_MINUTES = (3, 15, 60, 720, 1440, 10080)


class FrameAsleep(RuntimeError):
    """The TV's API port isn't listening (powered off or in standby/night sleep)."""


class FrameTimeout(FrameAsleep):
    """An art call didn't return in time (TV hung). Treated like asleep — skip & retry."""


@dataclass
class FrameConfig:
    host: str
    port: int = 8002
    token_file: str = ".token"
    name: str = "FrameArtOrganizer"
    connect_timeout: float = 3.0
    ws_timeout: float = 60.0        # generous: covers the one-time on-screen Allow prompt
    ws_call_timeout: float = 20.0   # hard cap per art call so a hang can't freeze us


class FrameClient:
    """Thin wrapper over samsungtvws with a reachability gate + per-call timeout."""

    def __init__(self, cfg: FrameConfig):
        self.cfg = cfg
        self._tv: Optional[SamsungTVWS] = None
        self._art = None
        self._exec: Optional[concurrent.futures.ThreadPoolExecutor] = None

    # ------------------------------------------------------------------ reachability
    def is_reachable(self) -> bool:
        """True iff the API port accepts a TCP connection (a good proxy for 'awake')."""
        try:
            with socket.create_connection(
                (self.cfg.host, self.cfg.port), timeout=self.cfg.connect_timeout
            ):
                return True
        except OSError:
            return False

    def ensure_awake(self) -> None:
        if not self.is_reachable():
            raise FrameAsleep(
                f"{self.cfg.host}:{self.cfg.port} not listening — TV is off or asleep"
            )

    # ------------------------------------------------------------------ connection
    @property
    def tv(self) -> SamsungTVWS:
        if self._tv is None:
            self._tv = SamsungTVWS(
                host=self.cfg.host, port=self.cfg.port, token_file=self.cfg.token_file,
                name=self.cfg.name, timeout=int(self.cfg.ws_timeout),
            )
        return self._tv

    @property
    def art(self):
        if self._art is None:
            self._art = self.tv.art()
        return self._art

    @property
    def _executor(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._exec is None:
            self._exec = concurrent.futures.ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="frameart-ws"
            )
        return self._exec

    def _bounded(self, fn, timeout: Optional[float] = None):
        """Run `fn` under a hard timeout. A hung call raises FrameTimeout; the worker
        thread is abandoned (not awaited) so we never block on a wedged firmware call."""
        fut = self._executor.submit(fn)
        try:
            return fut.result(timeout=timeout or self.cfg.ws_call_timeout)
        except concurrent.futures.TimeoutError:
            raise FrameTimeout(
                f"art call exceeded {timeout or self.cfg.ws_call_timeout}s — TV not responding"
            )

    def close(self) -> None:
        if self._tv is not None:
            try:
                self._tv.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                log.debug("error closing TV connection", exc_info=True)
            self._tv = None
            self._art = None
        if self._exec is not None:
            self._exec.shutdown(wait=False, cancel_futures=True)
            self._exec = None

    def __enter__(self) -> "FrameClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ operations
    # Each gates on ensure_awake() then runs under _bounded() so a sleeping/hung TV
    # fails fast and predictably (FrameAsleep / FrameTimeout).

    def device_info(self) -> dict:
        self.ensure_awake()
        return self._bounded(self.tv.rest_device_info)

    def api_version(self) -> str:
        self.ensure_awake()
        return self._bounded(self.art.get_api_version)

    def artmode(self) -> str:
        """'on' or 'off'."""
        self.ensure_awake()
        return self._bounded(self.art.get_artmode)

    def current(self) -> dict:
        self.ensure_awake()
        return self._bounded(self.art.get_current)

    def list_my_photos(self) -> list[dict]:
        self.ensure_awake()
        return self._bounded(lambda: self.art.available(MY_PHOTOS_CATEGORY))

    def upload_jpeg(self, data: bytes, matte: str = "none", date: str | None = None) -> str:
        """Upload JPEG bytes to My Photos; returns the TV-assigned content_id (MY_Fxxxx).

        `date` ('YYYY:MM:DD HH:MM:SS') sets the date the Frame's overlay shows — the only
        settable metadata. Embedded EXIF is ignored (tested), so it must be passed here.
        """
        self.ensure_awake()
        kwargs: dict[str, Any] = {"matte": matte, "portrait_matte": matte, "file_type": "JPEG"}
        if date:
            kwargs["date"] = date
        # Uploads move bytes, so allow more time than a normal control call.
        return self._bounded(lambda: self.art.upload(data, **kwargs),
                             timeout=max(self.cfg.ws_call_timeout, 45.0))

    def select(self, content_id: str, show: bool = True) -> Any:
        self.ensure_awake()
        return self._bounded(lambda: self.art.select_image(content_id, show=show))

    def delete(self, content_id: str) -> bool:
        self.ensure_awake()
        return self._bounded(lambda: self.art.delete(content_id))

    def set_slideshow(self, duration: int, shuffle: bool = True,
                      category_id: str = MY_PHOTOS_CATEGORY):
        """Make the TV cycle `category_id`. `duration` minutes must be a valid interval."""
        if duration not in VALID_SLIDESHOW_MINUTES:
            raise ValueError(
                f"slideshow interval must be one of {VALID_SLIDESHOW_MINUTES} minutes; "
                f"got {duration}"
            )
        self.ensure_awake()
        return self._bounded(lambda: self.art.set_slideshow_status(
            duration=duration, type=bool(shuffle), category_id=category_id))
