"""Deadline-aware parent end of a multiprocessing Pipe on macOS/Linux.

Connection.send/recv can block after poll reports just one byte. Use the same
length-prefixed pickle framing over a nonblocking duplicate of its Unix socket.
Only our own trusted worker can write to this channel, never a network peer.
"""
from __future__ import annotations

import pickle
import select
import socket
import struct
import time
from multiprocessing.reduction import ForkingPickler


class WorkerChannel:
    MAX_MESSAGE = 128 * 1024 * 1024

    def __init__(self, connection):
        self.sock = socket.fromfd(connection.fileno(), socket.AF_UNIX, socket.SOCK_STREAM)
        connection.close()
        self.sock.setblocking(False)
        self.buffer = bytearray()

    def _wait(self, deadline, *, writing=False):
        left = deadline-time.monotonic()
        if left <= 0:
            raise TimeoutError()
        readable, writable, _ = select.select([] if writing else [self.sock],
                                             [self.sock] if writing else [], [], left)
        if not (readable or writable):
            raise TimeoutError()

    def send(self, value, deadline):
        data = ForkingPickler.dumps(value)
        if len(data) > self.MAX_MESSAGE:
            raise ValueError("TV worker message exceeds the size limit")
        packet = memoryview(struct.pack("!i", len(data)) + data)
        while packet:
            self._wait(deadline, writing=True)
            try:
                sent = self.sock.send(packet)
            except BlockingIOError:
                continue
            if sent == 0:
                raise EOFError()
            packet = packet[sent:]

    def recv(self, deadline):
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            if len(self.buffer) >= 4:
                size = struct.unpack("!i", self.buffer[:4])[0]
                header = 4
                if size == -1:
                    if len(self.buffer) >= 12:
                        size = struct.unpack("!Q", self.buffer[4:12])[0]
                        header = 12
                    else:
                        size = None
                if size is not None:
                    if size < 0 or size > self.MAX_MESSAGE:
                        raise ValueError("Invalid TV worker message size")
                    if len(self.buffer) >= header+size:
                        data = bytes(self.buffer[header:header+size])
                        del self.buffer[:header+size]
                        return pickle.loads(data)
            self._wait(deadline)
            try:
                chunk = self.sock.recv(65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise EOFError()
            self.buffer.extend(chunk)

    def close(self):
        self.sock.close()
