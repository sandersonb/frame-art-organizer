"""IPC deadlines also bound partial messages and blocked large-image writes."""
import multiprocessing
import socket
import struct
import time

import pytest

from frame_art_organizer.worker_channel import WorkerChannel


@pytest.fixture
def channel():
    parent, child = multiprocessing.get_context("spawn").Pipe()
    pipe = WorkerChannel(parent)
    yield pipe, child
    pipe.close()
    child.close()


def test_frames_are_compatible_with_multiprocessing_connection(channel):
    parent, child = channel
    parent.send(("operation", {"data": b"jpeg"}), time.monotonic()+1)
    assert child.recv() == ("operation", {"data": b"jpeg"})
    child.send(("progress", "phase", {}))
    child.send(("result", [{"content_id": "MY_F0001"}]))
    assert parent.recv(time.monotonic()+1) == ("progress", "phase", {})
    assert parent.recv(time.monotonic()+1) == ("result", [{"content_id": "MY_F0001"}])


def test_partial_result_cannot_block_beyond_deadline(channel):
    parent, child = channel
    peer = socket.fromfd(child.fileno(), socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        peer.sendall(struct.pack("!i", 100) + b"partial")
        start = time.monotonic()
        with pytest.raises(TimeoutError): parent.recv(start+.05)
        assert time.monotonic()-start < .5
    finally:
        peer.close()


def test_large_image_write_to_nonreading_worker_obeys_deadline(channel):
    parent, _ = channel
    start = time.monotonic()
    with pytest.raises(TimeoutError): parent.send(("upload", b"x"*4_000_000), start+.05)
    assert time.monotonic()-start < .5
