"""Reachability-gated TV operations, serialized in a killable worker process."""
from __future__ import annotations

import logging
import math
import multiprocessing
import os
import re
import socket
import threading
import time
from dataclasses import dataclass

from samsungtvws import exceptions

from . import mattes
from .tv_worker import run_worker
from .worker_channel import WorkerChannel

log = logging.getLogger(__name__)
MY_PHOTOS_CATEGORY = "MY-C0002"
VALID_SLIDESHOW_MINUTES = (3, 15, 60, 720, 1440, 10080)


class FrameAsleep(RuntimeError):
    """The TV cannot currently be contacted."""


class FrameTimeout(FrameAsleep):
    """The operation exceeded its deadline; its worker has been killed and reaped."""


class FrameConnectionError(FrameAsleep):
    """Connection, authorization, or session startup failed."""


class FrameUploadUncertain(FrameAsleep):
    """An upload may have reached the TV. Do not automatically replay it."""


@dataclass
class FrameConfig:
    host: str
    port: int = 8002
    token_file: str = ".token"
    name: str = "FrameArtOrganizer"
    connect_timeout: float = 3.0
    ws_timeout: float = 60.0
    ws_call_timeout: float = 20.0
    connect_retries: int = 2
    connect_retry_delay: float = 1.0

    def __post_init__(self):
        for name in ("connect_timeout", "ws_timeout", "ws_call_timeout"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"[frame] {name} must be finite and positive")
        if not math.isfinite(self.connect_retry_delay) or self.connect_retry_delay < 0:
            raise ValueError("[frame] connect_retry_delay must be finite and nonnegative")


class FrameClient:
    def __init__(self, cfg: FrameConfig, *, observer=None, stack_seconds=None, worker_factory=None):
        self.cfg = cfg
        self._observer = observer
        self._stack_seconds = stack_seconds
        self._worker_factory = worker_factory
        self._process = self._pipe = None
        self._lock = threading.RLock()

    def is_reachable(self) -> bool:
        try:
            with socket.create_connection((self.cfg.host, self.cfg.port),
                                          timeout=self.cfg.connect_timeout):
                return True
        except OSError:
            return False

    def ensure_awake(self):
        self._progress("tcp.probe.begin")
        if not self.is_reachable():
            self._progress("tcp.probe.unreachable")
            raise FrameAsleep("TV API port is not reachable")
        self._progress("tcp.probe.complete")

    def _progress(self, stage, **fields):
        if self._observer is not None:
            self._observer(stage, **fields)
        log.log(logging.INFO if stage.startswith("upload.") or stage == "art.response.error" else logging.DEBUG,
                "%s %s", stage, fields)

    def _start_worker(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        try:
            self._pipe = WorkerChannel(parent)
            self._process = context.Process(
                target=run_worker,
                args=(child, self.cfg, self._stack_seconds, self._worker_factory, os.getpid()),
                name="frameart-tv", daemon=True,
            )
            self._process.start()
        except BaseException:
            parent.close()
            if self._pipe is not None:
                self._pipe.close()
            if self._process is not None:
                self._process.close()
            self._pipe = self._process = None
            raise
        finally:
            child.close()
        self._progress("worker.started", pid=self._process.pid)

    def _drop_connection(self):
        process, pipe = self._process, self._pipe
        self._process = self._pipe = None
        if pipe is not None:
            pipe.close()
        if process is not None:
            pid = process.pid
            if process.is_alive():
                process.kill()
            process.join()
            process.close()
            self._progress("worker.stopped", pid=pid)

    def _call(self, operation, args=(), kwargs=None, *, timeout=None):
        with self._lock:
            limit = self.cfg.ws_call_timeout if timeout is None else timeout
            deadline = time.monotonic() + limit
            attempts = 1 + max(0, self.cfg.connect_retries)
            for attempt in range(1, attempts + 1):
                try:
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    if self._process is None:
                        self._start_worker()
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    self._pipe.send((operation, (args, kwargs or {})), deadline)
                    while True:
                        message = self._pipe.recv(deadline)
                        if message[0] == "progress":
                            self._progress(message[1], **message[2])
                            continue
                        if message[0] == "result":
                            if operation == "upload_jpeg" and (
                                not isinstance(message[1], str) or not re.fullmatch(r"MY_F[0-9]+", message[1])
                            ):
                                raise FrameUploadUncertain("Upload returned no usable content_id; explicit resolution required")
                            return message[1]
                        _, error, submitted = message
                        self._drop_connection()
                        if error == "ApiUnreachable":
                            raise FrameAsleep("TV API port is not reachable")
                        if operation == "upload_jpeg" and submitted:
                            raise FrameUploadUncertain("Upload acknowledgement lost; explicit resolution required")
                        if error == "ResponseError":
                            raise exceptions.ResponseError("TV rejected the request")
                        if error == "ConnectionFailure" and attempt < attempts:
                            self._progress("connection.retry", attempt=attempt, attempts=attempts)
                            delay = min(self.cfg.connect_retry_delay, max(0, deadline-time.monotonic()))
                            if delay > 0:
                                time.sleep(delay)
                            break  # Retry the outer attempt with a new worker/pipe.
                        raise FrameConnectionError(f"TV operation {operation} failed ({error})")
                except TimeoutError:
                    self._drop_connection()
                    self._progress("operation.timeout", operation=operation, seconds=limit)
                    if operation == "upload_jpeg":
                        raise FrameUploadUncertain("Upload deadline expired; explicit resolution required") from None
                    raise FrameTimeout(f"TV operation {operation} exceeded {limit}s") from None
                except (EOFError, BrokenPipeError, OSError):
                    self._drop_connection()
                    if operation == "upload_jpeg":
                        raise FrameUploadUncertain("TV worker exited during upload; explicit resolution required") from None
                    raise FrameConnectionError("TV worker exited before returning a result") from None
                except (FrameAsleep, exceptions.ResponseError):
                    self._drop_connection()
                    raise
                except Exception:
                    self._drop_connection()
                    if operation == "upload_jpeg":
                        raise FrameUploadUncertain("Upload result could not be read; explicit resolution required") from None
                    raise FrameConnectionError("TV worker channel failed") from None
                except BaseException:
                    self._drop_connection()
                    raise

    def close(self):
        with self._lock:
            self._progress("client.close.begin")
            try:
                if self._pipe is not None:
                    try:
                        deadline = time.monotonic() + 1.0
                        self._pipe.send(("close", None), deadline)
                        while True:
                            message = self._pipe.recv(deadline)
                            if message[0] == "progress":
                                self._progress(message[1], **message[2])
                            if time.monotonic() >= deadline:
                                break
                    except (TimeoutError, EOFError, BrokenPipeError, OSError):
                        pass
            finally:
                self._drop_connection()
                self._progress("client.close.complete")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def device_info(self):
        return self._call("device_info")

    def api_version(self):
        return self._call("api_version")

    def artmode(self):
        return self._call("artmode")

    def current(self):
        return self._call("current")

    def list_my_photos(self):
        return self._call("list_my_photos", (MY_PHOTOS_CATEGORY,))

    def upload_jpeg(self, data: bytes, matte="none", date=None):
        if not mattes.valid_matte_string(matte):
            raise ValueError(f"refusing to upload with matte {matte!r}: not in the TV's matte list")
        kwargs = {"matte": mattes.normalize(matte), "portrait_matte": "none", "file_type": "JPEG"}
        if date:
            kwargs["date"] = date
        return self._call("upload_jpeg", (data,), kwargs,
                          timeout=max(self.cfg.ws_call_timeout, 45.0))

    def select(self, content_id, show=True):
        return self._call("select", (content_id,), {"show": show})

    def delete(self, content_id):
        try:
            confirmed = self._call("delete", (content_id,))
        except exceptions.ResponseError:
            confirmed = False
        if confirmed:
            return True
        # A lost earlier delete reply can leave an already-absent item recorded
        # locally. Only a fresh successful inventory may confirm its absence.
        return all(item.get("content_id") != content_id for item in self.list_my_photos())

    def set_slideshow(self, duration, shuffle=True, category_id=MY_PHOTOS_CATEGORY):
        if duration not in VALID_SLIDESHOW_MINUTES:
            raise ValueError(f"slideshow interval must be one of {VALID_SLIDESHOW_MINUTES} minutes; got {duration}")
        return self._call("set_slideshow", kwargs={"duration": duration, "type": bool(shuffle),
                                                  "category_id": category_id})
