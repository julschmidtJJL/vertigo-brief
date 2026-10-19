"""A positions table must not imply one session when it spans two.

Regression cover for 2026-08-25, when the daily brief priced ten of thirteen
holdings at Friday 08-21's close and reported Thursday->Friday moves under a
header that read "last session close to close", on a Tuesday. the US, .TO and
.HK listings were measured Friday->Monday in the same table, and the day-delta total
(+41) added the two together.

Same Yahoo behaviour as the 2026-08-22 scoreboard bug, and the opposite remedy on purpose. A COMPARISON moves
back to one shared date (`choose_asof`). A positions table keeps each holding's
freshest close, because a broker screen does, and discloses the spread instead
(`session_spread`). Pinning this table to a common date is itself a known bug:
the weekly scoreboard did it, one holding at EUR 0.174 against 0.258 on screen.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from yf_util import session_spread  # noqa: E402


MON = date(2026, 8, 24)
FRI = date(2026, 8, 21)
THU = date(2026, 8, 20)


class TestSessionSpread:
    def test_the_2026_08_25_shape_three_fresh_ten_stale(self):
        """The actual brief: the US/.TO/.HK listings on Monday, everything else on Friday."""
        newest, stale = session_spread([MON, MON, MON] + [FRI] * 10)
        assert newest == MON
        assert stale == [FRI]

    def test_aligned_basket_reports_no_spread(self):
        newest, stale = session_spread([FRI, FRI, FRI])
        assert newest == FRI
        assert stale == []

    def test_three_distinct_sessions_all_surface(self):
        newest, stale = session_spread([MON, FRI, THU])
        assert newest == MON
        assert stale == [THU, FRI]

    def test_failed_fetches_do_not_count_as_a_session(self):
        """A None is a missing row, not evidence of an older session."""
        newest, stale = session_spread([MON, None, MON])
        assert newest == MON
        assert stale == []

    def test_all_missing_returns_nothing_rather_than_today(self):
        """Unlike choose_asof, there is no sensible fallback date to invent."""
        assert session_spread([None, None]) == (None, [])
        assert session_spread([]) == (None, [])

    def test_single_row_is_trivially_aligned(self):
        assert session_spread([FRI]) == (FRI, [])

    def test_duplicate_dates_collapse(self):
        """Thirteen rows on two dates is a two-date spread, not a thirteen-date one."""
        newest, stale = session_spread([MON] * 5 + [FRI] * 8)
        assert (newest, stale) == (MON, [FRI])
