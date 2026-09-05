"""IntradayBarRequest / IntradayTickRequest - granular price history within a
single day (or a short window), down to tick level.
"""
from __future__ import annotations

import datetime
from typing import Iterable, List, Optional

from .session import BLPSession
from .util import element_to_dict


def intraday_bars(
    session: BLPSession,
    security: str,
    event_type: str,
    interval: int,
    start_datetime: datetime.datetime,
    end_datetime: datetime.datetime,
) -> List[dict]:
    """`event_type` is one of TRADE, BID, ASK, BID_BEST, ASK_BEST, BEST_BID,
    BEST_ASK, ... `interval` is bar width in minutes.

    Returns a list of {"time": ..., "open": ..., "high": ..., "low": ...,
    "close": ..., "volume": ..., "numEvents": ...} bars.
    """
    service = session.service("//blp/refdata")
    request = service.createRequest("IntradayBarRequest")
    request.set("security", security)
    request.set("eventType", event_type)
    request.set("interval", interval)
    request.set("startDateTime", start_datetime)
    request.set("endDateTime", end_datetime)

    messages = session.send_and_collect(request)

    bars: List[dict] = []
    for msg in messages:
        if not msg.hasElement("barData"):
            continue
        bar_tick_data = msg.getElement("barData").getElement("barTickData")
        for i in range(bar_tick_data.numValues()):
            bars.append(element_to_dict(bar_tick_data.getValueAsElement(i)))
    return bars


def intraday_ticks(
    session: BLPSession,
    security: str,
    event_types: Iterable[str],
    start_datetime: datetime.datetime,
    end_datetime: datetime.datetime,
    include_condition_codes: bool = False,
) -> List[dict]:
    """`event_types` e.g. ["TRADE", "BID", "ASK"].

    Returns a list of {"time": ..., "type": ..., "value": ..., "size": ...} ticks.
    """
    service = session.service("//blp/refdata")
    request = service.createRequest("IntradayTickRequest")
    request.set("security", security)
    event_types_element = request.getElement("eventTypes")
    for event_type in event_types:
        event_types_element.appendValue(event_type)
    request.set("startDateTime", start_datetime)
    request.set("endDateTime", end_datetime)
    request.set("includeConditionCodes", include_condition_codes)

    messages = session.send_and_collect(request)

    ticks: List[dict] = []
    for msg in messages:
        if not msg.hasElement("tickData"):
            continue
        tick_data = msg.getElement("tickData").getElement("tickData")
        for i in range(tick_data.numValues()):
            ticks.append(element_to_dict(tick_data.getValueAsElement(i)))
    return ticks
