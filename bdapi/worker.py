"""A single dedicated worker thread owning one BLPSession, serialising every
call through a job queue.

blpapi.Session.nextEvent() returns the next event for the WHOLE session, not
scoped to one in-flight request - if two threads both call sendRequest/
nextEvent concurrently, one thread's response can be consumed by the other's
loop. Any app with more than one caller (e.g. a web server handling requests
while also polling in the background) needs exactly one thread doing all the
session I/O. This is that thread; everyone else calls `submit()` and gets a
concurrent.futures.Future back.
"""
from __future__ import annotations

import concurrent.futures
import queue
import threading
from typing import Callable

from .session import BLPSession


class BLPWorker:
    def __init__(self, host: str = "localhost", port: int = 8194):
        self._host = host
        self._port = port
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = threading.Event()
        self._start_error: Exception | None = None

    def start(self) -> None:
        self._thread.start()
        self._started.wait(timeout=15)
        if self._start_error:
            raise self._start_error

    def submit(self, fn: Callable, *args, **kwargs) -> concurrent.futures.Future:
        """`fn(session, *args, **kwargs)` runs on the worker thread; `session`
        is the shared BLPSession. Returns a Future - call `.result()` to block
        for the answer, or attach a callback."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((fn, args, kwargs, future))
        return future

    def _run(self) -> None:
        try:
            session = BLPSession(self._host, self._port)
            session.__enter__()
        except Exception as exc:  # noqa: BLE001
            self._start_error = exc
            self._started.set()
            return
        self._started.set()
        try:
            while True:
                fn, args, kwargs, future = self._jobs.get()
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = fn(session, *args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    future.set_exception(exc)
                else:
                    future.set_result(result)
        finally:
            session.__exit__(None, None, None)
