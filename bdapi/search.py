"""Security/instrument lookup via //blp/instruments."""
from __future__ import annotations

from typing import List, Optional

from .session import BLPSession
from .util import element_to_dict


def search_securities(
    session: BLPSession,
    query: str,
    max_results: int = 10,
    yellow_key_filter: Optional[str] = None,
) -> List[dict]:
    """Returns a list of {"security": ..., "description": ...} matches."""
    service = session.service("//blp/instruments")
    request = service.createRequest("instrumentListRequest")
    request.set("query", query)
    request.set("maxResults", max_results)
    if yellow_key_filter:
        request.set("yellowKeyFilter", yellow_key_filter)

    messages = session.send_and_collect(request)

    results: List[dict] = []
    for msg in messages:
        if not msg.hasElement("results"):
            continue
        results_array = msg.getElement("results")
        for i in range(results_array.numValues()):
            results.append(element_to_dict(results_array.getValueAsElement(i)))
    return results
