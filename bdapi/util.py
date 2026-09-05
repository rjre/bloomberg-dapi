"""Shared helpers for pulling plain Python values out of blpapi elements."""
from __future__ import annotations

import blpapi


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
