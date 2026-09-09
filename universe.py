"""The curated global-macro instrument universe for the dashboard, and the
rule-based "Market Pulse" narrative generator that substitutes for a real
news feed (this Terminal has no Bloomberg News API entitlement - see
README.md).
"""
from __future__ import annotations

from typing import List, Tuple

# (bloomberg ticker, short display label)
UNIVERSE: dict = {
    "Equities": [
        ("SPX Index", "S&P 500"),
        ("INDU Index", "Dow Jones"),
        ("CCMP Index", "Nasdaq Composite"),
        ("RTY Index", "Russell 2000"),
        ("UKX Index", "FTSE 100"),
        ("SX5E Index", "Euro Stoxx 50"),
        ("DAX Index", "DAX"),
        ("NKY Index", "Nikkei 225"),
        ("HSI Index", "Hang Seng"),
        ("SHCOMP Index", "Shanghai Composite"),
    ],
    "Rates": [
        ("USGG10YR Index", "US 10Y"),
        ("USGG2YR Index", "US 2Y"),
        ("GUKG10 Index", "UK 10Y Gilt"),
        ("GDBR10 Index", "Germany 10Y Bund"),
        ("GJGB10 Index", "Japan 10Y"),
    ],
    "FX": [
        ("EURUSD Curncy", "EUR/USD"),
        ("GBPUSD Curncy", "GBP/USD"),
        ("USDJPY Curncy", "USD/JPY"),
        ("DXY Curncy", "Dollar Index"),
        ("AUDUSD Curncy", "AUD/USD"),
        ("USDCNH Curncy", "USD/CNH"),
    ],
    "Commodities": [
        ("XAU Curncy", "Gold (Spot)"),
        ("CO1 Comdty", "Brent Crude"),
        ("CL1 Comdty", "WTI Crude"),
        ("HG1 Comdty", "Copper"),
        ("SI1 Comdty", "Silver"),
    ],
    "Volatility": [
        ("VIX Index", "VIX"),
    ],
}

RATES_CATEGORY = "Rates"  # displayed in bps rather than %
FX_CATEGORY = "FX"  # displayed as bid/ask rather than a single last price

ALL_TICKERS: List[str] = [t for group in UNIVERSE.values() for t, _ in group]
LABELS: dict = {t: label for group in UNIVERSE.values() for t, label in group}
CATEGORY_OF: dict = {t: cat for cat, group in UNIVERSE.items() for t, _ in group}

# Reference-data field names, for one-off reference_data()/historical_data()
# lookups - NOT what the live dashboard/app use for their subscription-based
# view (that's SUBSCRIPTION_FIELDS in app/main.py and examples/dashboard/
# server.py: LAST_PRICE/NET_CHANGE/HIGH/LOW). Don't poll reference_data() with
# these fields in a loop to build a "live" view - see README.md "Rate limits".
FIELDS = ["PX_LAST", "CHG_PCT_1D", "CHG_NET_1D", "PX_HIGH", "PX_LOW"]


def _fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def build_pulse(snapshot: dict) -> str:
    """A short, templated market-commentary paragraph built from the live
    snapshot - the closest honest substitute for a real newsflow panel on
    an entitlement without Bloomberg News API access."""
    rows = []
    for cat, items in snapshot.items():
        for row in items:
            rows.append({**row, "category": cat})

    if not rows:
        return "Waiting for the first data refresh..."

    equities = [r for r in rows if r["category"] == "Equities" and r["chgPct"] is not None]
    rates = [r for r in rows if r["category"] == "Rates" and r["chgNet"] is not None]
    fx = [r for r in rows if r["category"] == "FX" and r["chgPct"] is not None]
    commod = [r for r in rows if r["category"] == "Commodities" and r["chgPct"] is not None]
    vix = next((r for r in rows if r["ticker"] == "VIX Index"), None)

    sentences = []

    if equities:
        up = [r for r in equities if r["chgPct"] > 0]
        down = [r for r in equities if r["chgPct"] < 0]
        biggest = max(equities, key=lambda r: abs(r["chgPct"]))
        tone = "broadly higher" if len(up) > len(down) else "broadly lower" if len(down) > len(up) else "mixed"
        sentences.append(
            f"Global equities are {tone}, led by {biggest['label']} at {_fmt_pct(biggest['chgPct'])}."
        )

    if rates:
        biggest_move = max(rates, key=lambda r: abs(r["chgNet"]))
        direction = "higher" if biggest_move["chgNet"] > 0 else "lower"
        bps = abs(biggest_move["chgNet"]) * 100
        sentences.append(
            f"In rates, {biggest_move['label']} yields are {direction} by {bps:.0f}bp to {biggest_move['last']:.2f}%."
        )

    dxy = next((r for r in fx if r["ticker"] == "DXY Curncy"), None)
    if dxy is not None:
        dollar_tone = "firmer" if dxy["chgPct"] > 0 else "softer"
        sentences.append(f"The Dollar Index is {dollar_tone} ({_fmt_pct(dxy['chgPct'])}).")

    gold = next((r for r in commod if r["ticker"] == "XAU Curncy"), None)
    oil = next((r for r in commod if r["ticker"] == "CO1 Comdty"), None)
    commod_bits = []
    if gold is not None:
        commod_bits.append(f"gold {_fmt_pct(gold['chgPct'])}")
    if oil is not None:
        commod_bits.append(f"Brent {_fmt_pct(oil['chgPct'])}")
    if commod_bits:
        sentences.append("Commodities: " + ", ".join(commod_bits) + ".")

    if vix is not None and vix.get("last") is not None:
        level = vix["last"]
        mood = "elevated" if level > 25 else "subdued" if level < 15 else "moderate"
        sentences.append(f"The VIX is {mood} at {level:.1f}.")

    return " ".join(sentences)
