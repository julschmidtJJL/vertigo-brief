"""D-8 regime strip: raw market and macro numbers, no interpretation.

Premise 3 of the plan of record is "no dip prediction". This does NOT forecast,
score, or label a regime. It replaces "feels heated up, like now" with numbers
that can be checked, and leaves the call to the reader.

Every metric degrades independently — one dead ticker never takes the strip down.
"""
import json, os, sys
from datetime import date, timedelta
import pandas as pd
from yf_util import series, percentile_rank
from fred_util import fred

HOME = os.environ.get("VERTIGO_HOME", os.path.expanduser("~/.vertigo"))
LOOKBACK_YEARS = 5
PCT_WINDOW = 252 * LOOKBACK_YEARS   # trading days

# An age only means something against how often the series publishes. One
# 45-day threshold served both kinds, so a continuously-priced series could go
# a week without a new bar and never be marked stale -- the 2026-08-25 IUSQ.DE
# gap exactly. Live series are judged in BUSINESS days (a Monday reading
# Friday's close is 1 business day old, not 3); scheduled macro keeps a
# calendar threshold because its cadence is monthly.
STALE_LIVE_BDAYS = 3
STALE_LAGGED_DAYS = 45

# How much further into the tail counts as a FRESH extreme rather than noise.
# percentile_rank returns a float, so 98.49 -> 98.51 is a "new high" and the
# daily repetition this change removes would walk straight back in. One
# percentile point of a 5y window is ~12 trading days of history cleared.
DEEPEN_MIN_PCT = 1.0

# Cross-run memory for band transitions. Regenerable, gitignored, and carried
# between Actions runs by the same cache that holds falsifier-state.json.
REGIME_STATE_FILE = "regime-state.json"


def _ordinal(n):
    n = int(round(n))
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _bar(p, cells=10):
    """Percentile as a 10-cell bar. The point of the strip is seeing WHERE you
    are; a bar says that without the eye having to parse digits.

    Uses █ and ░ — both Block Elements with the same advance width — so the
    bar's length still encodes the percentile in a PROPORTIONAL font. The
    earlier `·` filler was narrow, which made a 20th and an 80th look similar
    once the output moved to rendered Markdown.
    """
    if p is None:
        return "░" * cells
    filled = max(0, min(cells, int(round(p / 100.0 * cells))))
    return "█" * filled + "░" * (cells - filled)


def _age(asof, today):
    d = (today - asof).days
    if d <= 0:
        return "today"
    if d == 1:
        return "1d"
    return f"{d}d"


def vix():
    """Implied volatility of the S&P 500. High = the options market is paying up
    for protection."""
    s = series("^VIX", period="10y", adjust=False)
    if s is None or len(s) < 60:
        return None
    w = s.tail(PCT_WINDOW)
    cur = float(s.iloc[-1])
    return {
        "id": "vix",
        "label": "Expected volatility",
        "note": "S&P 500, next 30 days (VIX)",
        "value": f"{cur:>8.2f}",
        "pct": percentile_rank(w, cur),
        "tail": f"5y {float(w.min()):.0f}–{float(w.max()):.0f}",
        "asof": s.index[-1].date(),
    }


def spx_vs_200dma():
    """Distance of the S&P 500 from its 200-day moving average. Uses the index
    (^GSPC) rather than SPY: a 200DMA is an index concept and this avoids the
    ETF's tracking and distribution artifacts."""
    s = series("^GSPC", period="10y", adjust=False)
    if s is None or len(s) < 250:
        return None
    ma = s.rolling(200).mean()
    dist = (s / ma - 1.0).dropna()
    if not len(dist):
        return None
    cur = float(dist.iloc[-1])
    return {
        "id": "spx_200d",
        "label": "US market vs its 200-day trend",
        "note": "S&P 500 (^GSPC)",
        "value": f"{cur*100:>+7.1f}%",
        "pct": percentile_rank(dist.tail(PCT_WINDOW), cur),
        "tail": "",
        "asof": s.index[-1].date(),
    }


def hy_spread():
    """US high-yield option-adjusted spread — the ACTUAL credit spread.

    Replaced the HYG/IEF ETF ratio on 2026-08-17. That proxy needed a 200-day
    mean to cancel a mechanical drift (both constructions trend: price-only
    down because HYG distributes far more than IEF, total-return up because
    HYG's higher yield compounds faster). FRED publishes the real measurement,
    keyless and daily, so the proxy and its workaround are both retired.

    Wider = credit stress. Spreads tend to widen ahead of equity turning points
    more often than not, which is why this earns a place.
    """
    s = fred("BAMLH0A0HYM2")
    if s is None or len(s) < 250:
        return None
    cur = float(s.iloc[-1])
    return {
        "id": "hy_oas",
        "label": "Junk-bond risk premium",
        "note": "US high yield, option-adjusted spread",
        "value": f"{cur:>7.2f}%",
        "pct": percentile_rank(s.tail(PCT_WINDOW), cur),
        "tail": "",
        "asof": s.index[-1].date(),
    }


def yield_curve():
    """10-year minus 2-year Treasury. Negative = inverted, the single
    most-studied recession indicator. Daily, so it is never stale."""
    s = fred("T10Y2Y")
    if s is None or len(s) < 250:
        return None
    cur = float(s.iloc[-1])
    return {
        "id": "curve_10y2y",
        "label": "Yield curve",
        "note": "10-year minus 2-year Treasury",
        "value": f"{cur:>+8.2f}",
        "pct": percentile_rank(s.tail(PCT_WINDOW), cur),
        "tail": "inverted" if cur < 0 else "",
        "asof": s.index[-1].date(),
    }


def long_rate():
    """US 30-year Treasury yield. Added 2026-08-20.

    The strip already had the 10y-2y SPREAD, which is a recession indicator and
    NOT a level: a curve can steepen while long yields fall. That gap is why
    `rates_up` was deliberately left out of the extremes map, and why the
    largest position in the book — a growth-index ETF, the longest-duration
    equity held — had a rates driver that could only ever be qualitative.

    On 2026-08-19 the brief reported 30-year Treasuries at 5.33% by quoting
    Handelsblatt. A number that steers a driver on the largest position
    should be computed here, not read out of a newsletter.
    """
    s = fred("DGS30")
    if s is None or len(s) < 250:
        return None
    cur = float(s.iloc[-1])
    return {
        "id": "long_rate",
        "label": "US 30-year borrowing cost",
        "note": "30-year Treasury yield",
        "value": f"{cur:>7.2f}%",
        "pct": percentile_rank(s.tail(PCT_WINDOW), cur),
        "tail": "",
        "asof": s.index[-1].date(),
    }


def acwi_drawdown():
    """Drawdown of the benchmark the portfolio is actually measured against
    (IUSQ.DE, EUR), from its all-time high in the fetched window."""
    s = series("IUSQ.DE", period="10y", adjust=False)
    if s is None or len(s) < 60:
        return None
    ath = float(s.max())
    ath_date = s.idxmax().date()
    cur = float(s.iloc[-1])
    dd = cur / ath - 1.0
    return {
        "id": "acwi_drawdown",
        "label": "Benchmark below its high",
        "note": "ACWI in EUR (IUSQ.DE)",
        "value": f"{dd*100:>+7.1f}%",
        "pct": None,               # a drawdown percentile is not meaningful
        "tail": f"high {ath_date:%d %b}",
        "asof": s.index[-1].date(),
    }


def financial_conditions():
    """Chicago Fed National Financial Conditions Index. Purpose-built for
    'how stressed is the system'. Positive = tighter than average, negative =
    looser. Weekly, so it lags market data by up to a week."""
    s = fred("NFCI")
    if s is None or len(s) < 100:
        return None
    cur = float(s.iloc[-1])
    return {
        "id": "nfci",
        "label": "System-wide financial stress",
        "note": "Chicago Fed NFCI",
        "value": f"{cur:>+8.3f}",
        "pct": percentile_rank(s.tail(260 * LOOKBACK_YEARS // 52 * 52 or len(s)), cur),
        "tail": "tighter than avg" if cur > 0 else "looser than avg",
        "asof": s.index[-1].date(),
        "lagged": True,
    }


def housing_starts():
    """US housing starts, thousands of units, SAAR.

    Included as FUNDAMENTAL data for building-materials holdings, not as
    macro wallpaper: US housing starts are a direct demand driver for them. Monthly and released with a lag, so the observation date matters
    more than the level.
    """
    s = fred("HOUST")
    if s is None or len(s) < 24:
        return None
    cur = float(s.iloc[-1])
    yoy = None
    try:
        prev = s[s.index <= s.index[-1] - pd.Timedelta(days=350)]
        if len(prev):
            yoy = cur / float(prev.iloc[-1]) - 1.0
    except Exception:                                # noqa: BLE001
        pass
    note = ""
    if yoy is not None:
        note = f"{yoy*100:+.1f}% y/y"
    return {
        "id": "houst",
        "label": "New US home construction",
        "note": "US housing starts, annualised",
        "value": f"{cur:>7,.0f}k",
        "pct": percentile_rank(s.tail(60), cur),
        "tail": note,
        "asof": s.index[-1].date(),
        "lagged": True,
    }


# --- extremes: which holdings a reading at a 5y edge actually reaches ---------
#
# Added 2026-08-19, from reading the 2026-08-18 brief. US housing starts printed
# at the 3rd percentile of five years and -13.5% y/y — the confirmed driver on
# the largest stock holding — and it reached the reader as a subordinate
# clause inside a paragraph about earnings. The number was there; its weight was
# not. A model asked to judge prominence will bury this again, so Python names
# it instead and the AI half gets a fact rather than a decision.
#
# Only drivers with an HONEST single-series mapping appear here. `rates_up` is
# deliberately absent: the strip carries the 10y-2y SPREAD, and a curve
# steepening is not the same thing as rates rising. Mapping it would produce a
# confident line that is sometimes simply wrong.
EXTREME_PCT = 10        # at or beyond the 10th/90th percentile of five years

DRIVER_METRIC = {
    # driver name            metric id      tail that ACTIVATES it
    "us_housing_starts_fall": ("houst", "low"),
    "hy_spread_widens": ("hy_oas", "high"),
    "financial_conditions_tighten": ("nfci", "high"),
    # Mappable as of 2026-08-20, now that a long-rate LEVEL is in the strip.
    # It was withheld while only the 10y-2y spread existed, because a steepening
    # curve is not the same event as rates rising.
    "rates_up": ("long_rate", "high"),
}


def _theses(home):
    try:
        import yaml
        return (yaml.safe_load(open(os.path.join(home, "theses.yaml"))) or {}).get("theses") or []
    except Exception:
        return []


def _etfs(home):
    """~/.vertigo/etf-macro.yaml. The passive sleeve carries reviewed drivers
    too, and it can be most of the book — an extremes callout that reads only
    the conviction journal would have named two stock picks on 2026-08-20
    while silently omitting the largest position held, an index fund, and the
    whole reason the 30-year series was added that morning."""
    try:
        import yaml
        return (yaml.safe_load(open(os.path.join(home, "etf-macro.yaml"))) or {}).get("etfs") or {}
    except Exception:
        return {}


def _falsifier_status(home):
    """Written by falsifier_monitor.py earlier in the same run."""
    try:
        with open(os.path.join(home, "falsifier-state.json")) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _bdays(asof, today):
    """Weekdays strictly after `asof`, up to and including `today`.

    Market holidays are not modelled: a holiday buys one extra day of grace,
    which errs toward silence rather than toward a false stale mark.
    """
    if today <= asof:
        return 0
    n, d = 0, asof
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _is_stale(m, today):
    """F5. A continuously-priced row and a monthly macro print do not go stale
    on the same clock, and judging both at 45 calendar days meant the live half
    never went stale at all."""
    if m.get("lagged"):
        return (today - m["asof"]).days > STALE_LAGGED_DAYS
    return _bdays(m["asof"], today) > STALE_LIVE_BDAYS


def _band(pct):
    """Which tail of the 5y distribution a reading sits in."""
    if pct is None:
        return None
    if pct <= EXTREME_PCT:
        return "low"
    if pct >= 100 - EXTREME_PCT:
        return "high"
    return "mid"


def _tail_words(band):
    # EXTREME_PCT is 10, so the tail IS a decile. Say the word a reader knows.
    return "top decile" if band == "high" else "bottom decile"


def _since_words(since, today):
    if not since:
        return "for an unknown span"
    try:
        d = date.fromisoformat(since)
    except (TypeError, ValueError):
        return "for an unknown span"
    return "since today" if d >= today else f"since {d:%-d %b}"


def _load_regime_state(home):
    """{metric_id: {band, since, peak}}. Fail-soft exactly like the falsifier
    monitor's: a missing or corrupt file costs transition detection for one run
    and nothing else."""
    try:
        with open(os.path.join(home, REGIME_STATE_FILE)) as fh:
            return json.load(fh) or {}
    except Exception:                                # noqa: BLE001
        return {}


def _save_regime_state(home, state):
    try:
        with open(os.path.join(home, REGIME_STATE_FILE), "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
    except Exception as e:                           # noqa: BLE001
        print(f"  regime: could not persist {REGIME_STATE_FILE}: {type(e).__name__}",
              file=sys.stderr)


def band_events(by_id, prior, today):
    """What CHANGED about each reading's band since the last run.

    The strip used to call out every metric SITTING in a tail, so the US 30y
    line led the brief for seven consecutive mornings saying the same thing,
    and on 2026-08-27 it reported `rates_up` as "hurts" on a day the yield
    FELL (5.23% -> 5.17%, both inside the top decile). A driver named for a
    move has to fire on a move.

    `etf-macro.yaml`'s own caveat already said so -- "report it when the
    newsletters or the strip show a real move at the long end" -- and the code
    never implemented it. This is that rule.

    Returns ({metric_id: {event, since, prev}}, new_state). Four outcomes:

      entered   crossed into a tail since the last run
      deepened  still in the tail, at a new 5y extreme for this stay
      exited    left a tail it was in
      resumed   in a tail, with NO prior band on record
      (absent)  unchanged -- say nothing

    `resumed` is the cold start: no state file, or a metric never seen before.
    It reports, so a cache miss fails toward reporting rather than silence, but
    it is kept distinct from `entered` because the strip does not know whether
    the reading just crossed or has sat there for six weeks. Claiming a move it
    cannot see is the failure this whole change exists to remove.
    """
    events, new_state = {}, {}
    for mid, m in by_id.items():
        pct = m["pct"]
        band = _band(pct)
        was = prior.get(mid) or {}
        prev_band, peak, since = was.get("band"), was.get("peak"), was.get("since")
        event = None

        if prev_band == band:
            if band in ("low", "high"):
                if peak is None:
                    event, peak = "deepened", pct
                elif (pct <= peak - DEEPEN_MIN_PCT if band == "low"
                      else pct >= peak + DEEPEN_MIN_PCT):
                    event, peak = "deepened", pct
        elif band in ("low", "high"):
            event = "entered" if prev_band else "resumed"
            peak, since = pct, today.isoformat()
        elif prev_band in ("low", "high"):
            event, peak, since = "exited", None, today.isoformat()
        else:
            peak = None

        since = since or today.isoformat()
        if event:
            events[mid] = {"event": event, "since": since, "prev": prev_band}
        new_state[mid] = {"band": band, "since": since, "peak": peak}
    return events, new_state


def extremes(metrics, home, today=None):
    """Lines naming every 5y-extreme reading that a REVIEWED driver points at a
    holding, with that holding's weight and whether any rule is near firing.

    Grouped BY METRIC, one header per extreme reading and one bullet per
    holding it touches. The flat one-sentence-per-pair form restated the metric
    in full every time: on 2026-08-22 the 30y Treasury line appeared four times
    over, identical up to the holding name. Same information, four times the
    reading. The metric is the finding; the holdings are what it reaches.

    Reported on a CHANGE of band, never on residence in one. See band_events.
    """
    by_id = {m["id"]: m for m in metrics if m.get("pct") is not None}
    status = _falsifier_status(home)
    today = today or date.today()

    events, new_state = band_events(by_id, _load_regime_state(home), today)
    _save_regime_state(home, new_state)

    hits = {}           # metric id -> list of (company, effect, reach)
    stood_down = {}     # metric id -> list of company, for readings that left

    def add(mid, company, effect, reach):
        hits.setdefault(mid, []).append((company, effect, reach))

    for t in _theses(home):
        drivers = [d for d in (t.get("macro_sensitivity") or [])
                   if d.get("basis") in ("stated", "confirmed")]
        for d in drivers:
            mapped = DRIVER_METRIC.get(d.get("name"))
            if not mapped:
                continue
            mid, active_tail = mapped
            m = by_id.get(mid)
            if not m:
                continue
            if mid not in events:
                continue
            if events[mid]["event"] == "exited":
                stood_down.setdefault(mid, []).append(t["company"])
                continue
            band = _band(m["pct"])
            low, high = band == "low", band == "high"
            # An extreme in the tail that activates the driver bears the way the
            # driver says; the opposite tail bears the other way. Saying only
            # "extreme" would lose the sign, which is the whole point.
            activating = (low and active_tail == "low") or (high and active_tail == "high")
            effect = d.get("effect", "unclear")
            if not activating:
                effect = {"hurts": "helps", "helps": "hurts"}.get(effect, effect)

            fids = [f["id"] for f in (t.get("falsifiers") or [])]
            near = [f for f in fids if status.get(f, {}).get("status") in ("triggered", "closing")]
            n_manual = sum(1 for f in (t.get("falsifiers") or []) if f.get("check") == "manual")
            # Bullet-length, not sentence-length. These read in a list now, and
            # "none of its falsifiers is near firing, and its 1 manual one
            # needs a filing" is a lot of words to say nothing has changed.
            if near:
                reach = f"**{len(near)} falsifier{'s' if len(near) != 1 else ''} near firing**"
            else:
                reach = "no falsifier near firing"
                if n_manual:
                    reach += f" · {n_manual} manual check{'s' if n_manual != 1 else ''} pending"

            add(mid, t["company"], effect, reach)

    # The passive sleeve. No falsifiers by design. The old wording spelled that
    # out as "nothing here will ever fire on it" — a sentence that announces its
    # own inertness at full length, on every line, forever. Which ETF a driver
    # touches is real; the disclaimer is said once, in the group header.
    for isin, e in _etfs(home).items():
        for d in (e.get("drivers") or []):
            if d.get("basis") not in ("stated", "confirmed"):
                continue
            mapped = DRIVER_METRIC.get(d.get("name"))
            if not mapped:
                continue
            mid, active_tail = mapped
            m = by_id.get(mid)
            if not m:
                continue
            if mid not in events:
                continue
            if events[mid]["event"] == "exited":
                stood_down.setdefault(mid, []).append(e.get("name", isin))
                continue
            band = _band(m["pct"])
            low, high = band == "low", band == "high"
            activating = (low and active_tail == "low") or (high and active_tail == "high")
            effect = d.get("effect", "unclear")
            if not activating:
                effect = {"hurts": "helps", "helps": "hurts"}.get(effect, effect)
            add(mid, e.get("name", isin), effect, "index fund, no falsifiers")

    lines = []
    for mid, touched in hits.items():
        m = by_id[mid]
        # A macro reading published 52 days ago still generated a callout worded
        # exactly like a live one. The table already marks it stale; a line that
        # asks you to act on a number must carry the number's age too.
        age = (today - m["asof"]).days
        stale = f", on a reading {age} days old" if _is_stale(m, today) else ""
        band = _band(m["pct"])
        if events[mid]["event"] == "entered":
            head = (f"`ƒ` **{m['label']} moved into the {_tail_words(band)} of "
                    f"{LOOKBACK_YEARS}y — now the {_ordinal(m['pct'])} "
                    f"percentile{stale}.**")
        elif events[mid]["event"] == "resumed":
            head = (f"`ƒ` **{m['label']} is in the {_tail_words(band)} of "
                    f"{LOOKBACK_YEARS}y — {_ordinal(m['pct'])} percentile{stale}.** "
                    f"No prior band on record, so whether this is a new move is "
                    f"unknown.")
        else:
            head = (f"`ƒ` **{m['label']} set a fresh {LOOKBACK_YEARS}y extreme at "
                    f"the {_ordinal(m['pct'])} percentile{stale}** — in the "
                    f"{_tail_words(band)} {_since_words(events[mid]['since'], today)}.")
        lines.append(
            head + f" Reviewed drivers point it at "
            f"{len(touched)} holding{'s' if len(touched) != 1 else ''}:"
        )
        # helps before hurts, then alphabetical: the sign is what the eye wants.
        for company, effect, reach in sorted(touched, key=lambda x: (x[1] != "helps", x[0])):
            lines.append(f"  - {effect} **{company}** · {reach}")

    # Leaving an extreme is news for the same holdings that entering it was.
    # Reported without helps/hurts on purpose: the driver has stood down, and
    # inventing a sign for its absence would be a claim the strip cannot make.
    for mid, companies in stood_down.items():
        m = by_id[mid]
        names = sorted(set(companies))
        lines.append(
            f"`ƒ` **{m['label']} has left the {_tail_words(events[mid]['prev'])} of "
            f"{LOOKBACK_YEARS}y — now the {_ordinal(m['pct'])} percentile.** The "
            f"driver that pointed it at {len(names)} "
            f"holding{'s' if len(names) != 1 else ''} has stood down:"
        )
        for company in names:
            lines.append(f"  - **{company}**")
    return lines


def render():
    out = []
    out.append("")
    # Backticked ƒ to match the falsifier monitor's heading. The two ƒ blocks
    # sit next to each other in the daily brief and a reader should not have to
    # wonder whether two spellings mean two different things.
    out.append("## `ƒ` Regime strip")
    out.append("")
    metrics, failed = [], []
    for fn in (vix, spx_vs_200dma, hy_spread, yield_curve, long_rate, acwi_drawdown,
               financial_conditions, housing_starts):
        try:
            m = fn()
        except Exception as e:                       # noqa: BLE001
            print(f"  regime: {fn.__name__} raised {type(e).__name__}", file=sys.stderr)
            m = None
        (metrics if m else failed).append(m if m else fn.__name__)

    if not metrics:
        out.append("> ⚠️ Regime strip unavailable — every metric failed to fetch.")
        out.append("")
        return "\n".join(out)

    today = date.today()

    def rows(ms):
        r = ["| | level | 5y percentile | as of |", "|---|---:|:--|:--|"]
        for m in ms:
            age = _age(m["asof"], today)
            stale = _is_stale(m, today)
            pct = "—" if m["pct"] is None else f"`{_bar(m['pct'])}` {_ordinal(m['pct'])}"
            tail = m.get("tail", "")
            if stale:
                tail = ("**stale** · " + tail) if tail else "**stale**"
            asof = age + (f" · {tail}" if tail else "")
            # Label is the plain-English description; the ticker or series name
            # goes underneath in small type. "VIX" told the reader nothing they
            # could not already see; the docstrings had the real description all
            # along and only the code ever read them.
            note = m.get("note")
            name = f"{m['label']}<br>`{note}`" if note else m['label']
            r.append(f"| {name} | {m['value'].strip()} | {pct} | {asof} |")
        return r

    live = [m for m in metrics if not m.get("lagged")]
    lag = [m for m in metrics if m.get("lagged")]

    if live:
        out.append("**Market** · priced continuously")
        out.append("")
        out += rows(live)
        out.append("")
    if lag:
        out.append("**Macro** · published on a schedule, already known to the market")
        out.append("")
        out += rows(lag)
        out.append("")

    if failed:
        out.append(f"> Unavailable this run: {', '.join(failed)}")
        out.append("")
    out.append(f"`ƒ` percentile = share of the last {LOOKBACK_YEARS}y of observations at or below today")
    out.append("`ƒ` inputs to your judgement, not a signal — no score, no forecast")
    # Extremes last, so they read as the conclusion of the strip rather than a
    # header competing with it. Fail-soft: a bad theses.yaml costs these lines
    # and nothing else.
    try:
        ex = extremes(metrics, HOME, today)
    except Exception as e:                           # noqa: BLE001
        print(f"  regime: extremes raised {type(e).__name__}", file=sys.stderr)
        ex = []
    if ex:
        out.append("")
        for line in ex:
            out.append(line)
    out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    print(render())
