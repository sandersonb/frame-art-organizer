"""OS locks serialize managed mutations and are automatically released on crash."""
import subprocess
import sys
import threading

import pytest
from conftest import add_device

from frame_art_organizer.device_lock import DeviceBusy, device_lock


def test_nested_calls_in_same_thread_are_reentrant(conn):
    dev = add_device(conn)
    with device_lock(conn, dev):
        with device_lock(conn, dev): pass


def test_other_thread_cannot_mutate_same_device(conn, tmp_path):
    from frame_art_organizer import db
    dev = add_device(conn)
    errors = []
    def competing():
        c = db.open_db(tmp_path / "state.db")
        try:
            with device_lock(c, dev): errors.append("unexpected acquisition")
        except DeviceBusy:
            errors.append("busy")
        finally: c.close()
    with device_lock(conn, dev):
        thread = threading.Thread(target=competing)
        thread.start(); thread.join(3)
        assert not thread.is_alive()
    assert errors == ["busy"]


def test_cross_process_exclusion_and_release_after_crash(conn, tmp_path):
    from frame_art_organizer import db
    dev = add_device(conn)
    script = """
import sys,time
from frame_art_organizer import db
from frame_art_organizer.device_lock import device_lock
c=db.open_db(sys.argv[1])
with device_lock(c,int(sys.argv[2])):
 print('locked',flush=True)
 time.sleep(60)
"""
    proc = subprocess.Popen([sys.executable, "-u", "-c", script, str(tmp_path / "state.db"), str(dev)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(DeviceBusy):
            with device_lock(conn, dev): pass
        proc.kill();proc.wait()
        with device_lock(conn, dev): pass
    finally:
        if proc.poll() is None: proc.kill();proc.wait()
        proc.stdout.close()
