"""Shared helpers for pulling plain Python values out of blpapi elements."""
from __future__ import annotations

import blpapi


class BloombergResponseError(RuntimeError):
    """Raised when Bloomberg returns a top-level `responseError` instead of
    the expected data - e.g. DAILY_CAPACITY_REACHED, entitlement issues,
    service unavailability. Previously these were silently swallowed (a
    message with `responseError` has no `securityData`, so it was just
    skipped), which looked exactly like "no data" and took real live
    debugging to track down. Fail loud instead."""


def check_response_error(msg) -> None:
    if not msg.hasElement("responseError"):
        return
    err = msg.getElement("responseError")
    category = err.getElementAsString("category") if err.hasElement("category") else "UNKNOWN"
    subcategory = err.getElementAsString("subcategory") if err.hasElement("subcategory") else None
    message = err.getElementAsString("message") if err.hasElement("message") else "no message"
    raise BloombergResponseError(f"{subcategory or category}: {message}")


def to_python(value):
    """blpapi returns its own `Name` type for enumeration/name-typed fields
    (e.g. the tick "type" field) instead of a plain str - this breaks JSON
    serialization and str equality checks downstream if left as-is."""
    if isinstance(value, blpapi.Name):
        return str(value)
    return value


def element_to_dict(element) -> dict:
    """Flattens one level of a blpapi sequence Element into {name: value}."""
    result = {}
    for i in range(element.numElements()):
        el = element.getElement(i)
        result[str(el.name())] = to_python(el.getValue())
    return result
