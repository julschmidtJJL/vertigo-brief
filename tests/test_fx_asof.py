"""`shares x price x rate` is one quantity, so its factors must share a date.

Regression cover for C4, raised in code review and measured on
2026-08-26: the value loop took each FX series' OWN last bar, so two North American holdings
were priced to the 2026-08-25 close and converted at a 2026-08-26 rate — a
Wednesday-lunchtime, still-moving rate multiplied into a Tuesday close.

The error that afternoon was EUR 0.31 across the whole book, because EUR/USD
barely moved overnight. The point is that it is unbounded, not that it was
large: the worst single-day move in USDEUR=X over the preceding year was 1.24%,
which on the largest line would have been tens of euros.
"""
import os
import sys
from datetime import date

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from yf_util import fx_asof  # noqa: E402
from market_brief_gather import position_value, positions_table  # noqa: E402

MON, TUE, WED = date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)


def _fx(pairs):
    """A daily FX series from {date: rate}."""
    idx = pd.to_datetime([d for d, _ in pairs])
    return pd.Series([r for _, r in pairs], index=idx)


class TestFxAsOf:

    def test_it_takes_the_rate_from_the_price_date_not_the_newest(self):
        """The bug itself. The series has a Wednesday bar; a Tuesday price must
        not reach it."""
        s = _fx([(MON, 0.8501), (TUE, 0.8570), (WED, 0.8569)])
        assert fx_asof(s, TUE) == (pytest.approx(0.8570), TUE)

    def test_the_newest_rate_is_used_when_the_price_is_that_fresh(self):
        s = _fx([(MON, 0.8501), (TUE, 0.8570), (WED, 0.8569)])
        assert fx_asof(s, WED) == (pytest.approx(0.8569), WED)

    def test_it_falls_back_to_the_last_settled_bar_before_the_price_date(self):
        """A venue that traded on a day its currency pair did not publish. The
        row really is a cross-date product then, and the caller warns."""
        s = _fx([(MON, 0.8501), (WED, 0.8569)])
        rate, when = fx_asof(s, TUE)
        assert (rate, when) == (pytest.approx(0.8501), MON)

    def test_no_bar_old_enough_yields_no_rate_rather_than_a_wrong_one(self):
        s = _fx([(WED, 0.8569)])
        assert fx_asof(s, date(2020, 1, 2)) == (None, None)

    def test_a_missing_or_empty_series_is_not_an_exception(self):
        assert fx_asof(None, TUE) == (None, None)
        assert fx_asof(pd.Series(dtype=float), TUE) == (None, None)

    def test_it_never_reaches_forward(self):
        """Property: the returned date is never after the asked-for date."""
        s = _fx([(MON, 0.85), (TUE, 0.86), (WED, 0.87)])
        for d in (MON, TUE, WED):
            _, when = fx_asof(s, d)
            assert when <= d


class TestTheTableWarnsWhenItCannotAlignThem:

    def _pos(self, name, mkt, asof, fx_asof_date):
        return {"name": name, "ticker": "NGM", "sleeve": "stock",
                "broker": "broker-a", "_mkt": mkt, "_move_1d": 0.5,
                "_mkt_asof": asof, "_bar_last": asof, "_fx_asof": fx_asof_date}

    def test_a_cross_date_conversion_is_reported(self):
        book = [self._pos("Northgate Materials", 1215.0, TUE, MON)]
        _md, warnings = positions_table(book, 500.0, WED)
        assert any("converted at the" in w and "Northgate Materials" in w for w in warnings), warnings

    def test_an_aligned_row_says_nothing(self):
        book = [self._pos("Northgate Materials", 1215.0, TUE, TUE)]
        _md, warnings = positions_table(book, 500.0, WED)
        assert not any("converted at" in w for w in warnings), warnings

    def test_a_row_without_an_fx_leg_says_nothing(self):
        """EUR-priced rows carry the price date as their fx date, and rows that
        failed to fetch carry None. Neither is a mismatch."""
        eur = self._pos("Harbor Grocers", 310.0, TUE, TUE)
        unpriced = self._pos("Broken", None, None, None)
        _md, warnings = positions_table([eur, unpriced], 500.0, WED)
        assert not any("converted at" in w for w in warnings), warnings

    def test_the_table_still_renders_every_row(self):
        book = [self._pos("Northgate Materials", 1215.0, TUE, MON),
                self._pos("Harbor Grocers", 310.0, TUE, TUE)]
        md, _warnings = positions_table(book, 500.0, WED)
        assert "Northgate Materials" in md and "Harbor Grocers" in md
        assert "1,215" in md


class TestTheValueLoopActuallyUsesTheRule:
    """Mutation-testing gap, 2026-08-26. `fx_asof` and the warning were both
    covered, and replacing the call site with the original `.iloc[-1]` still
    left the whole suite green — the wiring between them was untested because
    it lived inline in `main` behind live fetches. This is that wiring."""

    def _px(self, on, value):
        return pd.Series([value], index=pd.to_datetime([on]))

    def test_a_tuesday_price_is_not_converted_at_the_wednesday_rate(self):
        """The C4 bug, at the call site, with the real 2026-08-26 shape:
        A US holding priced to Tuesday, every FX pair carrying a Wednesday bar."""
        px = self._px(TUE, 110.5)
        fx = _fx([(MON, 0.8501), (TUE, 0.8570), (WED, 0.8569)])
        value, price_date, fx_date = position_value(25, px, "USD", fx)
        assert (price_date, fx_date) == (TUE, TUE)
        assert value == pytest.approx(25 * 110.5 * 0.8570)
        # And explicitly NOT the number the original code produced.
        assert value != pytest.approx(25 * 110.5 * 0.8569)

    def test_a_eur_row_needs_no_rate_and_reports_no_spread(self):
        value, price_date, fx_date = position_value(4, self._px(TUE, 52.30), "EUR", None)
        assert value == pytest.approx(4 * 52.30)
        assert price_date == fx_date == TUE

    def test_a_missing_fx_series_yields_no_value_rather_than_an_unconverted_one(self):
        """The trap the falsifier monitor's dead `cur` parameter still carries:
        a missing rate must never fall through as 1.0, which would report a USD
        number as EUR."""
        value, price_date, fx_date = position_value(25, self._px(TUE, 110.5), "USD", None)
        assert value is None
        assert price_date == TUE and fx_date is None

    def test_no_price_series_yields_nothing_at_all(self):
        assert position_value(25, None, "USD", _fx([(TUE, 0.857)])) == (None, None, None)
        assert position_value(25, pd.Series(dtype=float), "USD", None) == (None, None, None)

    def test_it_reports_the_older_rate_it_had_to_settle_for(self):
        """A venue that traded on a day its pair published no bar. The value is
        computed, and the dates differ so the table can say so."""
        value, price_date, fx_date = position_value(
            25, self._px(TUE, 110.5), "USD", _fx([(MON, 0.8501), (WED, 0.8569)]))
        assert value == pytest.approx(25 * 110.5 * 0.8501)
        assert (price_date, fx_date) == (TUE, MON)
