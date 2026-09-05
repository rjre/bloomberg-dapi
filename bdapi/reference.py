"""ReferenceDataRequest - current/static field values for securities.
Equivalent to Excel's =BDP().
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional

from .session import BLPSession
from .util import element_to_dict


def reference_data(
    session: BLPSession,
    securities: Iterable[str],
    fields: Iterable[str],
    overrides: Optional[Dict[str, str]] = None,
) -> dict:
    """Returns {"data": {security: {field: value, ...}}, "errors": {security: message}}.

    A security-level error (e.g. an invalid ticker) lands in `errors`. A
    field-level issue for an otherwise-valid security (e.g. a field that
    doesn't apply to that instrument) is attached under the special key
    "_field_errors" inside that security's data dict.
    """
    service = session.service("//blp/refdata")
    request = service.createRequest("ReferenceDataRequest")

    securities_element = request.getElement("securities")
    for security in securities:
        securities_element.appendValue(security)

    fields_element = request.getElement("fields")
    for field in fields:
        fields_element.appendValue(field)

    if overrides:
        overrides_element = request.getElement("overrides")
        for field_id, value in overrides.items():
            override = overrides_element.appendElement()
            override.setElement("fieldId", field_id)
            override.setElement("value", value)

    messages = session.send_and_collect(request)

    data: dict = {}
    errors: dict = {}
    for msg in messages:
        if not msg.hasElement("securityData"):
            continue
        security_data_array = msg.getElement("securityData")
        for i in range(security_data_array.numValues()):
            sec_data = security_data_array.getValueAsElement(i)
            security = sec_data.getElementAsString("security")

            if sec_data.hasElement("securityError"):
                errors[security] = sec_data.getElement("securityError").getElementAsString("message")
                continue

            values: dict = element_to_dict(sec_data.getElement("fieldData"))

            if sec_data.hasElement("fieldExceptions"):
                field_errors: dict = {}
                fe_array = sec_data.getElement("fieldExceptions")
                for k in range(fe_array.numValues()):
                    fe = fe_array.getValueAsElement(k)
                    field_id = fe.getElementAsString("fieldId")
                    field_errors[field_id] = fe.getElement("errorInfo").getElementAsString("message")
                if field_errors:
                    values["_field_errors"] = field_errors

            data[security] = values

    return {"data": data, "errors": errors}
