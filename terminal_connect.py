""""Terminal Connect": push a security into the actual Bloomberg Terminal
via Bloomberg's own supported Terminal Connect product (a local GraphQL API,
"GAPI") - not UI automation, and not blpapi (the Desktop API this whole
project otherwise runs on is a pure data channel with no method to control
the Terminal's own UI at all).

Confirmed live on this machine (2026-09-10):
  - The local GAPI server IS running, at http://localhost:39393/terminal_connect/v3/
    (the first port in Bloomberg's documented 39393-39397 scan range) -
    trivial/introspection queries succeed with no auth at all, so Terminal
    Connect itself is already enabled for this Bloomberg user.
  - An actual `runFunctionInTab` mutation correctly comes back
    `{"errorCategory": "AUTHORIZATION", "errorMessage": "Missing or invalid
    api key."}` - i.e. the one missing piece is an API key for *this*
    application specifically. Per Bloomberg's docs (developer.bloomberg.com,
    Terminal Connect > Set Up API Access): "Contact your Bloomberg
    representative or Digital Strategy representative to request TC for
    your application. Once registered, Bloomberg will issue an API key."
    That's a registration step on Bloomberg's side, not a code problem -
    set the key as the TERMINAL_CONNECT_API_KEY environment variable once
    you have one and this module needs no other changes.

This replaces an earlier version of this module that drove the Terminal via
Windows UI automation (simulated keystrokes) - a reasonable fallback when a
supported API wasn't known to be available, but strictly worse than a real,
structured, Bloomberg-supported call now that one's confirmed working.
Real API in, UI automation out.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# Bloomberg's documented port-scan range for the local TC server (it starts
# on the first free one) - confirmed live as 39393 on this machine.
CANDIDATE_PORTS = [39393, 39394, 39395, 39396, 39397]

API_KEY_ENV_VAR = "TERMINAL_CONNECT_API_KEY"

_RUN_FUNCTION_QUERY = """
mutation($mnemonic: String!, $tabName: String!, $security1: String) {
  runFunctionInTab(input: { mnemonic: $mnemonic, tabName: $tabName, security1: $security1 }) {
    ... on Result { succeeded details }
    ... on Error { errorCategory errorMessage }
  }
}
"""


def _post(url: str, payload: dict, headers: dict, timeout: float):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def find_server_url(timeout: float = 1.0):
    """The first candidate-port TC endpoint that actually answers, or None
    if none do (Terminal not logged in, or this user isn't TC-enabled) -
    a plain schema query needs no API key, so this alone confirms the
    server is up without needing one."""
    for port in CANDIDATE_PORTS:
        url = f"http://localhost:{port}/terminal_connect/v3/"
        try:
            result = _post(url, {"query": "{ __typename }"}, {"Content-Type": "application/json"}, timeout)
            if result.get("data", {}).get("__typename"):
                return url
        except Exception:  # noqa: BLE001
            continue
    return None


def open_security(ticker: str, mnemonic: str = "DES", tab_name: str = "1") -> dict:
    """Runs `mnemonic` (default "DES", security description) in Terminal
    tab `tab_name` with `ticker` loaded as security1 - the Terminal Connect
    equivalent of typing a security into the Terminal and pressing <GO>.

    Returns {"ok": True, "details": ...} or {"ok": False, "error": "..."} -
    never raises for an expected failure (server not reachable, no API key,
    a Bloomberg-side error), so a caller can show the message directly
    rather than a stack trace."""
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        return {"ok": False, "error": f"No Terminal Connect API key set ({API_KEY_ENV_VAR} env var) - "
                                       "see terminal_connect.py's module docstring for how to get one."}

    url = find_server_url()
    if url is None:
        return {"ok": False, "error": f"Terminal Connect isn't responding on any of {CANDIDATE_PORTS} - "
                                       "is the Bloomberg Terminal logged in on this machine?"}

    variables = {"mnemonic": mnemonic, "tabName": tab_name, "security1": ticker}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    try:
        body = _post(url, {"query": _RUN_FUNCTION_QUERY, "variables": variables}, headers, timeout=5.0)
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc)}

    if body.get("errors"):
        return {"ok": False, "error": "; ".join(e.get("message", str(e)) for e in body["errors"])}
    result = (body.get("data") or {}).get("runFunctionInTab") or {}
    if result.get("errorCategory"):
        return {"ok": False, "error": f"[{result['errorCategory']}] {result.get('errorMessage')}"}
    return {"ok": bool(result.get("succeeded")), "details": result.get("details"), "tab": tab_name}
