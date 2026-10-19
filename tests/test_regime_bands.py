"""The strip reports what CHANGED about a reading, not that it is still there.

Two defects, one file, both about a number printed without the qualification
that makes it mean something.

F5 — one 45-calendar-day staleness threshold served both a continuously-priced
row and a monthly macro print, so the live half could never go stale. Housing
starts got its bold marker at 57 days; a four-day-old equity close never would.

rates_up — `DRIVER_METRIC` fired on RESIDENCE in a tail. The US 30y line led
the brief for seven straight mornings saying the same thing, and on 2026-08-27
it reported "hurts" on a day the yield FELL, 5.23% -> 5.17%, both inside the
top decile. `etf-macro.yaml`'s own caveat had said to "report it when the
newsletters or the strip show a real move at the long end" since 2026-08-20.
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from regime_strip import (  # noqa: E402
    _band, _bdays, _is_stale, _since_words, band_events, extremes,
)

MON = date(2026, 8, 24)
THU = date(2026, 8, 27)


def _m(mid="long_rate", pct=98, asof=THU, lagged=False, label="US 30-year borrowing cost"):
    return {"id": mid, "label": label, "note": "30-year Treasury yield",
            "value": " 5.17%", "pct": pct, "tail": "", "asof": asof,
            **({"lagged": True} if lagged else {})}


# --------------------------------------------------------------- F5: staleness

def test_bdays_skips_the_weekend():
    # Monday reading Friday's close is one business day old, not three. A
    # calendar threshold is what made Monday look like a data outage.
    assert _bdays(date(2026, 8, 21), MON) == 1
    assert _bdays(MON, MON) == 0
    assert _bdays(THU, MON) == 0          # future asof never goes negative


def test_live_row_goes_stale_in_business_days():
    assert not _is_stale(_m(asof=date(2026, 8, 26)), THU)     # 1 bday
    assert not _is_stale(_m(asof=date(2026, 8, 24)), THU)     # 3 bdays
    assert _is_stale(_m(asof=date(2026, 8, 21)), THU)         # 4 bdays


def test_lagged_macro_keeps_the_calendar_threshold():
    # The old single threshold. A monthly print is not stale at four days, and
    # housing starts at 57 days still has to carry the marker.
    assert not _is_stale(_m(asof=date(2026, 8, 21), lagged=True), THU)
    assert not _is_stale(_m(asof=date(2026, 7, 20), lagged=True), THU)   # 38d
    assert _is_stale(_m(asof=date(2026, 7, 1), lagged=True), THU)        # 57d


def test_a_live_row_four_business_days_old_would_not_have_been_stale_before():
    # The mutant: restore the old rule and this row passes as fresh.
    m = _m(asof=date(2026, 8, 21))
    assert (THU - m["asof"]).days <= 45      # what the old code asked
    assert _is_stale(m, THU)                 # what it should have asked


# ------------------------------------------------------------ bands and events

def test_band_edges():
    assert _band(10) == "low" and _band(11) == "mid"
    assert _band(90) == "high" and _band(89) == "mid"
    assert _band(None) is None


def test_residence_alone_reports_nothing():
    """The seven-mornings bug, in one assertion."""
    by_id = {"long_rate": _m(pct=98)}
    prior = {"long_rate": {"band": "high", "since": "2026-08-20", "peak": 99}}
    events, state = band_events(by_id, prior, THU)
    assert events == {}
    assert state["long_rate"]["since"] == "2026-08-20"   # residence not reset


def test_a_falling_rate_inside_the_tail_is_silent():
    """2026-08-27 exactly: 99th -> 98th percentile, still top decile, no event.

    The old rule reported `hurts` for four holdings on a day rates fell.
    """
    prior = {"long_rate": {"band": "high", "since": "2026-08-20", "peak": 99}}
    events, _ = band_events({"long_rate": _m(pct=98)}, prior, THU)
    assert "long_rate" not in events


def test_entering_the_tail_fires():
    prior = {"long_rate": {"band": "mid", "since": "2026-08-01", "peak": None}}
    events, state = band_events({"long_rate": _m(pct=91)}, prior, THU)
    assert events["long_rate"]["event"] == "entered"
    assert events["long_rate"]["since"] == THU.isoformat()
    assert state["long_rate"]["peak"] == 91


def test_a_hair_deeper_is_not_a_fresh_extreme():
    """percentile_rank returns a float. Without a margin, 98.49 -> 98.51 is a
    "new high" and the daily repetition walks straight back in."""
    prior = {"long_rate": {"band": "high", "since": "2026-08-20", "peak": 98.49}}
    events, state = band_events({"long_rate": _m(pct=98.51)}, prior, THU)
    assert events == {}
    assert state["long_rate"]["peak"] == 98.49      # the old peak stands


def test_a_fresh_extreme_inside_the_tail_fires():
    prior = {"long_rate": {"band": "high", "since": "2026-08-20", "peak": 98}}
    events, state = band_events({"long_rate": _m(pct=100)}, prior, THU)
    assert events["long_rate"]["event"] == "deepened"
    assert state["long_rate"]["peak"] == 100
    assert state["long_rate"]["since"] == "2026-08-20"   # the stay continues


def test_deepening_is_direction_aware_in_the_low_tail():
    prior = {"houst": {"band": "low", "since": "2026-07-01", "peak": 3}}
    lower = band_events({"houst": _m("houst", pct=1, lagged=True)}, prior, THU)[0]
    assert lower["houst"]["event"] == "deepened"
    higher = band_events({"houst": _m("houst", pct=8, lagged=True)}, prior, THU)[0]
    assert "houst" not in higher


def test_leaving_the_tail_fires_once():
    prior = {"long_rate": {"band": "high", "since": "2026-08-20", "peak": 100}}
    events, state = band_events({"long_rate": _m(pct=85)}, prior, THU)
    assert events["long_rate"]["event"] == "exited"
    assert events["long_rate"]["prev"] == "high"
    assert state["long_rate"] == {"band": "mid", "since": THU.isoformat(), "peak": None}
    # The next morning, still mid: silent.
    assert band_events({"long_rate": _m(pct=85)}, state, THU)[0] == {}


def test_cold_start_reports_but_does_not_claim_a_move():
    """A cache miss must not silently suppress every callout — and must not
    claim a crossing it cannot see. The first Actions run after this ships has
    no regime-state.json in the cache; the 30y has sat in the top decile for
    weeks, and "moved into" would be a false statement about it."""
    events, _ = band_events({"long_rate": _m(pct=98)}, {}, THU)
    assert events["long_rate"]["event"] == "resumed"


def test_a_real_crossing_is_not_filed_as_a_cold_start():
    prior = {"long_rate": {"band": "mid", "since": "2026-08-01", "peak": None}}
    events, _ = band_events({"long_rate": _m(pct=98)}, prior, THU)
    assert events["long_rate"]["event"] == "entered"


def test_cold_start_in_mid_says_nothing():
    assert band_events({"long_rate": _m(pct=50)}, {}, THU)[0] == {}


def test_since_words():
    assert _since_words(THU.isoformat(), THU) == "since today"
    assert _since_words("2026-08-20", THU) == "since 20 Aug"
    assert _since_words(None, THU) == "for an unknown span"
    assert _since_words("not-a-date", THU) == "for an unknown span"


# ------------------------------------------------------------- end to end

ETF_YAML = """
etfs:
  IE00B4L5Y983:
    name: World Index ETF
    drivers:
      - name: rates_up
        effect: hurts
        basis: confirmed
        channel: longest duration in the book
"""


@pytest.fixture
def home(tmp_path):
    (tmp_path / "etf-macro.yaml").write_text(ETF_YAML)
    return str(tmp_path)


def _state(home, band, since, peak):
    import json
    with open(os.path.join(home, "regime-state.json"), "w") as fh:
        json.dump({"long_rate": {"band": band, "since": since, "peak": peak}}, fh)


def test_extremes_is_silent_on_an_unchanged_extreme(home):
    _state(home, "high", "2026-08-20", 99)
    assert extremes([_m(pct=98)], home, THU) == []


def test_extremes_names_the_holding_on_entry(home):
    _state(home, "mid", "2026-08-01", None)
    lines = extremes([_m(pct=98)], home, THU)
    assert any("moved into the top decile" in ln for ln in lines)
    assert any("hurts **World Index ETF**" in ln for ln in lines)


def test_extremes_on_a_cold_start_discloses_that_it_cannot_see_the_move(home):
    """Tomorrow's first run, exactly: state file absent from the cache."""
    lines = extremes([_m(pct=98)], home, THU)
    assert any("No prior band on record" in ln for ln in lines)
    assert not any("moved into" in ln for ln in lines)
    # It still reaches the holding — disclosure, not suppression.
    assert any("hurts **World Index ETF**" in ln for ln in lines)


def test_extremes_reports_the_driver_standing_down(home):
    _state(home, "high", "2026-08-20", 100)
    lines = extremes([_m(pct=80)], home, THU)
    assert any("has left the top decile" in ln for ln in lines)
    assert any("stood down" in ln for ln in lines)
    # No helps/hurts claim is invented for an absence.
    assert not any("hurts" in ln or "helps" in ln for ln in lines)


def test_extremes_persists_state_for_the_next_run(home):
    import json
    _state(home, "mid", "2026-08-01", None)
    extremes([_m(pct=98)], home, THU)
    saved = json.load(open(os.path.join(home, "regime-state.json")))
    assert saved["long_rate"] == {"band": "high", "since": THU.isoformat(), "peak": 98}
    # Second run, same reading: the callout does not repeat.
    assert extremes([_m(pct=98)], home, THU) == []
