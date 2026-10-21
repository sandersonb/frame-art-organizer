"""Transport ownership, safe handshake events, and a real loopback readiness stall."""
import base64
import hashlib
import json
import socket
import threading
import time

import pytest
import websocket
from samsungtvws import exceptions

from frame_art_organizer import tv_worker
from frame_art_organizer.frame_client import FrameClient, FrameConfig, FrameTimeout
from frame_art_organizer.tv_worker import ManagedArt


class FakeWS:
    def __init__(self, events):
        self.events = iter(events)
        self.aborted = self.closed = self.shutdowns = 0

    def recv(self):
        value = next(self.events)
        if isinstance(value, Exception): raise value
        return json.dumps(value)

    def close(self, **kwargs): self.closed += 1
    def abort(self): self.aborted += 1
    def shutdown(self): self.shutdowns += 1


def test_handshake_tolerates_client_events_before_and_after_connect(monkeypatch):
    ws = FakeWS([{"event": "ms.channel.clientDisconnect"},
                 {"event": "ms.channel.connect", "data": {}},
                 {"event": "ms.channel.clientConnect"}, {"event": "ms.channel.ready"}])
    monkeypatch.setattr(tv_worker.websocket, "create_connection", lambda *a, **k: ws)
    art = ManagedArt("192.0.2.10")
    assert art.open() is ws
    assert art.open() is ws  # Reopening an established stream does not wait for another ready.
    art.close()
    assert ws.closed == ws.aborted == ws.shutdowns == 1


def test_startup_failure_closes_the_socket_even_before_connect(monkeypatch):
    ws = FakeWS([websocket.WebSocketTimeoutException("secret")])
    monkeypatch.setattr(tv_worker.websocket, "create_connection", lambda *a, **k: ws)
    art = ManagedArt("192.0.2.10")
    with pytest.raises(websocket.WebSocketTimeoutException): art.open()
    assert art.connection is None and ws.aborted == ws.shutdowns == 1


def test_unauthorized_is_not_ignored_and_socket_is_closed(monkeypatch):
    ws = FakeWS([{"event": "ms.channel.unauthorized", "data": {"token": "SECRET"}}])
    monkeypatch.setattr(tv_worker.websocket, "create_connection", lambda *a, **k: ws)
    art = ManagedArt("192.0.2.10")
    with pytest.raises(exceptions.UnauthorizedError): art.open()
    assert ws.shutdowns == 1


def test_token_is_optional_and_if_returned_is_never_logged(monkeypatch, tmp_path, caplog):
    token = tmp_path / "token"
    ws = FakeWS([{"event": "ms.channel.connect", "data": {"token": "SECRET"}},
                 {"event": "ms.channel.ready"}])
    monkeypatch.setattr(tv_worker.websocket, "create_connection", lambda *a, **k: ws)
    events = []
    art = ManagedArt("192.0.2.10", token_file=str(token),
                     progress=lambda stage, **kw: events.append((stage, kw)))
    art.open()
    art.close()
    assert token.read_text() == "SECRET"
    assert "SECRET" not in caplog.text + str(events)


def test_response_error_reports_only_numeric_code_and_matching_status():
    events = []
    art = ManagedArt("192.0.2.10", progress=lambda stage, **kw: events.append((stage, kw)))
    art._request_id, art._request_label = "same", "send_image"
    art._websocket_event("d2d_service_message", {"data": json.dumps({
        "event": "error", "error_code": "-7", "request_id": "same", "token": "SECRET",
        "request_data": "SECRET RESPONSE BODY"})})
    assert events[-1] == ("art.response.error", {"request": "send_image", "code": -7, "matched": True})
    assert "SECRET" not in str(events)


def test_failed_transfer_connect_explicitly_closes_socket(monkeypatch):
    class FailedSocket:
        closed = 0
        def settimeout(self, timeout): pass
        def connect(self, addr): raise OSError("failed")
        def close(self): self.closed += 1

    sock = FailedSocket()
    monkeypatch.setattr(tv_worker.socket, "socket", lambda *a: sock)
    art = ManagedArt("192.0.2.10", timeout=1)
    with pytest.raises(OSError): art._open_d2d_socket({"ip": "192.0.2.10", "port": 1234})
    assert sock.closed >= 1 and art._transfer is None


def test_upload_is_marked_submitted_before_the_request_write(monkeypatch):
    art = ManagedArt("192.0.2.10")
    monkeypatch.setattr(art, "open", lambda: None)
    from samsungtvws.art import SamsungTVArt

    def fail(self, *a, **k):
        assert self.upload_submitted
        raise OSError("write failed")

    monkeypatch.setattr(SamsungTVArt, "_send_art_request", fail)
    with pytest.raises(OSError): art._send_art_request({"request": "send_image"})


class StalledTV:
    """Local HTTP upgrade server that never sends Art readiness."""
    def __init__(self, chatter=False):
        self.chatter = chatter
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen()
        self.sock.settimeout(.1)
        self.stop = threading.Event()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(2)
        self.sock.close()

    @staticmethod
    def send_event(conn, event):
        frame = websocket.ABNF.create_frame(json.dumps({"event": event}), websocket.ABNF.OPCODE_TEXT)
        frame.mask_value = 0
        conn.sendall(frame.format())

    def run(self):
        while not self.stop.is_set():
            try: conn, _ = self.sock.accept()
            except socket.timeout: continue
            with conn:
                conn.settimeout(.1)
                data = b""
                while b"\r\n\r\n" not in data and not self.stop.is_set():
                    try: part = conn.recv(4096)
                    except socket.timeout: continue
                    if not part: break  # The reachability probe is not an HTTP client.
                    data += part
                if b"Sec-WebSocket-Key:" not in data: continue
                key = next(line.split(b":", 1)[1].strip() for line in data.split(b"\r\n")
                           if line.lower().startswith(b"sec-websocket-key:"))
                accept = base64.b64encode(hashlib.sha1(key+b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                             b"Connection: Upgrade\r\nSec-WebSocket-Accept: "+accept+b"\r\n\r\n")
                self.send_event(conn, "ms.channel.connect")
                while not self.stop.is_set():
                    try:
                        if self.chatter:
                            self.send_event(conn, "ms.channel.clientDisconnect")
                        if not conn.recv(1024):
                            self.closed.set()
                            break
                    except socket.timeout: continue
                    except OSError:
                        self.closed.set()
                        break


@pytest.mark.parametrize("chatter", [False, True])
def test_real_missing_ready_is_bounded_and_socket_disappears(chatter, tmp_path):
    events = []
    with StalledTV(chatter) as tv:
        fc = FrameClient(FrameConfig(host="127.0.0.1", port=tv.port, ws_timeout=30,
                                    ws_call_timeout=1, token_file=str(tmp_path / "absent")),
                         observer=lambda stage, **kw: events.append((stage, kw)))
        start = time.monotonic()
        with pytest.raises(FrameTimeout): fc.api_version()
        fc.close()
        assert time.monotonic()-start < 2
        assert tv.closed.wait(1)
    assert any(kw.get("event") == "ms.channel.connect" for _, kw in events)
    assert not any(stage == "art.request.sent" for stage, _ in events)
