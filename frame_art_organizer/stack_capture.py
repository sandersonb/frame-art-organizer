"""Bounded code-location stacks: no locals, source lines, paths or exceptions."""
from __future__ import annotations

import re
import sys
import threading
from contextlib import contextmanager


MODULE_ROOTS = {'frame_art_organizer', 'samsungtvws', 'websocket', 'urllib3', 'requests',
                'multiprocessing', 'threading', 'socket', 'ssl', 'logging', 'asyncio',
                'uvicorn', 'starlette', 'fastapi', 'concurrent', 'PIL', 'sqlite3',
                'contextlib', 'queue', 'selectors', 'http', 'anyio', 'pathlib'}


def sanitize_stacks(stacks):
    result = []
    if not isinstance(stacks, list):
        return result
    for stack in stacks[:32]:
        if not isinstance(stack, dict) or type(stack.get('thread_id')) is not int:
            continue
        frames = stack.get('frames')
        if not isinstance(frames, list):
            continue
        locations = []
        for frame in frames[:24]:
            if not isinstance(frame, dict) or type(frame.get('line')) is not int:
                continue
            module, name = frame.get('module'), frame.get('function')
            trusted = (isinstance(module, str) and module.split('.')[0] in MODULE_ROOTS
                       and re.fullmatch(r'[A-Za-z0-9_.]{1,100}', module))
            locations.append({'module': module if trusted else 'external',
                              'function': name if trusted and isinstance(name, str)
                              and re.fullmatch(r'[A-Za-z0-9_<>]{1,80}', name) else 'other',
                              'line': frame['line']})
        result.append({'thread_id': stack['thread_id'], 'frames': locations})
    return result


def capture_stacks():
    result = []
    for ident, frame in list(sys._current_frames().items())[:32]:
        if ident == threading.get_ident():
            continue
        locations = []
        for _ in range(24):
            if frame is None:
                break
            module = frame.f_globals.get('__name__', '')
            trusted = (isinstance(module, str) and module.split('.')[0] in MODULE_ROOTS
                       and re.fullmatch(r'[A-Za-z0-9_.]{1,100}', module))
            name = frame.f_code.co_name
            locations.append({'module': module if trusted else 'external',
                              'function': name if trusted and re.fullmatch(r'[A-Za-z0-9_<>]{1,80}', name) else 'other',
                              'line': frame.f_lineno})
            frame = frame.f_back
        result.append({'thread_id': ident, 'frames': locations})
    return result


def exception_stacks(exc):
    frames = []
    tb = exc.__traceback__
    while tb is not None and len(frames) < 24:
        frames.append({'module': tb.tb_frame.f_globals.get('__name__'),
                       'function': tb.tb_frame.f_code.co_name, 'line': tb.tb_lineno})
        tb = tb.tb_next
    return sanitize_stacks([{'thread_id': threading.get_ident(), 'frames': frames}])


@contextmanager
def operation_stacks(seconds, report):
    """One snapshot during an active operation, cancelled BEFORE its reply."""
    if seconds is None:
        yield
        return
    lock = threading.Lock()
    active = True

    def snapshot():
        with lock:
            if active:
                try:
                    report(capture_stacks())
                except Exception:
                    pass  # Diagnostics cannot determine the operation outcome.

    timer = threading.Timer(seconds, snapshot)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        with lock:
            active = False
            timer.cancel()
        timer.join(timeout=.1)
