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

from bdapi import BLPSession, BLPWorker, MarketDataSubscriber, historical_data, reference_data  # noqa: E402

from universe import ALL_TICKERS, CATEGORY_OF, FX_CATEGORY, LABELS, RATES_CATEGORY, UNIVERSE, build_pulse  # noqa: E402
import fxoption  # noqa: E402
import terminal_connect  # noqa: E402
import spxoption  # noqa: E402

# Subscribed once at startup and left open - NOT re-requested on a timer.
SUBSCRIPTION_FIELDS = ["LAST_PRICE", "RT_PX_CHG_NET_1D", "RT_PX_CHG_PCT_1D", "HIGH", "LOW", "BID", "ASK"]
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
_last_update: dict = {}  # ticker -> datetime.datetime (UTC) of its most recent tick
_history: dict = {}

worker: Optional[BLPWorker] = None


def _row_from_raw(ticker: str) -> dict:
    field_data = _raw.get(ticker, {})
    last = field_data.get("LAST_PRICE")
    chg_net = field_data.get("RT_PX_CHG_NET_1D")
    chg_pct = field_data.get("RT_PX_CHG_PCT_1D")
    last_update = _last_update.get(ticker)
    return {
        "ticker": ticker,
        "label": LABELS[ticker],
        "last": last,
        "chgPct": chg_pct,
        "chgNet": chg_net,
        "high": field_data.get("HIGH"),
        "low": field_data.get("LOW"),
        "bid": field_data.get("BID"),
        "ask": field_data.get("ASK"),
        "isRate": CATEGORY_OF[ticker] == RATES_CATEGORY,
        "isFx": CATEGORY_OF[ticker] == FX_CATEGORY,
        "lastUpdate": last_update.isoformat() if last_update else None,
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
                                if tick.get("time") is not None:
                                    _last_update[tick["security"]] = tick["time"]
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


# --- FX option: realtime Garman-Kohlhagen valuation off Bloomberg's own OTC
# FX vol surface (ATM/RR/BF) - see fxoption.py's module docstring for the
# full pricing approach and its deliberate approximations.
#
# Unlike the main grid above, this does poll reference_data() on a timer
# rather than subscribe - deliberately: the vol/RR/BF/forward tags a smile
# needs aren't part of the always-on ALL_TICKERS subscription (there are
# 14 pairs x 13 tenors x 6 tags of them - far too many to subscribe to
# permanently for a tool only one user's one selected instrument ever reads
# at a time), and unlike raw spot they don't need sub-second updates - vol
# surfaces move over minutes, not ticks. This is bounded to whichever single
# instrument is currently selected (a handful of tags), refreshed only while
# the FX Option tab is open, which is a different rate-limit profile than
# the "don't poll reference_data in a loop" warning in README.md was written
# about (an unbounded, always-on, whole-universe poll).
FXOPT_FAST_REFRESH_S = 4    # bracket-tenor smile + spot - the actual pricing inputs
FXOPT_TERM_REFRESH_S = 25   # full ATM term structure - context only, moves slowly

_fxopt_lock = threading.Lock()
_fxopt_instrument = {"base": "EUR", "quote": "USD", "strike": None, "expiry": None,
                     "is_call": False, "premium_adjusted": False}
_fxopt_tag_values: dict = {}    # tag -> float (PX_LAST)
_fxopt_term_values: dict = {}   # tag -> float (PX_LAST) - ATM *and* full smile tags, every tenor
_fxopt_fwd_scale: dict = {}     # pair_key -> int, fetched once per pair, cached forever
_fxopt_as_of: Optional[datetime.datetime] = None
_fxopt_tag_error: Optional[str] = None
_fxopt_history_cache: dict = {}  # (base, quote, tenor) -> {"dates", "vols", "fetched_at"}
FXOPT_HISTORY_TTL_S = 6 * 3600
FXOPT_HISTORY_LOOKBACK_DAYS = 30


def _fxopt_ensure_fwd_scale(base: str, quote: str) -> None:
    """FWD_SCALE (decimal places a pair's forward-point ticker is quoted in)
    is static - fetched once per pair, ever, then cached for the life of the
    process."""
    key = fxoption.pair_key(base, quote)
    if key in _fxopt_fwd_scale:
        return
    try:
        tag = fxoption.fwd_tag(base, quote, "1M")
        result = worker.submit(reference_data, [tag], ["FWD_SCALE"]).result(timeout=10)
        scale = result["data"].get(tag, {}).get("FWD_SCALE")
        if scale is not None:
            _fxopt_fwd_scale[key] = int(scale)
    except Exception as exc:  # noqa: BLE001
        print(f"[fxoption] FWD_SCALE fetch failed for {key}: {exc}", file=sys.stderr)


def _fxopt_do_fast_refresh() -> None:
    global _fxopt_as_of, _fxopt_tag_error
    with _fxopt_lock:
        instrument = dict(_fxopt_instrument)
    if instrument["strike"] is None:
        return
    base, quote = instrument["base"], instrument["quote"]
    try:
        today = datetime.datetime.now(datetime.timezone.utc).date()
        expiry_date = datetime.date.fromisoformat(instrument["expiry"])
        bracket = fxoption.bracket_tenors(expiry_date, today)
        securities = [fxoption.spot_tag(base, quote)]
        for tenor, _date in bracket:
            securities += fxoption.tags_for_tenor(base, quote, tenor)
        # The 9 USD-legged G10 spots, always - lets get_fxoption_snapshot
        # triangulate a GBP premium for whatever pair is actually selected
        # (fxoption.gbp_per_unit) without a separate refresh cycle. Plus the
        # quote currency's overnight rate, if one's confirmed live for it
        # (fxoption.OVERNIGHT_RATE_TAG) - the discount-rate input pricing
        # would otherwise assume r=0 for (see fxoption.value_option).
        for usd_base, usd_quote in fxoption.USD_LEGGED_PAIRS:
            securities.append(fxoption.spot_tag(usd_base, usd_quote))
        rate_tag = fxoption.overnight_rate_tag(quote)
        if rate_tag:
            securities.append(rate_tag)
        securities = list(dict.fromkeys(securities))
        result = worker.submit(reference_data, securities, ["PX_LAST"]).result(timeout=10)
        values = {sec: d.get("PX_LAST") for sec, d in result["data"].items()}
        with _fxopt_lock:
            _fxopt_tag_values.update(values)
            _fxopt_as_of = datetime.datetime.now(datetime.timezone.utc)
            _fxopt_tag_error = f"{len(result['errors'])} tag(s) failed" if result["errors"] else None
    except Exception as exc:  # noqa: BLE001
        with _fxopt_lock:
            _fxopt_tag_error = str(exc)
        print(f"[fxoption] fast refresh failed: {exc}", file=sys.stderr)


def _fxopt_do_term_refresh() -> None:
    """Full smile tags (not just ATM) for *every* quoted tenor - powers both
    the ATM term-structure chart and the vol-surface panel (build_tenor_smile
    per tenor, see get_fxoption_snapshot). 13 tenors x 6 tags = 78 tickers in
    one batched call, on FXOPT_TERM_REFRESH_S - a vol surface moves over
    minutes, not ticks, and this is scoped to one open tab's one selected
    pair, so a 78-ticker pull every 25s stays well inside a sane rate-limit
    budget (see the section docstring above)."""
    with _fxopt_lock:
        instrument = dict(_fxopt_instrument)
    if instrument["strike"] is None:
        return
    base, quote = instrument["base"], instrument["quote"]
    try:
        securities: list = []
        for tenor in fxoption.TENORS:
            securities += fxoption.tags_for_tenor(base, quote, tenor)
        securities = list(dict.fromkeys(securities))
        result = worker.submit(reference_data, securities, ["PX_LAST"]).result(timeout=15)
        values = {sec: d.get("PX_LAST") for sec, d in result["data"].items()}
        with _fxopt_lock:
            _fxopt_term_values.clear()
            _fxopt_term_values.update(values)
    except Exception as exc:  # noqa: BLE001
        print(f"[fxoption] term-structure refresh failed: {exc}", file=sys.stderr)


def _fxopt_fast_refresh_loop() -> None:
    while True:
        time.sleep(FXOPT_FAST_REFRESH_S)
        _fxopt_do_fast_refresh()


def _fxopt_term_refresh_loop() -> None:
    while True:
        _fxopt_do_term_refresh()
        time.sleep(FXOPT_TERM_REFRESH_S)


@app.post("/api/fxoption/instrument")
def set_fxoption_instrument(payload: dict) -> JSONResponse:
    """Sets the one instrument this tab is currently pricing - "providing
    the instrument", standing in for an eventual Aladdin order pull (see the
    FX Option tab's own to-do note). Triggers an immediate refresh rather
    than waiting up to FXOPT_FAST_REFRESH_S for the first snapshot."""
    try:
        base, quote = fxoption.parse_pair(payload.get("pair", ""))
        strike = float(payload["strike"])
        if strike <= 0:
            raise ValueError("strike must be positive")
        expiry_date = datetime.datetime.strptime(payload["expiry"], "%Y-%m-%d").date()
        option_type = (payload.get("type") or "put").strip().lower()
        if option_type not in ("put", "call"):
            raise ValueError("type must be 'put' or 'call'")
        premium_adjusted = bool(payload.get("premium_adjusted", False))
    except (KeyError, ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    _fxopt_ensure_fwd_scale(base, quote)
    with _fxopt_lock:
        pair_changed = (base, quote) != (_fxopt_instrument["base"], _fxopt_instrument["quote"])
        _fxopt_instrument.update(base=base, quote=quote, strike=strike, expiry=expiry_date.isoformat(),
                                  is_call=(option_type == "call"), premium_adjusted=premium_adjusted)
        if pair_changed:
            _fxopt_tag_values.clear()
    _fxopt_do_fast_refresh()
    if pair_changed:
        threading.Thread(target=_fxopt_do_term_refresh, daemon=True).start()
    return JSONResponse({"ok": True})


@app.get("/api/fxoption/snapshot")
def get_fxoption_snapshot() -> JSONResponse:
    with _fxopt_lock:
        instrument = dict(_fxopt_instrument)
        values = dict(_fxopt_tag_values)
        term_values = dict(_fxopt_term_values)
        as_of = _fxopt_as_of
        tag_error = _fxopt_tag_error

    if instrument["strike"] is None:
        return JSONResponse({"error": "no instrument selected yet"})
    base, quote = instrument["base"], instrument["quote"]
    spot = values.get(fxoption.spot_tag(base, quote))
    if spot is None:
        return JSONResponse({"error": f"waiting for the first live {fxoption.pair_label(base, quote)} spot tick"})
    fwd_scale = _fxopt_fwd_scale.get(fxoption.pair_key(base, quote))
    if fwd_scale is None:
        return JSONResponse({"error": "waiting for forward-point scale (FWD_SCALE) lookup"})

    today = datetime.datetime.now(datetime.timezone.utc).date()
    expiry_date = datetime.date.fromisoformat(instrument["expiry"])
    bracket = fxoption.bracket_tenors(expiry_date, today)
    bracket_data = []
    for tenor, date_ in bracket:
        smile = fxoption.build_tenor_smile(base, quote, tenor, values, spot, fwd_scale, today,
                                            premium_adjusted=instrument["premium_adjusted"])
        bracket_data.append((tenor, date_, smile))
    rate_tag = fxoption.overnight_rate_tag(quote)
    quote_rate_pct = values.get(rate_tag) if rate_tag else None
    try:
        result = fxoption.value_option(base, quote, bracket_data, spot, instrument["strike"],
                                        expiry_date, today, is_call=instrument["is_call"],
                                        quote_rate_pct=quote_rate_pct)
    except ValueError as exc:
        # Still include spot/quote even on a pricing failure (e.g. the
        # placeholder strike a fresh pair starts with, before the frontend's
        # own autofill-to-live-spot correction lands) - the frontend's
        # autofill depends on seeing a real spot here, not just an error
        # string, or a bad first guess can never self-correct.
        return JSONResponse({"error": str(exc), "spot": spot, "quote": quote, "base": base})

    result["quote"] = quote
    result["premium_adjusted"] = instrument["premium_adjusted"]
    result["as_of"] = as_of.isoformat() if as_of else None
    result["tag_error"] = tag_error
    result["term_structure"] = fxoption.build_term_structure(base, quote, term_values, today)
    result["rate_source"] = rate_tag

    # GBP triangulation, for whichever currency this option's premium is
    # actually denominated in (call_ccy - see value_option) - via the 9
    # USD-legged spots always fetched above, same triangulation a
    # GBP-denominated desk already does by eye.
    result["gbp_per_call_ccy_unit"] = fxoption.gbp_per_unit(values, result["call_ccy"])

    # Bloomberg source tags for each live-pulled field, for the page's own
    # "source" labels - not shown for computed/derived outputs (premium,
    # greeks), which get a "(computed)" label client-side instead.
    call_ccy = result["call_ccy"]
    gbp_source = None
    if call_ccy == "GBP":
        gbp_source = None  # GBP IS the call currency - no triangulation needed
    elif call_ccy == "USD":
        gbp_source = "GBPUSD Curncy"
    else:
        usd_leg = next((fxoption.spot_tag(b, q) for b, q in fxoption.USD_LEGGED_PAIRS if call_ccy in (b, q)), None)
        gbp_source = f"GBPUSD Curncy + {usd_leg}" if usd_leg else None
    result["sources"] = {
        "spot": fxoption.spot_tag(base, quote),
        "forward": [fxoption.fwd_tag(base, quote, tenor) for tenor, _date in bracket],
        "vol": [tag for tenor, _date in bracket for tag in
                (fxoption.atm_tag(base, quote, tenor), fxoption.rr_tag(base, quote, tenor, "25"),
                 fxoption.bf_tag(base, quote, tenor, "25"), fxoption.rr_tag(base, quote, tenor, "10"),
                 fxoption.bf_tag(base, quote, tenor, "10"))],
        "rate": rate_tag,
        "gbp": gbp_source,
    }

    # Full vol surface - every tenor with a smile built (from _fxopt_term_values,
    # the slower 78-tag refresh above), not just the 1-2 bracket tenors this
    # option is actually priced off. Purely a visualization of what's live
    # right now on the whole curve - build_tenor_smile is the same call the
    # bracket pricing above uses, just run once per tenor instead of twice.
    surface = []
    for tenor in fxoption.TENORS:
        tenor_date_ = fxoption.tenor_date(tenor, today)
        smile = fxoption.build_tenor_smile(base, quote, tenor, term_values, spot, fwd_scale, today,
                                            premium_adjusted=instrument["premium_adjusted"])
        if smile is None:
            continue
        surface.append({
            "tenor": tenor, "years": smile["years"],
            "points": [{"strike": p["strike"], "vol": p["vol"], "label": p["label"]} for p in smile["points"]],
            "curve": fxoption.strike_curve(smile["points"], n=25),
        })
    result["surface"] = surface
    return JSONResponse(result)


@app.get("/api/fxoption/history")
def get_fxoption_history(pair: str = "EURUSD", tenor: str = "1M") -> JSONResponse:
    try:
        base, quote = fxoption.parse_pair(pair)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if tenor not in fxoption.TENORS:
        return JSONResponse({"error": f"unknown tenor {tenor!r}"}, status_code=400)

    key = (base, quote, tenor)
    cached = _fxopt_history_cache.get(key)
    if cached and (time.time() - cached["fetched_at"]) < FXOPT_HISTORY_TTL_S:
        return JSONResponse({"pair": fxoption.pair_label(base, quote), "tenor": tenor,
                              "dates": cached["dates"], "vols": cached["vols"]})

    tag = fxoption.atm_tag(base, quote, tenor)
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=FXOPT_HISTORY_LOOKBACK_DAYS)
    try:
        result = worker.submit(historical_data, [tag], ["PX_LAST"],
                                start.strftime("%Y%m%d"), end.strftime("%Y%m%d")).result(timeout=15)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    rows = result.get(tag, [])
    dates = [r["date"].isoformat() for r in rows if "PX_LAST" in r]
    vols = [r["PX_LAST"] for r in rows if "PX_LAST" in r]
    _fxopt_history_cache[key] = {"dates": dates, "vols": vols, "fetched_at": time.time()}
    return JSONResponse({"pair": fxoption.pair_label(base, quote), "tenor": tenor, "dates": dates, "vols": vols})


# --- SPX option: realtime valuation off Bloomberg's own listed-option
# analytics - see spxoption.py's module docstring for why this reads
# Bloomberg's computed greeks directly rather than building a second,
# independent model on top (unlike fxoption.py, which has to - there's no
# per-strike FX option tag to read).
SPXOPT_FAST_REFRESH_S = 4  # one security's worth of fields - cheap enough for this cadence

_spxopt_lock = threading.Lock()
_spxopt_instrument = {"ticker": None, "strike": None, "expiry": None, "is_call": True, "contracts": 1.0}
_spxopt_values: dict = {}   # field -> value, for the one currently-resolved contract
_spxopt_as_of: Optional[datetime.datetime] = None
_spxopt_tag_error: Optional[str] = None


def _spx_resolve_contract(session, target_strike, expiry_date, is_call):
    """Runs on the BLPWorker thread (see spxoption.resolve_contract's own
    docstring for why reference_data_fn/session are passed in rather than
    imported directly - keeps the module testable without a live session)."""
    return spxoption.resolve_contract(reference_data, session, target_strike, expiry_date, is_call)


def _spxopt_do_fast_refresh() -> None:
    global _spxopt_as_of, _spxopt_tag_error
    with _spxopt_lock:
        ticker = _spxopt_instrument["ticker"]
    if ticker is None:
        return
    try:
        result = worker.submit(reference_data, [ticker], spxoption.FIELDS).result(timeout=10)
        values = result["data"].get(ticker, {})
        with _spxopt_lock:
            _spxopt_values.clear()
            _spxopt_values.update(values)
            _spxopt_as_of = datetime.datetime.now(datetime.timezone.utc)
            _spxopt_tag_error = f"{len(result['errors'])} tag(s) failed" if result["errors"] else None
    except Exception as exc:  # noqa: BLE001
        with _spxopt_lock:
            _spxopt_tag_error = str(exc)
        print(f"[spxoption] fast refresh failed: {exc}", file=sys.stderr)


def _spxopt_fast_refresh_loop() -> None:
    while True:
        time.sleep(SPXOPT_FAST_REFRESH_S)
        _spxopt_do_fast_refresh()


@app.post("/api/spxoption/instrument")
def set_spxoption_instrument(payload: dict) -> JSONResponse:
    """Resolves the request to a real, currently-listed contract nearest the
    requested strike (see spxoption.resolve_contract) and sets it as the one
    this tab is pricing - "providing the instrument", same as the FX Option
    tab, standing in for an eventual Aladdin order pull."""
    try:
        target_strike = float(payload["strike"])
        if target_strike <= 0:
            raise ValueError("strike must be positive")
        expiry_date = datetime.datetime.strptime(payload["expiry"], "%Y-%m-%d").date()
        option_type = (payload.get("type") or "call").strip().lower()
        if option_type not in ("put", "call"):
            raise ValueError("type must be 'put' or 'call'")
        contracts = float(payload.get("contracts", 1))
        if contracts <= 0:
            raise ValueError("contracts must be positive")
    except (KeyError, ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        resolved = worker.submit(_spx_resolve_contract, target_strike, expiry_date,
                                  option_type == "call").result(timeout=15)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    if resolved is None:
        return JSONResponse({"error": f"no listed SPX contract found near strike {target_strike:g} "
                                       f"for {expiry_date.isoformat()} - try a nearby strike or expiry"})

    with _spxopt_lock:
        _spxopt_instrument.update(ticker=resolved["ticker"], strike=resolved["strike"],
                                   expiry=expiry_date.isoformat(), is_call=(option_type == "call"),
                                   contracts=contracts)
        _spxopt_values.clear()
    _spxopt_do_fast_refresh()
    return JSONResponse({"ok": True, "ticker": resolved["ticker"], "strike": resolved["strike"],
                          "exact": resolved["exact"]})


@app.get("/api/spxoption/snapshot")
def get_spxoption_snapshot() -> JSONResponse:
    with _spxopt_lock:
        instrument = dict(_spxopt_instrument)
        values = dict(_spxopt_values)
        as_of = _spxopt_as_of
        tag_error = _spxopt_tag_error

    if instrument["ticker"] is None:
        return JSONResponse({"error": "no instrument selected yet"})
    with _state_lock:
        spot = _raw.get("SPX Index", {}).get("LAST_PRICE")
        spot_update = _last_update.get("SPX Index")
    if spot is None:
        return JSONResponse({"error": "waiting for the first live SPX Index tick"})

    snap = spxoption.snapshot(values, instrument["ticker"], spot, contracts=instrument["contracts"])
    snap["is_call"] = instrument["is_call"]
    snap["expiry"] = snap["expiry"] or instrument["expiry"]
    snap["as_of"] = as_of.isoformat() if as_of else None
    snap["spot_as_of"] = spot_update.isoformat() if spot_update else None
    snap["tag_error"] = tag_error
    return JSONResponse(snap)


def _start_worker_with_retry() -> None:
    """BLPWorker() creation, with backoff, run entirely off FastAPI's own
    startup event - `worker.start()` raises if Bloomberg isn't reachable at
    that exact moment (Terminal not logged in yet, bbcomm still starting up,
    a transient blip), and running it synchronously inside `startup()` used
    to mean the *whole HTTP server* never came up at all in that case - not
    even far enough to serve the static page with a "waiting to connect"
    message, just silent ECONNREFUSED on port 8008 (confirmed live: this is
    exactly what happened once the Terminal ended up logged out mid-session).
    Every other place that calls `worker.submit(...)` already wraps it in a
    try/except and degrades gracefully (a None or not-yet-connected `worker`
    just times out the same way a slow Bloomberg call would) - only this
    startup path and `_load_history()` were unprotected, so those two are
    what move into the retry loop below; nothing else needed to change."""
    global worker
    backoff = 5
    while True:
        candidate = BLPWorker()
        try:
            candidate.start()
        except Exception as exc:  # noqa: BLE001
            print(f"[startup] Bloomberg worker failed to start ({exc}) - retrying in {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        worker = candidate
        print("[startup] Bloomberg worker connected", file=sys.stderr)
        _load_history()
        return


@app.on_event("startup")
def startup() -> None:
    threading.Thread(target=_start_worker_with_retry, daemon=True).start()
    threading.Thread(target=_subscription_loop, daemon=True).start()
    threading.Thread(target=_fxopt_fast_refresh_loop, daemon=True).start()
    threading.Thread(target=_fxopt_term_refresh_loop, daemon=True).start()
    threading.Thread(target=_spxopt_fast_refresh_loop, daemon=True).start()


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


@app.post("/api/terminal/open")
def open_in_terminal(payload: dict) -> JSONResponse:
    """"Terminal Connect" - see terminal_connect.py's module docstring for
    how this actually drives the Terminal (Windows UI automation, not a
    Bloomberg API - blpapi has no "navigate the Terminal UI" call) and its
    current unverified status."""
    ticker = (payload.get("ticker") or "").strip()
    if not ticker:
        return JSONResponse({"ok": False, "error": "no ticker given"}, status_code=400)
    return JSONResponse(terminal_connect.open_security(ticker))


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8008, log_level="info")
