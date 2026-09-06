"""HistoricalDataRequest - time series over a date range. Equivalent to
Excel's =BDH().
"""
from __future__ import annotations

from typing import Iterable, Optional

from .session import BLPSession
from .util import check_response_error, element_to_dict


def historical_data(
    session: BLPSession,
    securities: Iterable[str],
    fields: Iterable[str],
    start_date: str,
    end_date: str,
    periodicity: str = "DAILY",
    currency: Optional[str] = None,
) -> dict:
    """`start_date`/`end_date` are "YYYYMMDD" strings. `periodicity` is one of
    DAILY, WEEKLY, MONTHLY, QUARTERLY, SEMI_ANNUALLY, YEARLY.

    Returns {security: [{"date": ..., field: value, ...}, ...]}.
    """
    service = session.service("//blp/refdata")
    request = service.createRequest("HistoricalDataRequest")

    securities_element = request.getElement("securities")
    for security in securities:
        securities_element.appendValue(security)

    fields_element = request.getElement("fields")
    for field in fields:
        fields_element.appendValue(field)

    request.set("periodicitySelection", periodicity)
    request.set("startDate", start_date)
    request.set("endDate", end_date)
    if currency:
        request.set("currency", currency)

    messages = session.send_and_collect(request)

    result: dict = {}
    for msg in messages:
        check_response_error(msg)
        if not msg.hasElement("securityData"):
            continue
        sec_data = msg.getElement("securityData")
        security = sec_data.getElementAsString("security")
        rows = []
        field_data_array = sec_data.getElement("fieldData")
        for i in range(field_data_array.numValues()):
            rows.append(element_to_dict(field_data_array.getValueAsElement(i)))
        result.setdefault(security, []).extend(rows)

    return result
