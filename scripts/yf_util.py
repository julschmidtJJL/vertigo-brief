"""Shared Yahoo fetch helpers for the brief scripts.

Extracted after the 2026-08-15 failure: yfinance raised on the first ticker and
took the whole weekly run with it. Every fetch in this codebase retries with
backoff and returns None rather than raising.
"""
import sys, time, warnings
warnings.filterwarnings("ignore")
import yfinance as yf
import pandas as pd


def series(ticker, start=None, period=None, adjust=False, attempts=4, quiet=False):
    """Daily closes as a tz-naive pandas Series, or None.

    adjust=False -> split-adjusted, dividend-UNadjusted (price return). Correct
                    when comparing against a broker cost basis.
    adjust=True  -> split- and dividend-adjusted (total return). Correct when
                    comparing two instruments with different yields, e.g. the
                    HYG/IEF credit ratio, where a price-only series drifts down
                    mechanically because HYG distributes far more than IEF.

    Backoff starts at 15s: the 2026-08-15 failure was Yahoo rate-limiting (a 429
    body parsed as JSON with no 'chart' key), and rate limits need tens of
    seconds, not three.
    """
    delay = 15
    kw = {"auto_adjust": adjust}
    if period:
        kw["period"] = period
    else:
        kw["start"] = start
    for n in range(attempts):
        try:
            h = yf.Ticker(ticker).history(**kw)
            if h is not None and not h.empty:
                s = h["Close"].copy()
                s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
                s = s.dropna()
                if len(s):
                    return s
            reason = "empty response"
        except Exception as e:                       # noqa: BLE001 - deliberate
            reason = f"{type(e).__name__}: {e}"
        if n < attempts - 1:
            if not quiet:
                print(f"  retry {n+1}/{attempts-1} {ticker}: {reason}", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    if not quiet:
        print(f"  GIVE UP {ticker}: {reason}", file=sys.stderr)
    return None


def percentile_rank(s, value):
    """Share of observations at or below `value`, as 0-100. None if empty."""
    if s is None or not len(s):
        return None
    return float((s <= value).sum()) / len(s) * 100.0


def choose_asof(last_bars, today, stale_days=5):
    """The newest date on which EVERY series has a settled observation.

    `last_bars` is an iterable of each series' last bar date (None for a series
    that failed to fetch). Returns min() of the ones that are not already
    stale, or `today` when nothing qualifies.

    This exists because a comparison is only valid if both sides cover the same
    span. Yahoo publishes the European venues late: on 2026-08-22 it had a
    Friday bar for the US, .TO and .HK listings and none for IUSQ.DE or anything on
    .DE/.MU/.AS/.PA, so taking each series' own last close measured a basket to
    Friday against a benchmark measured to Thursday.

    Series already staler than `stale_days` do not get a vote. One delisted or
    long-halted ticker must not drag the whole report backwards; those surface
    separately as stale-price warnings.
    """
    votes = [d for d in last_bars
             if d is not None and 0 <= (today - d).days <= stale_days]
    return min(votes) if votes else today


def fx_asof(fx_series, on_date):
    """The FX rate settled on or before `on_date`, and the date it came from.

    Returns `(rate, date)`, or `(None, None)` when the series is missing or has
    no bar that old.

    THE THIRD DATE RULE IN THIS FILE, and it answers a different question from
    the other two. `choose_asof` aligns separate series onto one shared date so
    a COMPARISON is honest. `session_spread` deliberately refuses to align, so a
    VALUE table can show each holding at its own last price and disclose the
    spread. This one is about a single multiplication: `shares x price x rate`
    is one quantity, and its three factors must describe the same moment or the
    product describes no moment at all.

    Before 2026-08-26 the value loop took each FX series' OWN last bar. FX
    trades nearly around the clock while an equity trades on its venue's
    calendar, so the two routinely land on different days: measured that
    afternoon, two North American holdings priced to 2026-08-25 while all three FX pairs
    had a 2026-08-26 bar — an in-progress rate, on a Wednesday lunchtime,
    multiplied into a Tuesday close. Aligning FX to the row's own price date
    does NOT make the row staler; the row was already at that date. It makes
    the product coherent, and it keeps the fix clear of `7e8224b`, which
    established that rows must not be pinned to a common date across holdings.

    The error is small on a quiet day — EUR 0.31 across the whole book that
    afternoon — and unbounded in principle. The largest single-day move in
    USDEUR=X over the preceding year was 1.24%, which on the largest line would be
    tens of euros.
    """
    if fx_series is None or not len(fx_series):
        return None, None
    settled = fx_series[fx_series.index.date <= on_date]
    if not len(settled):
        return None, None
    return float(settled.iloc[-1]), settled.index[-1].date()


def session_spread(bar_dates):
    """The newest last-bar date in a basket, and the earlier ones sitting under it.

    `bar_dates` is an iterable of each row's last bar date, None for a row whose
    fetch failed. Returns `(newest, stale)` where `stale` is the sorted list of
    distinct earlier dates present. An empty `stale` means every row that has a
    date shares it, so an aggregate over the basket covers one real session.
    `(None, [])` when nothing has a date.

    Companion to `choose_asof`, and deliberately NOT the same rule.
    `choose_asof` moves a whole report back to one shared date, which is right
    for a COMPARISON: both sides must cover the same span or the gap is fiction.
    A positions table must not do that. A broker shows every holding at its own
    last traded price, so the table keeps the freshest close per holding
    (the weekly scoreboard — pinning it to a common date put one
    holding at EUR 0.174 against 0.258 on screen) and DISCLOSES the spread instead.

    Same Yahoo behaviour underneath both: on 2026-08-22 and again on 2026-08-25
    there was no bar for the European venues (.DE/.MU/.AS/.PA, IUSQ.DE among
    them) while the US, .TO and .HK listings had one. Twice in three days, and the
    first time it misstated the stock-pick YTD gap by 1.8 points.
    """
    seen = sorted({d for d in bar_dates if d is not None})
    if not seen:
        return None, []
    return seen[-1], seen[:-1]
