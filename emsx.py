"""Historical trade fills via EMSX (Bloomberg's Execution Management
System), //blp/emsx.history.

This Terminal's entitlement opens the service (`session.service` succeeds)
but every actual `GetFills` request - tried with no Scope, `Scope.Uuids`,
`Scope.TradingSystem`, and `Scope.Team`, across several date windows -
comes back `ERROR_INTERNAL`/`ERROR_PERMISSION` ("System Error. Please
contact Bloomberg Trade Desk" / "Failed to fetch data for this request.").
That is Bloomberg's own backend declining every request shape tried, not a
malformed-request bug on this end - the structural fix (below) is confirmed
correct against the service's own schema (`service.createRequest("GetFills")`
+ `FromDateTime`/`ToDateTime`/`Scope`, exactly as the schema describes), and
this Terminal has no EMSX order/route data behind it at all - `//blp/
emsx.emsx_all` and `//blp/emsx.emsx_beta` (the live order/route blotter
services) fail to open outright, alongside GetFills's failures - together
pointing at an EMSX account not provisioned for trading data on this seat,
not a code defect. This module is built to the real, documented request
shape so it should work unmodified once that's resolved; callers must
surface `GetFillsError` to the user rather than hide it - see webapp wiring
in server.py, which shows Bloomberg's own error text rather than pretending
there's no data.

No live order/route blotter is available at all on this entitlement (see
above) - only this one historical-fills lookup.
"""

from __future__ import annotations

import datetime

from bdapi.util import element_to_dict, check_response_error


class GetFillsError(RuntimeError):
    """Bloomberg's own ErrorResponse from a GetFills call - see module
    docstring. `error_code` is Bloomberg's own enum value (e.g.
    "ERROR_INTERNAL", "ERROR_PERMISSION")."""

    def __init__(self, error_code: str, message: str):
        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code


FILL_FIELDS = [
    "Ticker", "SecurityName", "Side", "Type", "OrderId", "RouteId", "FillId",
    "FillPrice", "FillShares", "Currency", "Broker", "ExecutingBroker", "Account",
    "DateTimeOfFill", "SettlementDate", "AssetClass", "TraderName",
    "OrderReferenceId", "LimitPrice", "RouteNetMoney", "UserNetMoney",
]


def get_fills(session, from_dt: datetime.datetime, to_dt: datetime.datetime):
    """All EMSX fills between `from_dt` and `to_dt` (both timezone-aware
    UTC). Raises GetFillsError on a Bloomberg-side ErrorResponse - see
    module docstring; this is expected to raise on this Terminal today."""
    service = session.service("//blp/emsx.history")
    request = service.createRequest("GetFills")
    request.set("FromDateTime", from_dt)
    request.set("ToDateTime", to_dt)

    messages = session.send_and_collect(request)
    fills = []
    for msg in messages:
        check_response_error(msg)
        if str(msg.messageType()) == "ErrorResponse":
            code = msg.getElementAsString("ErrorCode") if msg.hasElement("ErrorCode") else "UNKNOWN"
            text = msg.getElementAsString("ErrorMsg") if msg.hasElement("ErrorMsg") else "no message"
            raise GetFillsError(code, text)
        if str(msg.messageType()) != "GetFillsResponse":
            continue
        response = msg
        if not response.hasElement("Fills"):
            continue
        fills_array = response.getElement("Fills")
        for i in range(fills_array.numValues()):
            fills.append(element_to_dict(fills_array.getValueAsElement(i)))
    return fills
