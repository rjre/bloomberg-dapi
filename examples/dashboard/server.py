"""Live global-macro dashboard, backed directly by a Bloomberg Terminal
running on this machine (Desktop API / blpapi) - no Web API entitlement
required.

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

from bdapi import BLPWorker, historical_data, reference_data  # noqa: E402

from universe import ALL_TICKERS, CATEGORY_OF, FIELDS, LABELS, RATES_CATEGORY, UNIVERSE, build_pulse  # noqa: E402

REFRESH_SECONDS = 3
HISTORY_DAYS = 30

app = FastAPI(title="Global Macro Morning Brief")

_state_lock = threading.Lock()
_state = {
    "snapshot": {cat: [] for cat in UNIVERSE},
    "as_of": None,
    "pulse": "Waiting for the first data refresh...",
    "connected": False,
    "history": {},  # ticker -> [{"date": "YYYY-MM-DD", "close": float}, ...]
    "movers": [],
}

worker: Optional[BLPWorker] = None


def _row_from_field_data(ticker: str, field_data: dict) -> dict:
    last = field_data.get("PX_LAST")
    chg_pct = field_data.get("CHG_PCT_1D")
    chg_net = field_data.get("CHG_NET_1D")
    return {
        "ticker": ticker,
        "label": LABELS[ticker],
        "last": last,
        "chgPct": chg_pct,
        "chgNet": chg_net,
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


def _refresh_loop() -> None:
    while True:
        try:
            _refresh_snapshot()
        except Exception as exc:  # noqa: BLE001
            with _state_lock:
                _state["connected"] = False
            print(f"[refresh] error: {exc}", file=sys.stderr)
        time.sleep(REFRESH_SECONDS)


def _load_history() -> None:
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
        _state["history"] = history


@app.on_event("startup")
def startup() -> None:
    global worker
    worker = BLPWorker()
    worker.start()
    _load_history()
    _refresh_snapshot()
    threading.Thread(target=_refresh_loop, daemon=True).start()


@app.get("/api/snapshot")
def get_snapshot() -> JSONResponse:
    with _state_lock:
        return JSONResponse(
            {
                "asOf": _state["as_of"],
                "connected": _state["connected"],
                "categories": _state["snapshot"],
                "pulse": _state["pulse"],
                "movers": _state["movers"],
            }
        )


@app.get("/api/history")
def get_history() -> JSONResponse:
    with _state_lock:
        return JSONResponse(_state["history"])


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8008, log_level="info")
