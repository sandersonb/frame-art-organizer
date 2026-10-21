"""Serialized TV I/O in a disposable process; no database access in the worker."""
from __future__ import annotations

import faulthandler
import logging
import os
import re
import socket
import ssl
import sys
import threading

import websocket
from samsungtvws import SamsungTVWS
from samsungtvws.art import SamsungTVArt
from samsungtvws import exceptions, helper


class ApiUnreachable(RuntimeError):
    pass


class ManagedArt(SamsungTVArt):
    """Own sockets during startup, tolerate incidental events, and report upload stages."""

    def __init__(self, *args, progress=lambda *a, **k: None, **kwargs):
        super().__init__(*args, **kwargs)
        self.progress = progress
        self._transfer = None
        self.upload_submitted = False
        self._request_id = None
        self._request_label = "none"

    def _new_request_uuid(self):
        value = super()._new_request_uuid()
        self._request_id = value
        return value

    def _set_token(self, token):
        # The upstream method logs the token at INFO. Absence of a token is fine.
        if self.token_file is not None:
            with open(self.token_file, "w") as stream:
                stream.write(token)
        else:
            self.token = token
        self.progress("pairing.token_saved")

    def _websocket_event(self, event, response):
        known = {"ms.channel.connect", "ms.channel.ready", "ms.channel.clientConnect",
                 "ms.channel.clientDisconnect", "ms.channel.unauthorized", "ms.channel.timeOut",
                 "ms.error", "ed.edenTV.update", "ms.voiceApp.hide", "d2d_service_message"}
        fields = {"event": event if isinstance(event, str) and event in known else "other"}
        data = response.get("data")
        if event == "ms.channel.connect" and isinstance(data, dict):
            clients = data.get("clients")
            if isinstance(clients, list):
                fields["client_count"] = len(clients)
                fields["host_count"] = sum(c.get("isHost") is True for c in clients if isinstance(c, dict))
        self.progress("websocket.event", **fields)
        if event == "d2d_service_message":
            payload = self._decode_d2d_payload(response)
            if isinstance(payload, dict) and payload.get("event") == "error":
                code = payload.get("error_code")
                code = int(str(code)) if re.fullmatch(r"-?[0-9]{1,10}", str(code)) else "unknown"
                self.progress("art.response.error", request=self._request_label, code=code,
                              matched=payload.get("request_id", payload.get("id")) == self._request_id)

    def open(self):
        if self.connection is not None:
            return self.connection
        self.progress("websocket.upgrade.begin")
        try:
            # If creation itself hangs, the parent deadline kills this process.
            self.connection = websocket.create_connection(
                self._format_websocket_url(self.endpoint), timeout=self.timeout,
                sslopt={"cert_reqs": ssl.CERT_NONE} if self.port == 8002 else {},
                connection="Connection: Upgrade",
            )
            self.progress("websocket.upgrade.complete")
            connected = False
            while True:
                response = helper.process_api_response(self.connection.recv())
                event = response.get("event")
                self._websocket_event(event, response)
                if event == "ms.channel.unauthorized":
                    raise exceptions.UnauthorizedError("TV refused the client")
                if event in {"ms.channel.timeOut", "ms.error"}:
                    raise exceptions.ConnectionFailure("TV rejected startup")
                if event == "ms.channel.connect":
                    connected = True
                    self._check_for_token(response)
                elif event == "ms.channel.ready" and connected:
                    return self.connection
                # Client connect/disconnect broadcasts are not our readiness reply.
                # A steady stream cannot extend the parent's overall call deadline.
        except BaseException:
            self.abort()
            raise

    def _send_art_request(self, request_data, *args, **kwargs):
        request = request_data.get("request")
        known = {"api_version", "get_api_version", "get_artmode_status", "get_current_artwork",
                 "get_content_list", "get_slideshow_status", "set_slideshow_status",
                 "send_image", "delete_image_list", "select_image"}
        label = request if request in known else "other"
        self._request_label = label
        if kwargs.get("request_uuid") is not None:
            self._request_id = kwargs["request_uuid"]
        self.open()  # Separate connection failures from any possible mutation.
        if request == "send_image":
            self.upload_submitted = True  # Conservative: set BEFORE the first request write.
            self.progress("upload.submitted")
        self.progress("art.request.begin", request=label)
        result = super()._send_art_request(request_data, *args, **kwargs)
        self.progress("art.request.complete", request=label)
        return result

    def send_command(self, *args, **kwargs):
        super().send_command(*args, **kwargs)
        self.progress("art.request.sent")

    def _open_d2d_socket(self, conn_info):
        self.progress("upload.transfer_connect.begin")
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock = raw
        try:
            raw.settimeout(self.timeout)
            if conn_info.get("secured", False):
                sock = helper.get_ssl_context().wrap_socket(raw)
            self._transfer = sock
            sock.connect((conn_info["ip"], int(conn_info["port"])))
            self.progress("upload.transfer_connect.complete")
            return sock
        except BaseException:
            sock.close()
            raw.close()
            self._transfer = None
            raise

    def _upload_ws_binary_send_image(self, **kwargs):
        self.open()
        self.upload_submitted = True
        self.progress("upload.submitted")
        return super()._upload_ws_binary_send_image(**kwargs)

    def _wait_for_d2d(self, *, request_uuid, wait_for_sub_event):
        if wait_for_sub_event == "image_added":
            self.progress("upload.bytes_sent")
            self.progress("upload.awaiting_ack")
        result = super()._wait_for_d2d(request_uuid=request_uuid,
                                      wait_for_sub_event=wait_for_sub_event)
        if wait_for_sub_event == "image_added":
            self.progress("upload.acknowledged")
        return result

    def abort(self):
        """No graceful recv on the failure path; don't compete with a blocked recv."""
        if self._transfer is not None:
            try:
                self._transfer.close()
            except OSError:
                pass
            self._transfer = None
        conn, self.connection = self.connection, None
        if conn is not None:
            try:
                conn.abort()
            finally:
                conn.shutdown()

    def close(self):
        self.progress("connection.close.begin")
        try:
            if self.connection is not None:
                self.connection.close(timeout=0.25)
        finally:
            self.abort()
        self.progress("connection.close.complete")


def run_worker(pipe, cfg, stack_seconds=None, factory=None, parent_pid=None):
    """Only plain result/progress/error data crosses IPC; never raw exceptions/tokens."""
    logging.disable(logging.CRITICAL)
    if parent_pid is not None:
        def watch_parent():
            pause = threading.Event()
            while os.getppid() == parent_pid:
                pause.wait(.25)
            # An abruptly killed parent cannot reap us or enforce its deadline.
            # Exit the whole worker so the OS closes any blocked network sockets.
            os._exit(75)
        threading.Thread(target=watch_parent, name="frameart-parent-watch", daemon=True).start()
    if stack_seconds is not None:
        faulthandler.dump_traceback_later(stack_seconds, repeat=True, file=sys.stderr)

    def progress(stage, **fields):
        pipe.send(("progress", stage, fields))

    tv = art = None
    try:
        tv = (factory or SamsungTVWS)(
            host=cfg.host, port=cfg.port, token_file=cfg.token_file,
            name=cfg.name, timeout=cfg.ws_timeout,
        )
        while True:
            operation, payload = pipe.recv()
            if operation == "close":
                break
            try:
                if factory is None:
                    progress("tcp.probe.begin")
                    try:
                        with socket.create_connection((cfg.host, cfg.port), timeout=cfg.connect_timeout):
                            pass
                    except OSError:
                        progress("tcp.probe.unreachable")
                        raise ApiUnreachable() from None
                    progress("tcp.probe.complete")
                if operation == "device_info":
                    progress("rest.device_info.begin")
                    result = tv.rest_device_info()
                    progress("rest.device_info.complete")
                else:
                    if art is None:
                        if factory is not None:
                            art = tv.art()
                        else:
                            art = ManagedArt(
                                cfg.host, port=cfg.port, token_file=cfg.token_file,
                                name=cfg.name, timeout=cfg.ws_timeout, progress=progress,
                            )
                    art.upload_submitted = False
                    methods = {"api_version": "get_api_version", "artmode": "get_artmode",
                               "current": "get_current", "list_my_photos": "available",
                               "upload_jpeg": "upload", "select": "select_image",
                               "delete": "delete", "set_slideshow": "set_slideshow_status"}
                    args, kwargs = payload
                    result = getattr(art, methods[operation])(*args, **kwargs)
                pipe.send(("result", result))
            except Exception as exc:
                submitted = bool(getattr(art, "upload_submitted", False))
                # No exception strings: upstream errors may contain raw TV responses.
                pipe.send(("error", type(exc).__name__, submitted))
                # Never reuse a broken stream, including for failed upload batches.
                break
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        try:
            pipe.send(("error", type(exc).__name__, False))
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        for conn in (art, tv):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        pipe.close()
