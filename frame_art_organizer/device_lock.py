"""One managed TV operation per device/database, including across CLI processes."""
from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .frame_client import FrameAsleep

_local = threading.local()


class DeviceBusy(FrameAsleep):
    pass


@contextmanager
def device_lock(conn, device_id):
    filename = next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")
    if not filename:  # In-memory databases cannot be shared by CLI processes.
        yield
        return
    path = str(Path(filename).resolve()) + f".device-{int(device_id)}.lock"
    held = getattr(_local, "held", set())
    if path in held:
        yield
        return
    with open(path, "a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeviceBusy("another managed operation is already using this TV") from None
        _local.held = held
        held.add(path)
        try:
            yield
        finally:
            held.remove(path)
            fcntl.flock(stream, fcntl.LOCK_UN)


def serialized_device(fn):
    @wraps(fn)
    def wrapped(client, conn, *args, **kwargs):
        device_id = kwargs["device_id"] if "device_id" in kwargs else args[0]
        with device_lock(conn, device_id):
            return fn(client, conn, *args, **kwargs)
    return wrapped
