r"""Global Macro Morning Brief - standalone desktop app.

Single process, single native window. No HTTP server, no open port, no
browser tab - `pywebview` embeds the OS's own WebView (WebView2 on Windows)
in a native window, and the page talks to Python directly through
`window.pywebview.api` instead of fetch() calls to a local server. This is
the same UI as examples/dashboard/, restructured for teams whose IT policy
doesn't want a locally-listening web server, even a loopback-only one.

Run with:
    python main.py

Package as a single .exe - verified working 2026-09-06 (PyInstaller 6.22.2,
Python 3.13.14, blpapi 3.24.11). blpapi loads its `ffiutils` helper via
`glob.glob()` next to internals.py at runtime (see blpapi/internals.py,
`_loadLibrary()`) instead of a normal `import`, so PyInstaller's static
analysis can't discover it - `--collect-all blpapi` alone misses it, and the
app fails at startup with `cannot access local variable 'toPy'`. Point
--add-binary at it explicitly (adjust the site-packages path for your Python
install - find yours with `python -c "import blpapi,os;
print(os.path.dirname(blpapi.__file__))"`):

    pyinstaller --onedir --windowed --name "GlobalMacroBrief" ^
        --collect-all blpapi ^
        --add-binary "<path-to-site-packages>\blpapi\ffiutils.cp311-win_amd64.pyd;blpapi" ^
        --add-data "static;static" ^
        --add-data "..\universe.py;." ^
        --add-data "..\bdapi;bdapi" ^
        main.py

`--onedir` (not `--onefile`) was used for the verified build - `--onefile`
should work too in principle (same fix applies) but wasn't the one actually
tested end-to-end. The result is dist/GlobalMacroBrief/GlobalMacroBrief.exe -
a real double-click app, no Python install needed on the target machine.
"""
from __future__ import annotations

import datetime
import sys
import threading
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # for `import bdapi`, `import universe`

import webview  # noqa: E402

from bdapi import BLPWorker, BloombergResponseError, historical_data, reference_data  # noqa: E402
from universe import ALL_TICKERS, FIELDS, UNIVERSE, LABELS, CATEGORY_OF, RATES_CATEGORY, build_pulse  # noqa: E402

# Bloomberg meters daily data capacity on the Desktop API - an earlier version
# of this app polled every 3s and exhausted a full day's quota overnight
# (confirmed via a `DAILY_CAPACITY_REACHED` responseError). A macro dashboard
# doesn't need sub-minute granularity anyway, so poll far less aggressively.
REFRESH_SECONDS = 60
# If the daily quota IS hit, retrying every REFRESH_SECONDS is pointless until
# it resets - back off hard instead of continuing to hammer the API.
CAPACITY_BACKOFF_SECONDS = 1800
HISTORY_DAYS = 30

_state_lock = threading.Lock()
_state = {
    "snapshot": {cat: [] for cat in UNIVERSE},
    "as_of": None,
    "pulse": "Waiting for the first data refresh...",
    "connected": False,
    "history": {},
    "movers": [],
}

worker: Optional[BLPWorker] = None


def _row_from_field_data(ticker: str, field_data: dict) -> dict:
    return {
        "ticker": ticker,
        "label": LABELS[ticker],
        "last": field_data.get("PX_LAST"),
        "chgPct": field_data.get("CHG_PCT_1D"),
        "chgNet": field_data.get("CHG_NET_1D"),
        "high": field_data.get("PX_HIGH"),
        "low": field_data.get("PX_LOW"),
        "isRate": CATEGORY_OF[ticker] == RATES_CATEGORY,
    }


def _refresh_snapshot() -> None:
    result = worker.submit(reference_data, ALL_TICKERS, FIELDS).result(timeout=15)
    data = result["data"]

    snapshot: dict = {cat: [] for cat in UNIVERSE}
    flat_rows = []
    for cat, items in UNIVERSE.items():
        for ticker, _label in items:
            field_data = data.get(ticker)
            if field_data is None:
                continue
            row = _row_from_field_data(ticker, field_data)
            snapshot[cat].append(row)
            flat_rows.append(row)

    movers = sorted(
        (r for r in flat_rows if r["chgPct"] is not None and not r["isRate"]),
        key=lambda r: abs(r["chgPct"]),
        reverse=True,
    )[:8]

    pulse = build_pulse(snapshot)

    with _state_lock:
        _state["snapshot"] = snapshot
        _state["as_of"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _state["pulse"] = pulse
        _state["connected"] = True
        _state["movers"] = movers


def _safe_refresh_once() -> float:
    """Runs one refresh, handling errors so a bad Bloomberg response never
    crashes the app (this is called both at startup, before the window
    exists, and from the background loop - it must never raise). Returns
    how long to wait before the next attempt."""
    try:
        _refresh_snapshot()
    except BloombergResponseError as exc:
        with _state_lock:
            _state["connected"] = False
            _state["pulse"] = (
                f"Bloomberg data limit hit ({exc}) - retrying in "
                f"{CAPACITY_BACKOFF_SECONDS // 60} min. Last good data stays on screen."
            )
        print(f"[refresh] Bloomberg limit hit, backing off: {exc}", file=sys.stderr)
        return CAPACITY_BACKOFF_SECONDS
    except Exception as exc:  # noqa: BLE001
        with _state_lock:
            _state["connected"] = False
        print(f"[refresh] error: {exc}", file=sys.stderr)
        return REFRESH_SECONDS
    return REFRESH_SECONDS


def _refresh_loop() -> None:
    while True:
        wait = _safe_refresh_once()
        time.sleep(wait)


def _load_history() -> None:
    end = datetime.date.today()
    start = end - datetime.timedelta(days=HISTORY_DAYS * 2)
    history: dict = {}
    for ticker in ALL_TICKERS:
        try:
            result = worker.submit(
                historical_data, [ticker], ["PX_LAST"], start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
            ).result(timeout=15)
            rows = result.get(ticker, [])
            history[ticker] = [
                {"date": r["date"].isoformat(), "close": r["PX_LAST"]}
                for r in rows
                if "PX_LAST" in r
            ][-HISTORY_DAYS:]
        except Exception as exc:  # noqa: BLE001
            print(f"[history] {ticker} failed: {exc}", file=sys.stderr)
            history[ticker] = []
    with _state_lock:
        _state["history"] = history


class Api:
    """Exposed to the page as `window.pywebview.api.<method>()` - each call
    returns a JS Promise resolving to this method's (JSON-serializable)
    return value."""

    def get_snapshot(self) -> dict:
        with _state_lock:
            return {
                "asOf": _state["as_of"],
                "connected": _state["connected"],
                "categories": _state["snapshot"],
                "pulse": _state["pulse"],
                "movers": _state["movers"],
            }

    def get_history(self) -> dict:
        with _state_lock:
            return _state["history"]


def start_background() -> None:
    global worker
    worker = BLPWorker()
    worker.start()
    _load_history()
    _safe_refresh_once()
    threading.Thread(target=_refresh_loop, daemon=True).start()


if __name__ == "__main__":
    start_background()
    api = Api()
    window = webview.create_window(
        "Global Macro — Morning Brief",
        str(Path(__file__).parent / "static" / "index.html"),
        js_api=api,
        width=1440,
        height=920,
        background_color="#0a0c10",
    )
    webview.start()
