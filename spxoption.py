"""Realtime valuation of a listed SPX index option at a user-chosen strike
and expiry, read directly off Bloomberg's own option analytics - unlike
fxoption.py, this module builds no pricing model of its own.

Why no model here: SPX options are exchange-listed (Cboe), and Bloomberg
carries live BID/ASK/PX_LAST plus its own computed DELTA/GAMMA/VEGA/RHO and
(via OPT_THETA_MID) theta directly on each specific contract's own security
- there is no "no strike-specific tag exists" gap to bridge the way there is
for OTC FX options (see fxoption.py's docstring). Reading Bloomberg's own
analytics for the exact listed contract is strictly more faithful than
re-deriving a vol surface from moneyness-bucketed fields and running a
second, independent Black-Scholes on top of it - so that's what this module
does: resolve the request to a real, currently-listed contract, then read
its fields.

Ticker convention (confirmed live against this Terminal):

    "{ROOT} US {MM/DD/YY} {C|P}{strike} Index"

e.g. "SPXW US 09/11/26 C7650 Index". `ROOT` is always "SPXW" here - for
every near-dated expiry checked live, "SPXW" and "SPX" resolved identical
data (same PX_LAST/DELTA/...), so there is no ambiguity to resolve per
request. Strikes near spot are listed in 25-point increments (confirmed
live for a ~1-week expiry); `resolve_contract` searches a small window
around the requested strike and snaps to the nearest one that's actually
listed (`OPT_STRIKE_PX` present) rather than assuming a fixed increment
blindly - Bloomberg's own increment schedule varies with tenor and distance
from spot.

Standard SPX contract multiplier is $100/point - used for the position-level
dollar figures (`dollar_delta`, `premium_total`) below.
"""

from __future__ import annotations

import datetime

CONTRACT_MULTIPLIER = 100.0

# How far (in strike points) either side of the requested strike to search
# when snapping to a listed contract, and at what increment - covers the
# 25-point-near-spot convention confirmed live, with a fallback 5-point pass
# for indices/expiries that list finer strikes close to the money.
_SEARCH_OFFSETS = list(range(-200, 201, 25)) + list(range(-20, 21, 5))

FIELDS = [
    "PX_LAST", "PX_BID", "PX_ASK",
    "OPT_IMPLIED_VOLATILITY_MID", "DELTA", "GAMMA", "VEGA", "RHO",
    "OPT_THETA_MID",  # plain THETA errors "Field not applicable" on SPX index options - confirmed live
    "OPT_STRIKE_PX", "OPT_EXPIRE_DT", "OPT_UNDL_PX", "OPT_PUT_CALL",
]


def _fmt_strike(strike: float) -> str:
    return f"{strike:g}"


def contract_ticker(strike: float, expiry_date: datetime.date, is_call: bool) -> str:
    return f"SPXW US {expiry_date:%m/%d/%y} {'C' if is_call else 'P'}{_fmt_strike(strike)} Index"


def candidate_strikes(target_strike: float):
    """Distinct candidate strikes to probe, nearest-first, so the first hit
    in resolve_contract's search is the closest listed strike to what was
    asked for."""
    seen = set()
    candidates = []
    for offset in sorted(_SEARCH_OFFSETS, key=abs):
        strike = round((target_strike + offset) / 5.0) * 5.0
        if strike <= 0 or strike in seen:
            continue
        seen.add(strike)
        candidates.append(strike)
    return candidates


def resolve_contract(reference_data_fn, session, target_strike: float, expiry_date: datetime.date, is_call: bool):
    """Finds the listed contract nearest `target_strike` for this expiry/type.
    `reference_data_fn(session, securities, fields)` is bdapi.reference_data
    (injected so this stays testable without a live session). Returns
    {"ticker", "strike", "exact"} or None if nothing in the search window is
    actually listed.

    One batched call for the whole search window (up to ~25 securities) -
    OPT_STRIKE_PX is present on any genuinely listed contract even when it
    has no live quote yet (confirmed live: a real but illiquid contract
    still carries OPT_STRIKE_PX/OPT_EXPIRE_DT), so that field is the
    existence test, independent of whether PX_LAST/DELTA happen to be
    populated yet."""
    strikes = candidate_strikes(target_strike)
    securities = [contract_ticker(s, expiry_date, is_call) for s in strikes]
    result = reference_data_fn(session, securities, ["OPT_STRIKE_PX"])
    data = result["data"]
    best = None
    for strike, security in zip(strikes, securities):
        entry = data.get(security)
        if entry is None or entry.get("OPT_STRIKE_PX") is None:
            continue
        if best is None or abs(strike - target_strike) < abs(best[0] - target_strike):
            best = (strike, security)
    if best is None:
        return None
    strike, security = best
    return {"ticker": security, "strike": strike, "exact": abs(strike - target_strike) < 1e-9}


def snapshot(values: dict, ticker: str, spot, contracts: float = 1.0):
    """`values`: {field: value} for `ticker`'s FIELDS, as last read from
    Bloomberg. `spot` is the live SPX Index price (from the dashboard's own
    existing subscription - genuinely tick-live, not part of this module's
    own periodic refresh). `contracts` scales the position-level figures
    ($100/point multiplier - see module docstring)."""
    price = values.get("PX_LAST")
    bid, ask = values.get("PX_BID"), values.get("PX_ASK")
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else price
    delta = values.get("DELTA")
    gamma = values.get("GAMMA")
    vega = values.get("VEGA")
    rho = values.get("RHO")
    theta = values.get("OPT_THETA_MID")
    iv = values.get("OPT_IMPLIED_VOLATILITY_MID")
    strike = values.get("OPT_STRIKE_PX")
    expiry = values.get("OPT_EXPIRE_DT")
    put_call = values.get("OPT_PUT_CALL")
    return {
        "ticker": ticker, "strike": strike, "expiry": str(expiry) if expiry else None,
        "put_call": put_call, "spot": spot,
        "price": price, "bid": bid, "ask": ask, "mid": mid,
        "iv_pct": iv, "delta": delta, "gamma": gamma, "vega": vega, "theta_per_day": theta, "rho": rho,
        "contracts": contracts,
        "premium_total": (mid * CONTRACT_MULTIPLIER * contracts) if mid is not None else None,
        "dollar_delta": (delta * CONTRACT_MULTIPLIER * contracts * spot) if delta is not None and spot else None,
        # $ change in dollar_delta per 1-point move in SPX - the standard
        # "dollar gamma" figure. Deliberately *not* scaled up to a 1% (~76pt)
        # move: gamma is a point estimate, and extrapolating it over a move
        # that large ignores how fast delta itself curves away from linear
        # over that range (a 76pt move can imply a bigger delta swing than
        # gamma*76 alone predicts, sometimes past 1.0) - a 1-point figure
        # stays honest to what a point gamma actually measures.
        "dollar_gamma_per_point": (gamma * CONTRACT_MULTIPLIER * contracts) if gamma is not None else None,
    }
