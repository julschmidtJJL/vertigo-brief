"""Falsifier evaluators for the EXAMPLE portfolio.

Copy to ~/.vertigo/evaluators.py and rewrite for your own theses.yaml. Every
falsifier with `check: auto` needs a function here under the same id, and a
direction in DISTANCE_KIND.

CONTRACT. An evaluator takes the falsifier dict from theses.yaml and returns
a 4-tuple (hit, now, trigger, proximity):

  hit        True / False, or None when it cannot be evaluated
  now        today's reading, as text (shown in the table and logged verbatim)
  trigger    the written condition, as text
  proximity  how close the rule is, scaled so 1.0 means "at the trigger":
               above         current / trigger
               below         trigger / current
               baseline_pct  worst leg / -0.25

The unevaluable path returns (None, reason, None, None).

`data` is injected by falsifier_monitor before this file runs. Call market
data through it (`data._info`, `data._px`, `data._q_avg`) rather than importing
the helpers, so tests can stub the network.
"""


def hei_valuation(_):
    i = data._info("HEI.DE")
    mc, rev, m = i.get("marketCap"), i.get("totalRevenue"), i.get("profitMargins")
    if not (mc and rev and m is not None):
        return None, "missing fields", None, None
    ps = mc / rev
    return (ps > 1.5 and m * 100 <= 8.0), \
        f"P/S {ps:.2f}x · net margin {m*100:.1f}%", \
        "P/S > 1.5 **and** margin ≤ 8%", ps / 1.5


def cat_copper(_):
    q = data._q_avg("HG=F", 2)
    if len(q) < 2:
        return None, "no quarterly data", None, None
    hit = all(v < 3.50 for v in q)
    return hit, f"copper 2Q avg {q[0]:.2f}, {q[1]:.2f}", \
        "both quarters < 3.50", 3.50 / max(q)


def cat_commodities(f):
    b = f.get("baseline") or {}
    cu, oil = data._px("HG=F"), data._px("BZ=F")
    if not (cu and oil and b):
        return None, "missing price or baseline", None, None
    dc, do = cu / b["copper_usd_lb"] - 1, oil / b["brent_usd_bbl"] - 1
    hit = dc <= -0.25 and do <= -0.25
    return hit, f"copper {dc:+.0%} · Brent {do:+.0%}", \
        f"both ≤ −25% vs {b['date']}", max(dc, do) / -0.25


def bn_pe(_):
    pe = data._info("BN.PA").get("trailingPE")
    if not pe:
        return None, "no trailingPE", None, None
    return pe > 25, f"P/E {pe:.1f}", "P/E > 25", pe / 25.0


def sie_below_entry(_):
    px = data._px("SIE.DE")
    if not px:
        return None, "missing price", None, None
    return px < 140, f"EUR {px:.2f}", "below EUR 140", 140 / px


EVALUATORS = {
    "hei-valuation-stretched": hei_valuation,
    "cat-copper-quarters": cat_copper,
    "cat-commodity-bust": cat_commodities,
    "bn-multiple-rerate": bn_pe,
    "sie-below-entry": sie_below_entry,
}

DISTANCE_KIND = {
    "hei-valuation-stretched": "above",
    "cat-copper-quarters": "below",
    "cat-commodity-bust": "baseline_pct",
    "bn-multiple-rerate": "above",
    "sie-below-entry": "below",
}
