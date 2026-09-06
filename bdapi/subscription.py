"""Real-time streaming market data via //blp/mktdata subscriptions.

Unlike the request/response helpers elsewhere in this package, subscriptions
use blpapi's own event loop directly (session.raw), since data keeps
arriving indefinitely rather than completing with a single RESPONSE.

This is the mechanism Bloomberg's own Excel Add-in uses for live-linked
cells: subscribe once, then Bloomberg pushes updates for as long as the
subscription stays open - there is no repeated "pull" per update, which is
why an Excel sheet with hundreds of live securities can sit open all day
with no issue. `reference_data()`/`historical_data()` are a fundamentally
different, metered mechanism (see bdapi/util.py, `BloombergResponseError`,
and README.md "Rate limits") - don't poll them in a loop to build a "live"
view; subscribe instead.
"""
from __future__ import annotations

from typing import Dict, Iterable, Iterator, Tuple

import blpapi

from .session import BLPSession
from .util import to_python


class MarketDataSubscriber:
    def __init__(self, session: BLPSession):
        self._session = session
        self._correlation_to_security: Dict[int, str] = {}

    def subscribe(self, securities_and_fields: Iterable[Tuple[str, Iterable[str]]]) -> None:
        """`securities_and_fields` is an iterable of (security, fields), e.g.
        [("IBM US Equity", ["LAST_PRICE", "BID", "ASK"])].

        This only enqueues the subscribe request - success/failure per
        security arrives asynchronously as SUBSCRIPTION_STATUS events, which
        `listen()` surfaces per-security (see below) rather than failing the
        whole batch."""
        subscriptions = blpapi.SubscriptionList()
        for i, (security, fields) in enumerate(securities_and_fields):
            correlation_id = blpapi.CorrelationId(i)
            self._correlation_to_security[i] = security
            subscriptions.add(security, ",".join(fields), "", correlation_id)
        self._session.raw.subscribe(subscriptions)

    def listen(self, timeout_ms: int = 1000) -> Iterator[dict]:
        """Yields one of:
          - {"security": ..., "field": ..., "value": ...} - a live update
          - {"security": ..., "error": "..."} - THIS security's subscription
            failed (e.g. DAILY_CAPACITY_REACHED, an entitlement issue, an
            invalid ticker). A per-security failure does not stop the
            generator - other securities in the same batch keep streaming.
            (An earlier version raised on the first failure, which killed
            the entire batch's stream over one bad ticker - wrong for a
            27-security dashboard subscription.)
          - {"heartbeat": True} - no event arrived within `timeout_ms`. Once
            every subscription in a batch has failed, blpapi has nothing
            left to deliver, and the loop below would otherwise spin on
            TIMEOUT internally forever without ever yielding - starving any
            caller that wants to make time-based decisions (e.g. "give up
            and reconnect after 30 minutes of nothing but errors") of the
            chance to ever run that check. Yielding a heartbeat every
            `timeout_ms` guarantees the caller regains control periodically
            regardless of whether real data is flowing.

        Call this in a loop, e.g. `for tick in subscriber.listen(): ...`.
        """
        while True:
            event = self._session.raw.nextEvent(timeout_ms)
            event_type = event.eventType()

            if event_type == blpapi.Event.TIMEOUT:
                yield {"heartbeat": True}
                continue

            if event_type == blpapi.Event.SUBSCRIPTION_STATUS:
                for msg in event:
                    if msg.messageType() != blpapi.Names.SUBSCRIPTION_FAILURE:
                        continue
                    correlation_id = msg.correlationId().value()
                    security = self._correlation_to_security.get(correlation_id, "<unknown>")
                    reason = msg.getElement("reason")
                    category = reason.getElementAsString("category") if reason.hasElement("category") else "UNKNOWN"
                    subcategory = reason.getElementAsString("subcategory") if reason.hasElement("subcategory") else None
                    description = reason.getElementAsString("description") if reason.hasElement("description") else "no description"
                    yield {"security": security, "error": f"{subcategory or category}: {description}"}
                continue

            if event_type != blpapi.Event.SUBSCRIPTION_DATA:
                continue

            for msg in event:
                correlation_id = msg.correlationId().value()
                security = self._correlation_to_security.get(correlation_id, "<unknown>")
                for i in range(msg.numElements()):
                    el = msg.getElement(i)
                    if el.isNull():
                        continue
                    yield {"security": security, "field": str(el.name()), "value": to_python(el.getValue())}
