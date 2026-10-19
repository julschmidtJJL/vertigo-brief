"""The decision log records what a rule READ when it moved, exactly once.

Why this exists at all: `falsifier_monitor._info()` returns `yf.Ticker(t).info`,
a live snapshot with no history endpoint. Four of the eight evaluators read from
it, so their measurements — trailing P/E,
price/book, net margin — are unrecoverable the moment Yahoo updates. Prices can
be refetched from history; these cannot. A missed entry is permanent.

The retry is the reason dedup is tested first rather than last: the daily
workflow schedules 05:30 and 07:15 UTC, and on 2026-08-24 it ran twice (06:02
and 08:03), both successful. Two passes over one transition is the NORMAL case.
"""
import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import falsifier_monitor as fm  # noqa: E402



THESES = """
theses:
  - isin: TEST0000001
    company: Test Holding
    falsifiers:
      - id: test-rule
        check: auto
        statement: >
          If the measure exceeds 45 the thesis is dead.
        trigger:
          measure_above: 45
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A private VERTIGO_HOME and BRIEFS_DIR, and one stubbed evaluator whose
    reading the test controls. No network."""
    home = tmp_path / "state"
    briefs = tmp_path / "briefs"
    home.mkdir()
    briefs.mkdir()
    (home / "theses.yaml").write_text(THESES)
    monkeypatch.setattr(fm, "HOME", str(home))
    monkeypatch.setattr(fm, "BRIEFS_DIR", str(briefs))

    state = {"prox": 0.5, "reading": "measure 22.5", "fail_append": False}

    def stub(_f):
        # (hit, now, trigger, proximity) — the 4-tuple every evaluator returns.
        return False, state["reading"], "measure > 45", state["prox"]

    monkeypatch.setitem(fm.EVALUATORS, "test-rule", stub)
    monkeypatch.setitem(fm.DISTANCE_KIND, "test-rule", "above")

    # The append-failure switch lives HERE, not in a per-test monkeypatch.
    # A test that patches append_decision itself has to undo the patch to model
    # a recovering disk, and monkeypatch.undo() reverts EVERY patch this
    # fixture made — including fm.HOME and fm.BRIEFS_DIR. The first version of
    # this file did exactly that, and the "recovered" run escaped onto the real
    # ~/.vertigo and ~/briefs and wrote a live decisions.jsonl.
    real_append = fm.append_decision
    monkeypatch.setattr(
        fm, "append_decision",
        lambda *a, **k: False if state["fail_append"] else real_append(*a, **k))
    return {"home": home, "briefs": briefs, "state": state}


def entries(briefs):
    """Readable rows only — a torn line is skipped, the way any real consumer
    of an append-only log has to treat one."""
    path = briefs / "decisions.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def run(capsys):
    # Hard sandbox gate. main() reads theses.yaml from fm.HOME and appends to
    # fm.BRIEFS_DIR, both of which are real, live, committed paths by default.
    # A test that loses its patches must fail loudly here rather than quietly
    # editing the journal.
    for name in ("HOME", "BRIEFS_DIR"):
        path = getattr(fm, name)
        assert "pytest" in path or "/tmp" in path or "/private/var" in path, \
            f"REFUSING to run: fm.{name} is {path!r}, outside the sandbox"
    rc = fm.main()
    capsys.readouterr()          # the monitor prints its markdown table
    return rc


class TestBaseline:
    def test_first_ever_run_logs_nothing(self, env, capsys):
        """A first observation is a baseline. Nothing has moved yet, so there
        is no event, and a log full of 'I started watching' is noise."""
        assert run(capsys) == 0
        assert entries(env["briefs"]) == []


class TestTransition:
    def test_a_status_change_logs_exactly_one_entry(self, env, capsys):
        run(capsys)                              # baseline: clear
        env["state"]["prox"] = 0.95              # into the closing band
        env["state"]["reading"] = "measure 42.8"
        run(capsys)

        rows = entries(env["briefs"])
        assert len(rows) == 1
        assert rows[0]["status"] == "closing"
        assert rows[0]["prev_status"] == "clear"
        assert rows[0]["reason"] == "status"

    def test_the_verbatim_reading_is_captured(self, env, capsys):
        """The whole point. `reading` is the only copy of this measurement."""
        run(capsys)
        env["state"]["prox"] = 0.95
        env["state"]["reading"] = "P/E 42.8 · net margin 3.1%"
        run(capsys)

        assert entries(env["briefs"])[0]["reading"] == "P/E 42.8 · net margin 3.1%"

    def test_alert_id_is_the_falsifier_id_not_a_headline_hash(self, env, capsys):
        """D3 recipe 1 of 3: an auto falsifier already has a stable id in
        theses.yaml. Only news alerts need sha1(isin + headline)."""
        run(capsys)
        env["state"]["prox"] = 0.95
        run(capsys)

        assert entries(env["briefs"])[0]["alert_id"] == "test-rule"

    def test_review_fields_are_present_as_nulls(self, env, capsys):
        """`--review` (T10) fills these later. Present from the start so the
        shape of a row never changes mid-history."""
        run(capsys)
        env["state"]["prox"] = 0.95
        run(capsys)

        row = entries(env["briefs"])[0]
        for f in ("action_taken", "note", "price_at_review",
                  "return_since_alert", "outcome"):
            assert f in row and row[f] is None


class TestIdempotency:
    def test_the_retry_does_not_double_log(self, env, capsys):
        """06:02 and 08:03 on 2026-08-24, both successful. This is the norm."""
        run(capsys)
        env["state"]["prox"] = 0.95
        run(capsys)                              # first attempt
        run(capsys)                              # the 07:15 UTC retry
        assert len(entries(env["briefs"])) == 1

    def test_a_rule_that_stays_put_logs_nothing_further(self, env, capsys):
        run(capsys)
        env["state"]["prox"] = 0.95
        run(capsys)
        for _ in range(5):                       # a week of unchanged mornings
            run(capsys)
        assert len(entries(env["briefs"])) == 1

    def test_moving_back_and_forth_logs_both_legs(self, env, capsys):
        """clear -> closing -> clear is two events, not one, and the second
        must be distinguishable from the first."""
        run(capsys)
        env["state"]["prox"] = 0.95
        run(capsys)
        env["state"]["prox"] = 0.5               # below NEAR_EXIT, backs off
        run(capsys)

        rows = entries(env["briefs"])
        assert [r["status"] for r in rows] == ["closing", "clear"]
        assert rows[0]["since"] != rows[1]["since"] or rows[0]["status"] != rows[1]["status"]


class TestRuleChanges:
    def test_rewriting_the_rule_logs_even_when_the_status_holds(self, env, capsys):
        """The case a status-only key misses entirely: tighten a threshold on a
        rule sitting at `clear`, and without rule_hash the record of your own
        commitment never mentions that you changed it."""
        run(capsys)
        (env["home"] / "theses.yaml").write_text(
            THESES.replace("measure_above: 45", "measure_above: 30"))
        run(capsys)

        rows = entries(env["briefs"])
        assert len(rows) == 1
        assert rows[0]["reason"] == "rule_changed"
        assert rows[0]["status"] == "clear"      # status never moved

    def test_rule_hash_covers_the_trigger_not_just_the_prose(self):
        base = {"statement": "same words", "trigger": {"measure_above": 45}}
        tightened = {"statement": "same words", "trigger": {"measure_above": 30}}
        assert fm.rule_hash(base) != fm.rule_hash(tightened)

    def test_rule_hash_is_stable_across_key_order(self):
        a = {"statement": "x", "trigger": {"a": 1, "b": 2}}
        b = {"statement": "x", "trigger": {"b": 2, "a": 1}}
        assert fm.rule_hash(a) == fm.rule_hash(b)


class TestFailureIsLoud:
    def test_an_unwritable_log_exits_3_not_0(self, env, capsys):
        """Exit 2 already means 'a rule fired, look at it' and is non-fatal.
        A lost entry is different in kind: the brief is fine, but the reading is
        gone and no later run can fetch it back. 3 turns the job red."""
        run(capsys)
        env["state"]["prox"] = 0.95
        env["state"]["fail_append"] = True

        assert run(capsys) == 3

    def test_a_torn_line_does_not_hide_the_rest(self, env, capsys):
        """A half-written line must not make the whole history unreadable, or
        one bad row silently re-opens every earlier transition for re-logging."""
        run(capsys)
        env["state"]["prox"] = 0.95
        run(capsys)

        path = env["briefs"] / "decisions.jsonl"
        path.write_text(path.read_text() + '{"fid": "torn", "rule_h\n')
        run(capsys)                              # the retry, against a torn file

        good = [r for r in entries(env["briefs"]) if r.get("fid") == "test-rule"]
        assert len(good) == 1


class TestAFailedAppendIsRetried:
    """Regression cover for the review of 2026-08-24.

    The first version advanced falsifier-state.json even when the append had
    failed. The next run then saw the NEW status as its baseline, detected no
    transition, and never retried — one failed write consumed the event
    permanently. The Actions cache saves state `if: always()`, so even the
    07:15 retry inherited the advanced value.
    """

    def test_the_transition_is_re_logged_on_the_next_run(self, env, capsys):
        run(capsys)                                  # baseline
        env["state"]["prox"] = 0.95
        env["state"]["reading"] = "P/E 42.8 · net margin 3.1%"

        env["state"]["fail_append"] = True
        assert run(capsys) == 3                      # append fails, job goes red
        assert entries(env["briefs"]) == []

        env["state"]["fail_append"] = False          # the retry, disk healthy
        assert run(capsys) == 0

        rows = entries(env["briefs"])
        assert len(rows) == 1, "the failed transition was never retried"
        assert rows[0]["status"] == "closing"
        assert rows[0]["reading"] == "P/E 42.8 · net margin 3.1%"

    def test_state_is_not_advanced_while_the_append_is_failing(self, env, capsys):
        run(capsys)
        env["state"]["prox"] = 0.95
        env["state"]["fail_append"] = True
        run(capsys)

        import json as _json
        state = _json.loads((env["home"] / "falsifier-state.json").read_text())
        assert state["test-rule"]["status"] == "clear", \
            "state advanced past an unrecorded transition"


class TestEventTypesAreDistinct:
    def test_an_evaluator_change_is_not_deduped_against_a_status_entry(self, env, capsys, monkeypatch):
        """The key must carry `reason` and the evaluator hash. Without them a
        rule that logged clear->closing on Monday produces an identical key when
        its evaluator changes later at the same status and `since`, so the
        second event dedupes against the first and vanishes."""
        run(capsys)
        env["state"]["prox"] = 0.95
        run(capsys)                                  # status transition logged
        assert len(entries(env["briefs"])) == 1

        # Same status, same `since`, same rule text — only the evaluator moves.
        def stub_v2(_f):
            return False, env["state"]["reading"], "measure > 45", env["state"]["prox"]
        monkeypatch.setitem(fm.EVALUATORS, "test-rule", stub_v2)
        run(capsys)

        rows = entries(env["briefs"])
        assert len(rows) == 2, "the evaluator change was deduped away"
        assert rows[1]["reason"] == "evaluator_changed"
        assert rows[1]["status"] == rows[0]["status"]


class TestMigrationFromTheOldStateFile:
    """The very next production run reads state written by master.

    master's falsifier_monitor writes {status, since, need, history} and knows
    nothing about `rule` or `evaluator`. On Actions that state arrives through
    an actions/cache restore, so the first run after this deploys sees the old
    shape for every rule. Two ways that could go wrong, both silent:

      - a KeyError inside record() takes down the falsifier half of the brief
      - or every rule reports rule_changed/evaluator_changed and eight phantom
        rows land in a permanent, committed journal on day one

    The `was.get("rule") and` / `was.get("evaluator") and` guards are what make
    it a quiet no-op. Nothing tested them until this.
    """

    OLD_SHAPE = {"test-rule": {"status": "clear", "since": "2026-08-20",
                               "need": 0.12, "history": [["2026-08-20", 0.12]]}}

    def test_old_state_logs_nothing_and_backfills_the_hashes(self, env, capsys):
        (env["home"] / "falsifier-state.json").write_text(json.dumps(self.OLD_SHAPE))

        assert run(capsys) == 0
        assert entries(env["briefs"]) == [], "migration wrote a phantom event"

        state = json.loads((env["home"] / "falsifier-state.json").read_text())
        assert state["test-rule"]["rule"], "rule hash was not backfilled"
        assert state["test-rule"]["evaluator"], "evaluator hash was not backfilled"

    def test_since_survives_the_migration(self, env, capsys):
        """`since` is how long a rule has stood where it is. Resetting it to
        today would quietly erase that on every rule at once."""
        (env["home"] / "falsifier-state.json").write_text(json.dumps(self.OLD_SHAPE))
        run(capsys)

        state = json.loads((env["home"] / "falsifier-state.json").read_text())
        assert state["test-rule"]["since"] == "2026-08-20"

    def test_a_real_transition_out_of_old_state_is_still_recorded(self, env, capsys):
        """The migration must be quiet, not deaf. A rule that genuinely moves
        on the first run after the deploy still has to reach the journal."""
        (env["home"] / "falsifier-state.json").write_text(json.dumps(self.OLD_SHAPE))
        env["state"]["prox"] = 0.95                  # clear -> closing
        env["state"]["reading"] = "P/E 43.1"

        run(capsys)

        rows = entries(env["briefs"])
        assert len(rows) == 1
        assert rows[0]["reason"] == "status"
        assert rows[0]["prev_status"] == "clear"
        assert rows[0]["reading"] == "P/E 43.1"


class TestNoNetworkInThisModule:
    def test_the_helpers_are_pure(self):
        """rule_hash and evaluator_hash must never reach for data. If either
        grows a fetch, this suite stops being safe to run in CI."""
        assert fm.rule_hash({"statement": "a", "trigger": {}})
        assert fm.evaluator_hash("does-not-exist") == ""
        fid = next(iter(fm.EVALUATORS))
        assert len(fm.evaluator_hash(fid)) == 12
