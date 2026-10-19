"""The positions table must never present two sessions as one.

Regression cover for the 2026-08-25 brief, which priced ten of thirteen holdings
at Friday 08-21's close, reported their Thursday->Friday moves as "last session"
on a Tuesday, and summed a day-delta of +41 across two different days.

The fixture below is that exact shape: the US, .TO and .HK listings carried a Monday
bar, the .DE/.MU/.AS/.PA listings did not.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from market_brief_gather import positions_table  # noqa: E402


TUE = date(2026, 8, 25)
MON = date(2026, 8, 24)
FRI = date(2026, 8, 21)


def _pos(name, ticker, mkt, move_1d, asof, sleeve="stock", broker="broker-a"):
    return {"name": name, "ticker": ticker, "sleeve": sleeve, "broker": broker,
            "_mkt": mkt, "_move_1d": move_1d, "_mkt_asof": asof,
            "_bar_last": asof}


def _split_book():
    """The 2026-08-25 shape: three venues fresh, the rest a session behind."""
    return [
        _pos("Northgate Materials", "NGM", 1210.0, 0.76, MON),
        _pos("Borealis Nickel", "BNX.TO", 80.0, -2.50, MON, broker="broker-b"),
        _pos("Example Mining Group H", "EMG.HK", 35.0, -4.37, MON, broker="broker-b"),
        _pos("Harbor Grocers", "HBG.DE", 310.0, -0.67, FRI),
        _pos("World Index ETF", "WIX.DE", 2400.0, 0.43, FRI, sleeve="etf"),
        _pos("Pacific Insurance H", "PCI.MU", 140.0, 4.67, FRI, broker="broker-b"),
    ]


def _aligned_book():
    return [_pos("Northgate Materials", "NGM", 1210.0, 0.76, MON),
            _pos("Harbor Grocers", "HBG.DE", 310.0, -0.67, MON)]


class TestMixedSession:
    def test_day_total_is_suppressed(self):
        md, _ = positions_table(_split_book(), 500.0, TUE)
        total = [l for l in md.splitlines() if l.startswith("| **Total**")][0]
        assert "**—**" in total, total
        # The bug shipped a signed number here. Nothing signed may survive.
        assert "+" not in total.split("|")[3] and "-" not in total.split("|")[3]

    def test_it_says_why_the_total_is_gone(self):
        md, _ = positions_table(_split_book(), 500.0, TUE)
        assert "day total is suppressed" in md
        assert "not share one session" in md

    def test_lagging_rows_are_marked_and_fresh_ones_are_not(self):
        md, _ = positions_table(_split_book(), 500.0, TUE)
        lines = {l.split("|")[1].strip(): l for l in md.splitlines()
                 if l.startswith("| ") and "%" in l}
        assert "†" in lines["Harbor Grocers"]
        assert "†" in lines["Pacific Insurance H"]
        assert "†" not in lines["Northgate Materials"]
        assert "†" not in lines["Borealis Nickel"]

    def test_the_footnote_names_the_stale_date(self):
        md, _ = positions_table(_split_book(), 500.0, TUE)
        assert "2026-08-21" in md and "2026-08-24" in md

    def test_header_states_the_newest_close_not_just_last_session(self):
        md, _ = positions_table(_split_book(), 500.0, TUE)
        assert "close 2026-08-24" in md


class TestUnsettledBar:
    """A bar dated today is the session in progress, never its close.

    Not hypothetical: the 07:15 UTC retry cron runs at 09:15 CEST, fifteen
    minutes after Xetra opens, so a European listing can carry an in-progress
    bar. Observed on an ad-hoc run at 10:50 CEST 2026-08-26.
    """

    def test_todays_bar_is_not_called_a_close(self):
        book = [_pos("Northgate Materials", "NGM", 1210.0, 0.76, TUE)]
        md, _ = positions_table(book, 500.0, TUE)
        assert "not yet a close" in md
        assert "close 2026-08-25" not in md

    def test_a_settled_bar_still_reads_as_a_close(self):
        book = [_pos("Northgate Materials", "NGM", 1210.0, 0.76, MON)]
        md, _ = positions_table(book, 500.0, TUE)
        assert "close 2026-08-24" in md
        assert "not yet a close" not in md

    def test_it_warns(self):
        _, warnings = positions_table(_split_book(), 500.0, TUE)
        assert any("more than one session" in w for w in warnings)
        assert any("suppressed" in w for w in warnings)

    def test_values_still_use_each_holdings_own_freshest_close(self):
        """Disclose the spread; do NOT pin the table back to a common date.

        Pinning is its own bug — the weekly scoreboard did it, one holding at
        EUR 0.174 against 0.258 on the broker screen.
        """
        md, _ = positions_table(_split_book(), 500.0, TUE)
        assert "| 310 |" in md          # Harbor Grocers still at its own Friday close
        assert "| 1,210 |" in md        # Northgate still at its own Monday close


class TestAlignedSession:
    def test_total_is_computed(self):
        md, warnings = positions_table(_aligned_book(), 500.0, TUE)
        total = [l for l in md.splitlines() if l.startswith("| **Total**")][0]
        assert "**—**" not in total
        assert warnings == []

    def test_no_daggers_and_no_footnote(self):
        md, _ = positions_table(_aligned_book(), 500.0, TUE)
        assert "†" not in md
        assert "suppressed" not in md


class TestTwoFetchesDisagree:
    def test_a_row_whose_value_and_move_came_from_different_bars_warns(self):
        book = _aligned_book()
        book[0]["_bar_last"] = FRI          # value priced Monday, move to Friday
        _, warnings = positions_table(book, 500.0, TUE)
        assert any("two fetches disagreed" in w for w in warnings)


class TestDegradedInputs:
    def test_a_failed_fetch_does_not_fabricate_a_session(self):
        book = _aligned_book() + [_pos("Broken", "X.DE", None, None, None)]
        md, warnings = positions_table(book, 500.0, TUE)
        assert not any("more than one session" in w for w in warnings)
        assert "| Broken | — | — | | broker-a |" in md

    def test_every_fetch_failing_still_renders(self):
        book = [_pos("A", "A.DE", None, None, None),
                _pos("B", "B.DE", None, None, None)]
        md, warnings = positions_table(book, 500.0, TUE)
        assert "## `ƒ` Positions" in md
        assert not any("more than one session" in w for w in warnings)
