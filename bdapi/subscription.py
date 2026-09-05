"""Real-time streaming market data via //blp/mktdata subscriptions.

Unlike the request/response helpers elsewhere in this package, subscriptions
use blpapi's own event loop directly (session.raw), since data keeps
arriving indefinitely rather than completing with a single RESPONSE.
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
        [("IBM US Equity", ["LAST_PRICE", "BID", "ASK"])]."""
        subscriptions = blpapi.SubscriptionList()
        for i, (security, fields) in enumerate(securities_and_fields):
            correlation_id = blpapi.CorrelationId(i)
            self._correlation_to_security[i] = security
            subscriptions.add(security, ",".join(fields), "", correlation_id)
        self._session.raw.subscribe(subscriptions)

    def listen(self, timeout_ms: int = 1000) -> Iterator[dict]:
        """Yields {"security": ..., "field": ..., "value": ...} as updates
        arrive. Blocks (per `timeout_ms`) between checks - call this in a
        loop, e.g. `for tick in subscriber.listen(): ...`."""
        while True:
            event = self._session.raw.nextEvent(timeout_ms)
            if event.eventType() != blpapi.Event.SUBSCRIPTION_DATA:
                continue
            for msg in event:
                correlation_id = msg.correlationId().value()
                security = self._correlation_to_security.get(correlation_id, "<unknown>")
                for i in range(msg.numElements()):
                    el = msg.getElement(i)
                    if el.isNull():
                        continue
                    yield {"security": security, "field": str(el.name()), "value": to_python(el.getValue())}
