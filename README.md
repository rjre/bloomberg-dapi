# bloomberg-dapi

A Python client for the **Bloomberg Desktop API** (`blpapi`) — talks directly to a
Bloomberg Terminal running on the same machine over `localhost:8194`. Unlike
[IB Connect](https://github.com/rjre/bloomberg-ib), this needs **no Web API
entitlement, no console.bloomberg.com application, no firm-admin approval** —
just a Terminal installed and logged in locally.

Two ways to run the flagship example, a live "global macro morning brief":

- **`app/`** — a self-contained **desktop app** (recommended). One process, one
  native window, no HTTP server, no open port, no browser tab. Can be packaged
  into a single standalone `.exe` — verified working.
- **`examples/dashboard/`** — the same UI as a FastAPI server + browser tab.
  Kept for comparison/reference; `app/` is the one to actually deploy if your
  IT policy doesn't want a locally-listening server, even a loopback-only one.

## Setup

`blpapi` is not on PyPI — install it from Bloomberg's own index:

```bash
pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
pip install -r requirements.txt
```

Bloomberg Terminal must be running and logged in on this machine. That's it —
no credentials, no config file.

## The desktop app (`app/`)

```bash
cd app
python main.py
```

Opens a single native window titled "Global Macro — Morning Brief" — no
browser, no port. Built with `pywebview`, which embeds the OS's WebView
(WebView2 on Windows) directly; the page talks to Python through
`window.pywebview.api.get_snapshot()` / `.get_history()` instead of `fetch()`
calls to a server.

### Packaging as a single `.exe`

Verified working end-to-end 2026-09-06 (PyInstaller 6.22.2, Python 3.13.14,
blpapi 3.24.11) — see the full command and the one non-obvious gotcha
(`blpapi`'s `ffiutils` helper is loaded via `glob.glob()` at runtime, not a
normal `import`, so PyInstaller's static analysis misses it and needs an
explicit `--add-binary`) in the docstring at the top of `app/main.py`. The
result: `dist/GlobalMacroBrief/GlobalMacroBrief.exe` — double-click, no Python
install needed on the target machine, still needs a Terminal running locally
on whatever machine runs it.

## The browser-based dashboard (`examples/dashboard/`)

```bash
cd examples/dashboard
python server.py
# open http://localhost:8008
```

Same UI, served over FastAPI + a browser tab instead of a native window. Kept
for reference/comparison — prefer `app/` unless you specifically want a
server (e.g. so multiple people on a LAN can view the same instance, which
`app/` doesn't support - it's single-viewer, single-process).

## What the dashboard shows

Equity indices, government bond yields, FX majors, commodities, and VIX (27
instruments, all validated live) with sparklines, plus:

- **Market Pulse** — a short, rule-based commentary paragraph generated from
  the live numbers (biggest equity mover, biggest rates move, dollar
  direction, gold/oil, VIX level).
- **Market Movers** — the biggest movers today within this universe.

The frontend is plain HTML/CSS/JS with hand-rolled inline-SVG sparklines and
CSS bar charts — deliberately **zero external CDN/JS dependencies**, so it
can't be broken by a corporate firewall blocking outbound script hosts.

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
| `util.py` | `BloombergResponseError` — raised on a top-level Bloomberg `responseError` (see "Rate limits" below); `element_to_dict`/`to_python` internal helpers |

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
gets a `concurrent.futures.Future` back. Both `app/` and
`examples/dashboard/` use it.

## Rate limits — read this before changing the refresh interval

Bloomberg **meters daily data capacity** on the Desktop API. An early version
of this dashboard polled every 3 seconds; left running unattended overnight,
it exhausted a full day's quota (confirmed via a Bloomberg `responseError`:
`DAILY_CAPACITY_REACHED`). `reference_data`/`historical_data`/`intraday_*`
used to swallow this silently (a `responseError` message has no
`securityData`, so it looked exactly like "no data" - real live debugging was
needed to find the actual cause). They now raise `BloombergResponseError`
instead.

Both apps now poll every **60 seconds** (`REFRESH_SECONDS` in
`app/main.py` / `examples/dashboard/server.py`) — plenty for a macro
dashboard, nowhere near daily capacity — and back off to 30 minutes
(`CAPACITY_BACKOFF_SECONDS`) if the limit is ever hit again, rather than
continuing to hammer an already-exhausted quota. If you widen the instrument
universe or shorten the interval, watch for `BloombergResponseError` and
consider whether your firm's daily capacity can actually support it.

## On "newsflow"

This Terminal has no Bloomberg **News API** entitlement — `//blp/newsheadlines`,
`//blp/newsstory`, and `//blp/newscategory` all fail to resolve
(`ServiceOpenFailure`), and searching `//blp/apiflds` for "headline" only turns
up financial-statement fields (e.g. `HEADLINE_REV`), not story/headline
services. If your Terminal *does* have News API entitlement, that's a
different `blpapi` service this library doesn't wrap yet. In its place, the
Market Pulse panel generates commentary from the live price data itself — it's
clearly labelled as such, not presented as real news.

## On IB (Instant Bloomberg) chat messages

This library does **not** read personal IB chat history, and deliberately
doesn't try to. That content isn't exposed through the Desktop API at all —
the only programmatic path is [IB Connect](https://github.com/rjre/bloomberg-ib),
which (a) is blocked for this account pending a Web API entitlement request,
and (b) even once entitled, is designed around firm-governed streams for
integrating chat into internal systems, not personal message export. Chat
content is subject to firm compliance/surveillance obligations that a
side-channel API client has no business working around.

## Validated live

- `reference_data` — 27 tickers across equities/rates/FX/commodities/vol, all resolved (2026-09-05).
- `historical_data` — 30-day daily series, correct (2026-09-05).
- `intraday_bars` / `intraday_ticks` — correct once queried within actual market hours (2026-09-05).
- `search_securities` — correct (`instrumentListRequest` on `//blp/instruments`) (2026-09-05).
- `BLPWorker` — correct under concurrent submits (2026-09-05).
- `BloombergResponseError` — confirmed it now surfaces `DAILY_CAPACITY_REACHED` correctly instead of returning empty data (2026-09-06, found by hitting the real limit).
- Both apps degrade gracefully (no crash, last-good data stays visible, clear on-screen message) when Bloomberg returns a `responseError` — verified by actually hitting `DAILY_CAPACITY_REACHED` (2026-09-06).
- `app/` desktop window — verified opening and rendering correctly as a real
  native window on this machine (2026-09-06), both as a plain `python main.py`
  process and as the packaged standalone `GlobalMacroBrief.exe`.
