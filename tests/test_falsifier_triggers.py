"""Do the falsifier rules actually FIRE when they should?

Every evaluator reported "clear" on the live run. So would a rule with an
inverted comparison. A falsifier that can never trigger is worse than no
falsifier, because it is trusted — and no amount of successful live runs will
ever reveal it, since "not triggered" is the expected output either way.

So each rule is fed synthetic inputs: one that MUST fire, and cases that must
NOT. The network is stubbed out; this is pure logic and runs offline.

Converted from `scripts/test_falsifier_triggers.py` on 2026-08-24. That file was
never collected: `pyproject.toml` sets `testpaths = ["tests"]` and it lived in
`scripts/`, so the one test proving these rules fire had never run in the suite.
It was also not pytest-shaped — no test functions, assertions replaced by a
FAILS list, and `sys.exit(1)` at module scope — so moving it would have made
pytest execute it during collection and report nothing collected.
"""
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import falsifier_monitor as fm  # noqa: E402

# The trigger tests always run against the EXAMPLE evaluators, whatever
# VERTIGO_HOME points at: they test the arithmetic of known rules, not your
# registry. The registry tests further down use VERTIGO_HOME.
EXAMPLE_HOME = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "vertigo-home")
EX, EX_KIND = fm.load_evaluators(EXAMPLE_HOME)


@pytest.fixture
def stub(monkeypatch):
    """Replace the three network calls with fixed answers."""
    def _stub(info=None, px=None, q=None):
        monkeypatch.setattr(fm, "_info", lambda t: (info or {}).get(t, {}))
        monkeypatch.setattr(fm, "_px", lambda t, cur="USD": (px or {}).get(t))
        monkeypatch.setattr(fm, "_q_avg", lambda t, quarters=2: (q or {}).get(t, []))
    return _stub


def fires(fid, arg=None):
    """The `hit` leg of the 4-tuple (hit, now, trigger, proximity).

    Unpacking all four here is deliberate: an evaluator that returns the old
    3-tuple fails LOUDLY at this line rather than in a live run, where the
    caller swallows the ValueError and reports the rule as merely unevaluable.
    """
    hit, _now, _trigger, _prox = EX[fid](arg or {})
    return hit


# --- P/S above a ceiling unless margin escapes ------------------------------
class TestValuationWithMarginEscape:
    @pytest.mark.parametrize("mc,rev,margin,expected", [
        (2000, 1000, 0.06, True),    # P/S 2.0, margin 6% — MUST FIRE
        (2000, 1000, 0.10, False),   # P/S 2.0 but margin 10% — the escape
        (1200, 1000, 0.06, False),   # P/S 1.2, margin 6%
    ])
    def test_rule(self, stub, mc, rev, margin, expected):
        stub(info={"HEI.DE": {"marketCap": mc, "totalRevenue": rev,
                              "profitMargins": margin}})
        assert fires("hei-valuation-stretched") is expected

    def test_unevaluable_when_fields_missing(self, stub):
        stub(info={"HEI.DE": {}})
        assert fires("hei-valuation-stretched") is None


# --- both of the last 2 quarterly averages below a floor --------------------
class TestQuarterlyFloor:
    @pytest.mark.parametrize("quarters,expected", [
        ([3.00, 3.20], True),        # both below 3.50 — MUST FIRE
        ([3.00, 4.00], False),       # only one below
        ([4.40, 4.60], False),
    ])
    def test_rule(self, stub, quarters, expected):
        stub(q={"HG=F": quarters})
        assert fires("cat-copper-quarters") is expected

    def test_unevaluable_with_one_quarter(self, stub):
        stub(q={"HG=F": [3.0]})
        assert fires("cat-copper-quarters") is None


# --- trailing P/E above a ceiling --------------------------------------------
class TestMultipleRerate:
    @pytest.mark.parametrize("pe,expected", [(30, True), (18.5, False)])
    def test_rule(self, stub, pe, expected):
        stub(info={"BN.PA": {"trailingPE": pe}})
        assert fires("bn-multiple-rerate") is expected


# --- price below a level -----------------------------------------------------
class TestBelowEntry:
    @pytest.mark.parametrize("px,expected", [
        (120.0, True),               # MUST FIRE
        (200.0, False),
        (140.5, False),              # boundary, just above
    ])
    def test_rule(self, stub, px, expected):
        stub(px={"SIE.DE": px})
        assert fires("sie-below-entry") is expected


# --- two legs both <= -25% from a baseline -----------------------------------
BASE = {"baseline": {"date": "2026-02-03",
                     "copper_usd_lb": 4.50, "brent_usd_bbl": 75.00}}


class TestBaselineBreak:
    @pytest.mark.parametrize("cu_mult,oil_mult,expected", [
        (0.70, 0.70, True),          # both -30% — MUST FIRE
        (0.70, 0.90, False),         # copper -30%, Brent -10%
        (1.00, 1.00, False),         # both flat
    ])
    def test_rule(self, stub, cu_mult, oil_mult, expected):
        stub(px={"HG=F": 4.50 * cu_mult, "BZ=F": 75.00 * oil_mult})
        assert fires("cat-commodity-bust", BASE) is expected

    def test_unevaluable_without_a_baseline(self, stub):
        stub(px={"HG=F": 4.0, "BZ=F": 70.0})
        assert fires("cat-commodity-bust", {}) is None


# --- registry coverage -----------------------------------------------------
def _auto_falsifier_ids():
    home = os.environ.get("VERTIGO_HOME", os.path.expanduser("~/.vertigo"))
    path = os.path.join(home, "theses.yaml")
    if not os.path.exists(path):
        pytest.skip(f"no theses.yaml at {path} — set VERTIGO_HOME to check "
                    "registry coverage")
    d = yaml.safe_load(open(path))
    return [f["id"] for t in d["theses"] for f in t["falsifiers"]
            if f["check"] == "auto"]


class TestRegistryCoverage:
    def test_every_auto_falsifier_has_an_evaluator(self):
        """A rule marked `auto` with no evaluator is reported as unevaluable at
        runtime, which is safe — but it means a falsifier you believe is being
        checked is not, and that is worth failing a build over."""
        missing = [i for i in _auto_falsifier_ids() if i not in fm.EVALUATORS]
        assert missing == [], f"auto falsifiers with no evaluator: {missing}"

    def test_evaluators_without_a_matching_auto_falsifier_are_reported(self):
        """A warning in the original, kept as one: an orphan evaluator is dead
        code, not a broken rule, so it must not fail the build."""
        orphan = [i for i in fm.EVALUATORS if i not in _auto_falsifier_ids()]
        if orphan:
            pytest.skip(f"evaluators not marked auto in theses.yaml: {orphan}")


# --- distance coverage -----------------------------------------------------
# Without this a new rule silently falls out of DISTANCE_KIND and the "Must
# move" column prints nothing for it, or worse, a confident number with the
# wrong sign if a default were ever added.
class TestDistanceCoverage:
    def test_every_evaluator_declares_a_direction(self):
        no_kind = [i for i in fm.EVALUATORS if i not in fm.DISTANCE_KIND]
        assert no_kind == [], f"evaluator with no DISTANCE_KIND: {no_kind}"

    def test_example_evaluators_declare_a_direction(self):
        no_kind = [i for i in EX if i not in EX_KIND]
        assert no_kind == [], f"evaluator with no DISTANCE_KIND: {no_kind}"

    def test_no_direction_without_an_evaluator(self):
        orphan = [i for i in fm.DISTANCE_KIND if i not in fm.EVALUATORS]
        assert orphan == [], f"DISTANCE_KIND with no evaluator: {orphan}"


# --- the arithmetic itself, on known values --------------------------------
@pytest.fixture
def example_kinds(monkeypatch):
    monkeypatch.setattr(fm, "DISTANCE_KIND", EX_KIND)


@pytest.mark.usefixtures("example_kinds")
class TestRequiredMove:
    @pytest.mark.parametrize("label,fid,prox,want", [
        ("above  P/E 22.4 vs 25 -> must rise 12%",
         "bn-multiple-rerate", 22.4 / 25, +0.116),
        ("above  P/S 1.21 vs 1.5 -> must rise 24%",
         "hei-valuation-stretched", 1.21 / 1.5, +0.240),
        ("below  EUR 210 vs 140 -> must fall 33%",
         "sie-below-entry", 140 / 210, -0.333),
        ("below  copper 5.00 vs 3.50 -> fall 30%",
         "cat-copper-quarters", 3.50 / 5.00, -0.300),
    ])
    def test_known_values(self, label, fid, prox, want):
        assert fm.required_move(fid, prox) == pytest.approx(want, abs=0.005), label

    def test_baseline_pct_leg_up_2pct_must_fall_26pct(self):
        got = fm.required_move("cat-commodity-bust", 0.02 / -0.25)
        assert got == pytest.approx(-0.265, abs=0.005)

    @pytest.mark.parametrize("label,fid,prox", [
        ("None prox", "bn-multiple-rerate", None),
        ("zero prox", "bn-multiple-rerate", 0.0),
        ("unknown id", "does-not-exist", 0.5),
    ])
    def test_returns_none_when_it_cannot_be_expressed_honestly(self, label, fid, prox):
        assert fm.required_move(fid, prox) is None, label
