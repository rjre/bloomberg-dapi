# bloomberg-dapi

A Python client for the **Bloomberg Desktop API** (`blpapi`) — talks directly to a
Bloomberg Terminal running on the same machine over `localhost:8194`. Unlike
[IB Connect](https://github.com/rjre/bloomberg-ib), this needs **no Web API
entitlement, no console.bloomberg.com application, no firm-admin approval** —
just a Terminal installed and logged in locally. Every module here was tested
live against a running Terminal on 2026-09-05.

Also includes a working example: a live, self-refreshing **global macro
dashboard** (`examples/dashboard/`) built on top of the library.

## Setup

`blpapi` is not on PyPI — install it from Bloomberg's own index:

```bash
pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
pip install -r requirements.txt
```

Bloomberg Terminal must be running and logged in on this machine. That's it —
no credentials, no config file.

## Library (`bdapi/`)

| Module | What it does |
|---|---|
| `session.py` | `BLPSession` — context-managed wrapper around one `blpapi.Session` |
| `worker.py` | `BLPWorker` — a dedicated background thread owning the session, serialising concurrent callers through a job queue (needed the moment more than one part of your app talks to Bloomberg at once — see below) |
| `reference.py` | `reference_data()` — current field values (`=BDP()` equivalent) |
| `historical.py` | `historical_data()` — time series over a date range (`=BDH()` equivalent) |
| `intraday.py` | `intraday_bars()` / `intraday_ticks()` — granular intraday history |
| `search.py` | `search_securities()` — ticker/instrument lookup |
| `subscription.py` | `MarketDataSubscriber` — real-time streaming quotes |

```python
from bdapi import BLPSession, reference_data, historical_data

with BLPSession() as s:
    print(reference_data(s, ["IBM US Equity", "SPX Index"], ["PX_LAST", "CHG_PCT_1D"]))
    print(historical_data(s, ["SPX Index"], ["PX_LAST"], "20260101", "20260201"))
```

### Why `BLPWorker` exists

`blpapi.Session.nextEvent()` returns the next event for the **whole session**,
not scoped to one in-flight request. If two threads both call
`sendRequest`/`nextEvent` concurrently (e.g. a web server handling a request
while a background poller is also mid-request), one thread's response can be
silently consumed by the other's loop. `BLPWorker` runs a single thread that
owns the session exclusively; everyone else calls `.submit(fn, *args)` and
gets a `concurrent.futures.Future` back. This is what the dashboard uses.

## The dashboard (`examples/dashboard/`)

```bash
cd examples/dashboard
python server.py
# open http://localhost:8008
```

A live "global macro morning brief": equity indices, government bond yields,
FX majors, commodities, and VIX (28 instruments, all validated live), each
with a sparkline, refreshing every 3 seconds, plus:

- **Market Pulse** — a short, rule-based commentary paragraph generated from
  the live numbers (biggest equity mover, biggest rates move, dollar
  direction, gold/oil, VIX level).
- **Market Movers** — the biggest movers today within this universe.

Architecture: one `BLPWorker` thread owns the Bloomberg session; a background
thread refreshes a cached snapshot every 3s via `worker.submit(...)`; FastAPI
endpoints (`/api/snapshot`, `/api/history`) just read the cache, so HTTP
requests never block on Bloomberg I/O. The frontend is plain HTML/CSS/JS with
hand-rolled inline-SVG sparklines and CSS bar charts — deliberately **zero
external CDN/JS dependencies**, so it can't be broken by a corporate firewall
blocking outbound script hosts.

### On "newsflow"

This Terminal has no Bloomberg **News API** entitlement — `//blp/newsheadlines`,
`//blp/newsstory`, and `//blp/newscategory` all fail to resolve
(`ServiceOpenFailure`), and searching `//blp/apiflds` for "headline" only turns
up financial-statement fields (e.g. `HEADLINE_REV`), not story/headline
services. If your Terminal *does* have News API entitlement, that's a
different `blpapi` service this library doesn't wrap yet. In its place, the
dashboard's Market Pulse panel generates commentary from the live price data
itself — it's clearly labelled as such, not presented as real news.

### On IB (Instant Bloomberg) chat messages

This library does **not** read personal IB chat history, and deliberately
doesn't try to. That content isn't exposed through the Desktop API at all —
the only programmatic path is [IB Connect](https://github.com/rjre/bloomberg-ib),
which (a) is blocked for this account pending a Web API entitlement request,
and (b) even once entitled, is designed around firm-governed streams for
integrating chat into internal systems, not personal message export. Chat
content is subject to firm compliance/surveillance obligations that a
side-channel API client has no business working around.

## Validated live (2026-09-05)

- `reference_data` — 28 tickers across equities/rates/FX/commodities/vol, all resolved.
- `historical_data` — 30-day daily series, correct.
- `intraday_bars` / `intraday_ticks` — correct once queried within actual market hours.
- `search_securities` — correct (`instrumentListRequest` on `//blp/instruments`).
- `BLPWorker` — correct under concurrent submits (reference + historical + reference fired together, resolved correctly, no cross-talk).
- Dashboard — running end-to-end, all 5 categories + movers + pulse rendering correctly (verified via DOM inspection, not just screenshots — the preview tool used to build this had a scroll-position screenshot rendering quirk that doesn't reflect a real browser).
