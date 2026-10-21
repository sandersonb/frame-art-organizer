"""Opt-in isolated diagnostics, using the same killable TV worker as production."""
from __future__ import annotations

import faulthandler
import importlib.metadata
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .frame_client import FrameAsleep, FrameClient, FrameConnectionError, FrameTimeout

EXIT_DEADLINE = 124


class Reporter:
    def __init__(self):
        self.started = time.monotonic()
        self._lock = threading.Lock()

    def __call__(self, stage, **fields):
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        details = " ".join(f"{key}={value}" for key, value in fields.items())
        with self._lock:
            print(f"{stamp} +{time.monotonic()-self.started:.3f}s {stage} {details}".rstrip(),
                  file=sys.stderr, flush=True)


def _supervise(command, timeout, report):
    # The probe may own an I/O worker. Kill the entire isolated group on deadline,
    # not just its parent (which would otherwise leave the network worker alive).
    proc = subprocess.Popen(command, start_new_session=True)

    def kill_group():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        result = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        report("probe.deadline", seconds=timeout, action="kill-child")
        kill_group()
        proc.wait()
        return EXIT_DEADLINE
    except BaseException:
        kill_group()
        proc.wait()
        raise
    report("probe.exited", code=result)
    return result


def run_info(config_path, *, timeout=60.0):
    report = Reporter()
    report("probe.launch", deadline_seconds=timeout)
    return _supervise(
        [sys.executable, "-u", "-m", "frame_art_organizer.diagnostics",
         str(Path(config_path).resolve()), str(min(10.0, timeout/3))], timeout, report,
    )


def _probe(config_path, stack_seconds):
    from .cli import _print_info

    report = Reporter()
    report("probe.environment", python=sys.version.split()[0], pid=os.getpid(),
           samsungtvws=importlib.metadata.version("samsungtvws"),
           websocket_client=importlib.metadata.version("websocket-client"))
    logging.disable(logging.CRITICAL)
    cfg = config.frame_config(config.load(config_path), Path(config_path).parent)
    cfg.connect_retries = 0
    report("probe.settings", connect_timeout=cfg.connect_timeout, socket_timeout=cfg.ws_timeout,
           call_timeout=cfg.ws_call_timeout, retries=0)
    # Parent and I/O worker both capture stacks, including stalled shutdown.
    faulthandler.dump_traceback_later(stack_seconds, repeat=True, file=sys.stderr)
    try:
        result = _print_info(FrameClient(cfg, observer=report, stack_seconds=stack_seconds))
    except FrameTimeout:
        report("probe.result", status="art-or-rest-timeout")
        return 2
    except FrameConnectionError:
        report("probe.result", status="connection-or-authorization-failure")
        return 2
    except FrameAsleep:
        report("probe.result", status="tcp-unreachable")
        return 2
    except Exception as exc:
        report("probe.result", status="error", error=type(exc).__name__)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        return 1
    report("probe.result", status="success")
    return result


if __name__ == "__main__":
    sys.exit(_probe(sys.argv[1], float(sys.argv[2])))
