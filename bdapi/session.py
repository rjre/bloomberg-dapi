"""Thin wrapper around a blpapi.Session talking to a locally running Bloomberg
Terminal (the Desktop API / DAPI). No Web API entitlement needed - this only
requires the Terminal to be installed, running, and logged in on this
machine, listening on localhost:8194 (blpapi's default).
"""
from __future__ import annotations

from typing import List, Optional

import blpapi


class BLPSession:
    def __init__(self, host: str = "localhost", port: int = 8194):
        options = blpapi.SessionOptions()
        options.setServerHost(host)
        options.setServerPort(port)
        # Without this, msg.timeReceived() raises ValueError("Message has no
        # timestamp") on every message - needed for MarketDataSubscriber to
        # report a per-security last-update time (see subscription.py).
        options.setRecordSubscriptionDataReceiveTimes(True)
        self._session = blpapi.Session(options)
        self._opened_services: set = set()

    def __enter__(self) -> "BLPSession":
        if not self._session.start():
            raise ConnectionError(
                "Failed to start a Bloomberg Desktop API session. Make sure "
                "Bloomberg Terminal is running and logged in on this machine."
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._session.stop()

    def service(self, name: str) -> blpapi.Service:
        if name not in self._opened_services:
            if not self._session.openService(name):
                raise ConnectionError(f"Failed to open Bloomberg service {name}")
            self._opened_services.add(name)
        return self._session.getService(name)

    def send_and_collect(self, request: blpapi.Request, timeout_ms: int = 10_000) -> List[blpapi.Message]:
        """Send a request and collect every message up to and including the
        final RESPONSE event (accumulating any PARTIAL_RESPONSE events first)."""
        self._session.sendRequest(request)
        messages: List[blpapi.Message] = []
        while True:
            event = self._session.nextEvent(timeout_ms)
            event_type = event.eventType()
            for msg in event:
                messages.append(msg)
            if event_type == blpapi.Event.RESPONSE:
                break
            if event_type == blpapi.Event.TIMEOUT:
                raise TimeoutError("Bloomberg request timed out waiting for a response")
        return messages

    @property
    def raw(self) -> blpapi.Session:
        """Escape hatch to the underlying blpapi.Session for anything not
        wrapped here (e.g. subscriptions - see bdapi/subscription.py)."""
        return self._session
