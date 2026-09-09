"""Realtime Garman-Kohlhagen (Black-76) valuation of a G10 vanilla FX option
at an arbitrary strike and expiry date, built from Bloomberg's own OTC FX
vol-surface quotes (ATM / risk-reversal / butterfly) rather than from any
listed-option field - Bloomberg doesn't carry a single tag for "this option
at this strike", so the value here is derived, not quoted.

Field conventions (all confirmed live against this Terminal, see the repo's
working notes - there is no public field-reference for the FX vol surface,
so these were verified by direct query rather than assumed):

  spot            "{PAIR} Curncy"                    e.g. EURUSD Curncy
  ATM vol         "{PAIR}V{TENOR} Curncy"             e.g. EURUSDV1M Curncy
  25D risk-rev.   "{PAIR}25R{TENOR} Curncy"
  25D butterfly   "{PAIR}25B{TENOR} Curncy"
  10D risk-rev.   "{PAIR}10R{TENOR} Curncy"
  10D butterfly   "{PAIR}10B{TENOR} Curncy"
  fwd points      "{PAIR}{FWD_TENOR} Curncy"          PX_LAST is *points*, not
                                                       an outright - divide by
                                                       10**FWD_SCALE (a static
                                                       field on the same
                                                       ticker) and add to spot.
                                                       FWD_TENOR is TENOR
                                                       unchanged, except "1Y"
                                                       becomes "12M" (Bloomberg
                                                       quirk, confirmed live -
                                                       "EURUSD1Y Curncy" is an
                                                       invalid security, but
                                                       "EURUSD12M Curncy" is
                                                       exactly the 1Y point).

All vol figures are in vol points (5.11 means 5.11%), RR/BF in the same
units. PAIR is written the way Bloomberg/the market actually quotes it (base,
quote) - see G10_PAIRS - matching the direction universe.py's FX category
already uses for the 4 pairs it carries (a "call on the base currency" is
USD call/JPY put on USD/JPY; a "put on the base currency" is USD put/JPY
call).

Pricing approach and its deliberate approximations
----------------------------------------------------
1. **The smile is recovered from ATM/RR/BF, not read as a smile directly.**
   Bloomberg's market convention (unlike Citi's tag set that some sibling
   tools in this org were built against) only publishes three numbers per
   tenor - ATM, and a risk-reversal + butterfly at each of two deltas (25D,
   10D) - not seven individual smile-point vols. The standard conversion
   (Reiswich & Wystup, and consistent with Bloomberg's own "BFY" naming,
   which denotes the smile/vega butterfly rather than a broker strangle):

       vol(25D call) = ATM + BF25 + 0.5*RR25
       vol(25D put)  = ATM + BF25 - 0.5*RR25
       vol(10D call) = ATM + BF10 + 0.5*RR10
       vol(10D put)  = ATM + BF10 - 0.5*RR10

   giving 5 smile points per tenor: P10, P25, ATM, C25, C10.

2. **Strikes are recovered by inverting the (forward) delta formula**, since
   nothing here publishes an actual strike level for a quoted vol point. For
   a quoted point at (unsigned) delta `d` and vol `sigma`:

       d1 = N^-1(d)         for a call        (delta = N(d1))
       d1 = N^-1(1 - d)     for a put         (delta = N(d1) - 1)
       K  = F * exp(-d1*sigma*sqrt(T) + 0.5*sigma^2*T)

   using the *forward* (Black-76) delta convention by default - not
   premium-adjusted delta, which several pairs (JPY crosses among them) use
   in practice and which moves a strike by a percent or so at typical vol
   levels. Both are available: `strike_from_delta` (plain) and
   `strike_from_delta_premium_adjusted` (Newton's method off the plain
   strike as a starting guess, since premium-adjusted delta has no closed
   form and is not even monotonic in strike); `build_tenor_smile`'s
   `premium_adjusted` flag picks which one.

3. **ATM strike is approximated as the forward.** The true ATM convention is
   the delta-neutral straddle strike, `F * exp(0.5 * sigma_ATM^2 * T)`. For
   the tenors this module brackets (weeks to ~2 years) that correction is a
   few pips to a percent at most - negligible next to the option's own
   bid/ask - so the ATM point is plotted at the forward outright itself.

4. **Domestic (quote-currency) discounting is dropped (r_d = 0).** The
   quoted forward already embeds the *rate differential* through covered
   interest rate parity, so pricing off Black-76 in the forward measure only
   drops the *discount factor*, e^(-r_d*T) - for an option days to a couple
   of years out that's worth basis points on the premium, not real money,
   next to the option's own bid/ask. (Unlike some sibling tools in this org,
   Bloomberg *does* carry live deposit/OIS curves per currency - a future
   version could discount properly - but that's a second project, not a
   free upgrade folded into this one.)

5. **Time interpolation is flat-forward (linear in total variance).** The
   requested expiry almost never lands exactly on one of Bloomberg's quoted
   tenors, so this brackets it between the two nearest tenors and
   interpolates `sigma^2 * T` linearly between them - the standard way to
   interpolate an implied-vol term structure without manufacturing a
   calendar-spread arbitrage. The forward itself is interpolated linearly in
   the same fraction.

Every value this module returns traces back to a live Bloomberg tag plus one
of the five approximations above; none of it is calibrated or fitted beyond
the natural cubic spline used to draw/interpolate the smile in strike space.
"""

from __future__ import annotations

import datetime
import math

# The 9 USD-legged G10 majors, plus several genuine crosses with no USD leg -
# all 14 confirmed live (spot + V/RR/BF + forward-points families all
# resolve) in exactly this (base, quote) direction.
G10_PAIRS = [
    ("EUR", "USD"), ("GBP", "USD"), ("AUD", "USD"), ("NZD", "USD"),
    ("USD", "JPY"), ("USD", "CHF"), ("USD", "CAD"), ("USD", "SEK"), ("USD", "NOK"),
    ("CHF", "JPY"), ("EUR", "JPY"), ("GBP", "CHF"), ("EUR", "GBP"), ("EUR", "CHF"),
]

# Bloomberg's quoted tenor ladder for FX vol/RR/BF - confirmed live for
# EURUSD; ON through 3M plus 4M/6M/9M/1Y/18M/2Y (no 5Y here - unconfirmed,
# and this module brackets rather than extrapolates far past 2Y anyway).
TENORS = ["ON", "1W", "2W", "3W", "1M", "2M", "3M", "4M", "6M", "9M", "1Y", "18M", "2Y"]

# (u, label) on a 0-100 delta-ish axis, so a 10-delta put sits left of ATM
# and a 10-delta call sits right of it - purely for chart layout, not used
# in pricing.
SMILE_POINTS = [(10, "P10"), (25, "P25"), (50, "ATM"), (75, "C25"), (90, "C10")]


def pair_key(base: str, quote: str) -> str:
    return f"{base}{quote}"


def pair_label(base: str, quote: str) -> str:
    return f"{base}/{quote}"


def parse_pair(text: str):
    """(base, quote) for a G10 pair given as "EURUSD", "EUR/USD", "eur.usd",
    etc. Raises ValueError (with the valid list) for anything else."""
    normalized = text.strip().upper().replace("/", "").replace(".", "").replace("-", "").replace(" ", "")
    for base, quote in G10_PAIRS:
        if normalized == pair_key(base, quote):
            return base, quote
    choices = ", ".join(pair_key(b, q) for b, q in G10_PAIRS)
    raise ValueError(f"unsupported pair {text!r} - choose one of {choices}")


def spot_tag(base: str, quote: str) -> str:
    return f"{pair_key(base, quote)} Curncy"


def atm_tag(base: str, quote: str, tenor: str) -> str:
    return f"{pair_key(base, quote)}V{tenor} Curncy"


def rr_tag(base: str, quote: str, tenor: str, delta: str) -> str:
    return f"{pair_key(base, quote)}{delta}R{tenor} Curncy"


def bf_tag(base: str, quote: str, tenor: str, delta: str) -> str:
    return f"{pair_key(base, quote)}{delta}B{tenor} Curncy"


def _fwd_tenor(tenor: str) -> str:
    """Bloomberg quirk, confirmed live: the forward-*points* ticker uses
    "12M" for the 1-year point, not "1Y" (which is invalid) - every other
    tenor (including 18M/2Y) is unchanged."""
    return "12M" if tenor == "1Y" else tenor


def fwd_tag(base: str, quote: str, tenor: str) -> str:
    return f"{pair_key(base, quote)}{_fwd_tenor(tenor)} Curncy"


def tags_for_tenor(base: str, quote: str, tenor: str):
    """The 6 tags one tenor's smile needs (ATM, 25D/10D RR+BF, forward
    points) - one batched reference_data() call fetches PX_LAST for all of
    them at once."""
    return [
        atm_tag(base, quote, tenor),
        rr_tag(base, quote, tenor, "25"), bf_tag(base, quote, tenor, "25"),
        rr_tag(base, quote, tenor, "10"), bf_tag(base, quote, tenor, "10"),
        fwd_tag(base, quote, tenor),
    ]


def atm_tags(base: str, quote: str):
    """The 13 ATM tags across every quoted tenor - the full term structure,
    independent of any strike or bracket; context for the term-structure
    chart, not a pricing input."""
    return [atm_tag(base, quote, tenor) for tenor in TENORS]


def build_tenor_smile(base, quote, tenor, values, spot, fwd_scale, today, premium_adjusted=False):
    """`values`: {tag: float} of PX_LAST readings, however many of
    tags_for_tenor's 6 tags happen to be in hand. `fwd_scale`: the pair's
    static FWD_SCALE (decimal places a forward-point ticker is quoted in -
    4 for most pairs, 2 for JPY crosses). Returns {"points", "forward",
    "years"}, sorted ascending by strike, or None if the smile isn't
    fully in hand yet."""
    atm = values.get(atm_tag(base, quote, tenor))
    rr25 = values.get(rr_tag(base, quote, tenor, "25"))
    bf25 = values.get(bf_tag(base, quote, tenor, "25"))
    rr10 = values.get(rr_tag(base, quote, tenor, "10"))
    bf10 = values.get(bf_tag(base, quote, tenor, "10"))
    fwd_points = values.get(fwd_tag(base, quote, tenor))
    if None in (atm, rr25, bf25, rr10, bf10, fwd_points):
        return None

    years = year_frac(today, tenor_date(tenor, today))
    forward = spot + fwd_points / (10 ** fwd_scale)

    invert = strike_from_delta_premium_adjusted if premium_adjusted else strike_from_delta
    smile_vols = {
        "P10": atm + bf10 - 0.5 * rr10,
        "P25": atm + bf25 - 0.5 * rr25,
        "ATM": atm,
        "C25": atm + bf25 + 0.5 * rr25,
        "C10": atm + bf10 + 0.5 * rr10,
    }
    points = []
    for u, label in SMILE_POINTS:
        vol = smile_vols[label]
        if label == "ATM":
            strike = forward
        else:
            delta = int(label[1:]) / 100.0
            strike = invert(forward, vol / 100.0, years, delta, is_call=label.startswith("C"))
        points.append({"u": u, "label": label, "strike": strike, "vol": vol})
    points.sort(key=lambda p: p["strike"])
    return {"points": points, "forward": forward, "years": years}


def build_term_structure(base, quote, atm_values, today):
    """[{"tenor", "date", "years", "vol_pct"}, ...] for every TENORS entry
    with a live ATM value in hand - skips silently over whichever tenors
    aren't cached yet, so the chart just draws through whatever term
    structure is actually known so far."""
    points = []
    for tenor in TENORS:
        vol = atm_values.get(atm_tag(base, quote, tenor))
        if vol is None:
            continue
        date_ = tenor_date(tenor, today)
        points.append({"tenor": tenor, "date": date_.isoformat(),
                        "years": year_frac(today, date_), "vol_pct": vol})
    return points


# --- delta <-> strike inversion, Black-76, spline, tenor arithmetic --------

def _inv_norm_cdf(p: float) -> float:
    """Standard normal inverse CDF via Newton's method - good enough for `p`
    away from the extremes (our deltas run 0.10-0.90), which is all this
    module ever calls it with."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    x = 0.0
    for _ in range(100):
        diff = _norm_cdf(x) - p
        if abs(diff) < 1e-12:
            break
        x -= diff / _phi(x)
    return x


def strike_from_delta(forward, sigma, years, delta, is_call):
    """The strike a quoted (unsigned) `delta` and `sigma` corresponds to,
    inverting the Black-76 (forward) delta formula - see module docstring
    point 2. `sigma` is a decimal, `delta` is unsigned (0.10, not -0.10 for
    a put)."""
    d1 = _inv_norm_cdf(delta if is_call else 1.0 - delta)
    sqrt_t = math.sqrt(years)
    return forward * math.exp(-d1 * sigma * sqrt_t + 0.5 * sigma * sigma * years)


def _pa_signed_delta(forward, strike, years, sigma, is_call):
    """Premium-adjusted delta (Reiswich & Wystup), signed like Black-76's
    own delta: N(d1)-style for a call, negative for a put, but scaled down
    by the strike-to-forward ratio since the premium itself (already in
    quote-currency terms) is subtracted from the raw forward exposure."""
    sqrt_t = math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * sigma * sigma * years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if is_call:
        return (strike / forward) * _norm_cdf(d2)
    return -(strike / forward) * _norm_cdf(-d2)


def strike_from_delta_premium_adjusted(forward, sigma, years, delta, is_call):
    """As strike_from_delta, but inverts premium-adjusted delta instead of
    plain forward delta - the convention several pairs (a number of JPY
    crosses among them) actually quote in practice.

    Premium-adjusted delta is not monotonic in strike (it has a turning
    point, so two strikes can share the same delta) - there is no closed
    form. Newton's method from strike_from_delta's plain-forward-delta
    strike as a starting guess converges to the smaller, standard strike the
    market actually quotes rather than jumping to the far/second root; the
    step is damped (never more than half the current strike per iteration)
    to keep it that way."""
    target = delta if is_call else -delta
    strike = strike_from_delta(forward, sigma, years, delta, is_call)
    for _ in range(50):
        residual = _pa_signed_delta(forward, strike, years, sigma, is_call) - target
        if abs(residual) < 1e-10:
            break
        bump = strike * 1e-6
        residual_bumped = _pa_signed_delta(forward, strike + bump, years, sigma, is_call) - target
        derivative = (residual_bumped - residual) / bump
        if derivative == 0:
            break
        step = residual / derivative
        step = max(-0.5 * strike, min(0.5 * strike, step))
        strike -= step
    return strike


def _phi(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76(forward, strike, years, sigma, is_call):
    """Undiscounted Black-76 (r_d = 0 - see module docstring point 4) price
    and greeks, in the forward measure. `sigma` is a decimal (0.08, not 8)."""
    if years <= 0:
        raise ValueError("time to expiry must be positive")
    if sigma <= 0:
        raise ValueError("volatility must be positive")
    sqrt_t = math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * sigma * sigma * years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    n_d1, n_d2 = _norm_cdf(d1), _norm_cdf(d2)
    if is_call:
        price = forward * n_d1 - strike * n_d2
        delta_f = n_d1
    else:
        price = strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1)
        delta_f = n_d1 - 1.0
    vega = forward * _phi(d1) * sqrt_t                       # per 1.00 (100 vol points) change in sigma
    gamma_f = _phi(d1) / (forward * sigma * sqrt_t)
    theta_annual = -forward * _phi(d1) * sigma / (2.0 * sqrt_t)
    return {"price": price, "delta_f": delta_f, "gamma_f": gamma_f, "vega": vega,
            "theta_annual": theta_annual, "d1": d1, "d2": d2}


class NaturalCubicSpline:
    """A natural cubic spline through (xs, ys), xs strictly ascending. Needs
    at least 3 points. Standard tridiagonal solve for the second
    derivatives, no external dependency."""

    def __init__(self, xs, ys):
        if len(xs) < 3:
            raise ValueError("need at least 3 points for a natural cubic spline")
        self.xs = list(xs)
        self.ys = list(ys)
        n = len(xs)
        h = [xs[i + 1] - xs[i] for i in range(n - 1)]
        alpha = [0.0] * n
        for i in range(1, n - 1):
            alpha[i] = (3.0 / h[i]) * (ys[i + 1] - ys[i]) - (3.0 / h[i - 1]) * (ys[i] - ys[i - 1])
        l = [1.0] * n
        mu = [0.0] * n
        z = [0.0] * n
        for i in range(1, n - 1):
            l[i] = 2.0 * (xs[i + 1] - xs[i - 1]) - h[i - 1] * mu[i - 1]
            mu[i] = h[i] / l[i]
            z[i] = (alpha[i] - h[i - 1] * z[i - 1]) / l[i]
        c = [0.0] * n
        b = [0.0] * n
        d = [0.0] * n
        for j in range(n - 2, -1, -1):
            c[j] = z[j] - mu[j] * c[j + 1]
            b[j] = (ys[j + 1] - ys[j]) / h[j] - h[j] * (c[j + 1] + 2.0 * c[j]) / 3.0
            d[j] = (c[j + 1] - c[j]) / (3.0 * h[j])
        self._b, self._c, self._d = b, c, d

    def __call__(self, x):
        xs = self.xs
        n = len(xs)
        if x <= xs[0]:
            i = 0
        elif x >= xs[-1]:
            i = n - 2
        else:
            i = max(0, min(n - 2, next(k for k in range(n - 1) if xs[k] <= x <= xs[k + 1])))
        dx = x - xs[i]
        return self.ys[i] + self._b[i] * dx + self._c[i] * dx * dx + self._d[i] * dx * dx * dx


def strike_vol(points, strike):
    """(vol, kind) at `strike`, off a natural cubic spline through the
    smile's quoted (strike, vol) pairs. kind is "quoted" at (near enough) a
    quoted strike, "interpolated" inside the quoted range, "extrapolated"
    outside it."""
    xs = [p["strike"] for p in points]
    ys = [p["vol"] for p in points]
    spline = NaturalCubicSpline(xs, ys)
    vol = spline(strike)
    lo, hi = xs[0], xs[-1]
    if any(abs(strike - x) < 1e-6 for x in xs):
        kind = "quoted"
    elif lo <= strike <= hi:
        kind = "interpolated"
    else:
        kind = "extrapolated"
    return vol, kind


def strike_curve(points, n=41):
    """`n` evenly spaced (strike, vol) samples off the same spline, spanning
    a little either side of the quoted range - purely for drawing a smooth
    smile curve; not used for pricing."""
    xs = [p["strike"] for p in points]
    ys = [p["vol"] for p in points]
    spline = NaturalCubicSpline(xs, ys)
    lo, hi = xs[0], xs[-1]
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (n - 1)
    return [{"strike": lo + i * step, "vol": spline(lo + i * step)} for i in range(n)]


def _add_months(d, months):
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, _days_in_month(year, month))
    return d.replace(year=year, month=month, day=day)


def _days_in_month(year, month):
    if month == 12:
        return (datetime.date(year + 1, 1, 1) - datetime.date(year, 12, 1)).days
    return (datetime.date(year, month + 1, 1) - datetime.date(year, month, 1)).days


def tenor_date(tenor, today):
    """The calendar date `tenor` (as quoted, e.g. "1M", "2W") lands on,
    counting from `today`. Ignores spot-date (T+2) conventions - a day or
    two out of a term structure spanning weeks to years, immaterial next to
    the strike-space interpolation this module already does."""
    if tenor == "ON":
        return today + datetime.timedelta(days=1)
    if tenor.endswith("W"):
        return today + datetime.timedelta(weeks=int(tenor[:-1]))
    if tenor.endswith("M"):
        return _add_months(today, int(tenor[:-1]))
    if tenor.endswith("Y"):
        return _add_months(today, int(tenor[:-1]) * 12)
    raise ValueError(f"unrecognised tenor {tenor!r}")


def bracket_tenors(expiry_date, today):
    """The one or two TENORS entries bracketing `expiry_date`: a single
    (tenor, date) if it lands exactly on a quoted tenor, else the two
    straddling it - clamped to the shortest/longest tenor if `expiry_date`
    falls outside the quoted range altogether (extrapolated rather than
    refused)."""
    dated = [(t, tenor_date(t, today)) for t in TENORS]
    for i, (tenor, date_) in enumerate(dated):
        if date_ == expiry_date:
            return [(tenor, date_)]
        if date_ > expiry_date:
            return [dated[0]] if i == 0 else [dated[i - 1], dated[i]]
    return [dated[-2], dated[-1]]


def year_frac(d0, d1):
    return (d1 - d0).days / 365.0


def value_option(base, quote, bracket_data, spot, strike, expiry_date, today, is_call):
    """bracket_data: [(tenor, tenor_date, smile_or_None), ...], 1 or 2
    entries, as returned by bracket_tenors()/build_tenor_smile(). Raises
    ValueError (with a message fit to show the user) if any bracket tenor's
    smile isn't built yet, or if `expiry_date` isn't in the future."""
    years = year_frac(today, expiry_date)
    if years <= 0:
        raise ValueError("expiry date must be in the future")

    legs = []
    for tenor, date_, smile in bracket_data:
        if smile is None:
            raise ValueError(f"{tenor}: waiting for live vol/RR/BF/forward quotes to build the smile")
        vol, kind = strike_vol(smile["points"], strike)
        legs.append({
            "tenor": tenor, "date": date_.isoformat(), "years": year_frac(today, date_),
            "vol_pct": vol, "vol_kind": kind, "forward": smile["forward"],
            "curve": strike_curve(smile["points"]),
            "quoted": [{"strike": p["strike"], "vol": p["vol"], "label": p["label"]} for p in smile["points"]],
        })

    if len(legs) == 1:
        leg = legs[0]
        sigma = leg["vol_pct"] / 100.0
        forward = leg["forward"]
    else:
        near, far = legs
        var_near = (near["vol_pct"] / 100.0) ** 2 * near["years"]
        var_far = (far["vol_pct"] / 100.0) ** 2 * far["years"]
        frac = (years - near["years"]) / (far["years"] - near["years"])
        total_var = var_near + (var_far - var_near) * frac
        sigma = math.sqrt(max(total_var, 0.0) / years)
        forward = near["forward"] + (far["forward"] - near["forward"]) * frac

    priced = black76(forward, strike, years, sigma, is_call)
    forward_over_spot = forward / spot
    # Premium as a percentage of the *call currency's* notional - the
    # market's usual way to size premium independent of the notional's
    # currency. A put on the base currency calls the quote currency: 1 unit
    # of base notional is worth `strike` units of quote at the strike
    # itself, so %d = price / strike. A call on the base currency calls the
    # base currency instead: %f = price / spot.
    call_ccy = base if is_call else quote
    premium_pct_call_ccy = priced["price"] / (spot if is_call else strike) * 100.0
    return {
        "pair": pair_label(base, quote), "base": base, "quote": quote,
        "is_call": is_call, "strike": strike,
        "expiry": expiry_date.isoformat(), "days_to_expiry": (expiry_date - today).days,
        "years_to_expiry": years, "spot": spot, "forward": forward, "sigma_pct": sigma * 100.0,
        "price": priced["price"],
        "call_ccy": call_ccy, "premium_pct_call_ccy": premium_pct_call_ccy,
        "delta": priced["delta_f"] * forward_over_spot,
        "smile_delta": priced["delta_f"],
        "gamma": priced["gamma_f"] * forward_over_spot ** 2,
        "vega_per_vol_point": priced["vega"] / 100.0,
        "theta_per_day": priced["theta_annual"] / 365.0,
        "legs": legs,
    }
