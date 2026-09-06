"""Live global-macro dashboard, backed directly by a Bloomberg Terminal
running on this machine (Desktop API / blpapi) - no Web API entitlement
required.

Live prices come from a `//blp/mktdata` SUBSCRIPTION (bdapi.MarketDataSubscriber),
not repeated `reference_data()` pulls - subscribe once, then Bloomberg pushes
updates for as long as the connection stays open, matching how Bloomberg's own
Excel Add-in keeps hundreds of live-linked cells open all day with no issue.
An earlier version of this app polled `reference_data()` on a timer instead,
which is a metered "pull" mechanism (see README.md "Rate limits") - that
exhausted a full day's Bloomberg data capacity overnight. `historical_data()`
is still used, but only once at startup, to seed the sparklines.

Run with:
    python server.py
then open http://localhost:8008
"""
from __future__ import annotations

import datetime
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for `import bdapi`

from bdapi import BLPSession, BLPWorker, MarketDataSubscriber, historical_data  # noqa: E402

from universe import ALL_TICKERS, CATEGORY_OF, LABELS, RATES_CATEGORY, UNIVERSE, build_pulse  # noqa: E402

# Subscribed once at startup and left open - NOT re-requested on a timer.
SUBSCRIPTION_FIELDS = ["LAST_PRICE", "RT_PX_CHG_NET_1D", "RT_PX_CHG_PCT_1D", "HIGH", "LOW"]
HISTORY_DAYS = 30
# If some tickers are still failing (e.g. a capacity limit that hasn't reset
# yet) after this long, tear down and re-subscribe the whole batch - a failed
# subscription doesn't self-heal on its own once the underlying condition
# clears.
RESUBSCRIBE_AFTER_SECONDS = 1800

app = FastAPI(title="Global Macro Morning Brief")

_state_lock = threading.Lock()
_raw: dict = {ticker: {} for ticker in ALL_TICKERS}  # ticker -> {"LAST_PRICE": ..., ...}
_raw_errors: dict = {}  # ticker -> last error message, for tickers currently failing
_history: dict = {}

worker: Optional[BLPWorker] = None


def _row_from_raw(ticker: str) -> dict:
    field_data = _raw.get(ticker, {})
    last = field_data.get("LAST_PRICE")
    chg_net = field_data.get("RT_PX_CHG_NET_1D")
    chg_pct = field_data.get("RT_PX_CHG_PCT_1D")
    return {
        "ticker": ticker,
        "label": LABELS[ticker],
        "last": last,
        "chgPct": chg_pct,
        "chgNet": chg_net,
        "high": field_data.get("HIGH"),
        "low": field_data.get("LOW"),
        "isRate": CATEGORY_OF[ticker] == RATES_CATEGORY,
    }


def _build_snapshot() -> dict:
    """Pure computation over already-in-memory subscription data - no
    Bloomberg calls here, so this can be (and is) called on every HTTP
    request with no rate-limit concern at all."""
    snapshot: dict = {cat: [] for cat in UNIVERSE}
    flat_rows = []
    for cat, items in UNIVERSE.items():
        for ticker, _label in items:
            row = _row_from_raw(ticker)
            snapshot[cat].append(row)
            flat_rows.append(row)

    movers = sorted(
        (r for r in flat_rows if r["chgPct"] is not None and not r["isRate"]),
        key=lambda r: abs(r["chgPct"]),
        reverse=True,
    )[:8]

    connected = any(_raw.get(t) for t in ALL_TICKERS)
    pulse = build_pulse(snapshot)
    if _raw_errors and not connected:
        sample_ticker, sample_error = next(iter(_raw_errors.items()))
        pulse = (
            f"Bloomberg subscription failed for all {len(_raw_errors)} instruments "
            f"({sample_error}). Retrying automatically - last good data will appear "
            f"here once it recovers."
        )
    elif _raw_errors:
        pulse += f" ({len(_raw_errors)} of {len(ALL_TICKERS)} instruments not currently subscribed.)"

    return {"snapshot": snapshot, "movers": movers, "pulse": pulse, "connected": connected}


def _subscription_loop() -> None:
    """Owns its own dedicated BLPSession for the life of the app - a
    subscription needs one thread continuously calling nextEvent() on the
    same session that subscribed, and that must not be shared with
    BLPWorker's request/response session (see bdapi/session.py /
    bdapi/worker.py docstrings on why only one thread may drive a session).
    """
    backoff = 5
    while True:
        try:
            with BLPSession() as session:
                subscriber = MarketDataSubscriber(session)
                subscriber.subscribe([(ticker, SUBSCRIPTION_FIELDS) for ticker in ALL_TICKERS])
                cycle_start = time.time()
                for tick in subscriber.listen(timeout_ms=1000):
                    if "heartbeat" not in tick:
                        with _state_lock:
                            if "error" in tick:
                                _raw_errors[tick["security"]] = tick["error"]
                            else:
                                _raw.setdefault(tick["security"], {})[tick["field"]] = tick["value"]
                                _raw_errors.pop(tick["security"], None)
                    with _state_lock:
                        still_failing = bool(_raw_errors)
                    if still_failing and time.time() - cycle_start > RESUBSCRIBE_AFTER_SECONDS:
                        print("[subscription] some tickers still failing - reconnecting", file=sys.stderr)
                        break
            backoff = 5  # clean reconnect cycle - reset backoff
        except Exception as exc:  # noqa: BLE001
            print(f"[subscription] session error: {exc}", file=sys.stderr)
            backoff = min(backoff * 2, 300)
        time.sleep(backoff)


def _load_history() -> None:
    """One-time historical_data() pull at startup, for sparklines only -
    this is a legitimate single small pull (27 tickers x 1 field x 30 days,
    once), not a repeated one, so it doesn't meaningfully touch daily
    capacity the way polling reference_data() on a timer did."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=HISTORY_DAYS * 2)  # *2 to comfortably cover weekends/holidays
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
        _history.update(history)


@app.on_event("startup")
def startup() -> None:
    global worker
    worker = BLPWorker()
    worker.start()
    _load_history()
    threading.Thread(target=_subscription_loop, daemon=True).start()


@app.get("/api/snapshot")
def get_snapshot() -> JSONResponse:
    with _state_lock:
        built = _build_snapshot()
    return JSONResponse(
        {
            "asOf": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "connected": built["connected"],
            "categories": built["snapshot"],
            "pulse": built["pulse"],
            "movers": built["movers"],
        }
    )


@app.get("/api/history")
def get_history() -> JSONResponse:
    with _state_lock:
        return JSONResponse(dict(_history))


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8008, log_level="info")
