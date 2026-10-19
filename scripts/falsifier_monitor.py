"""Evaluate every machine-checkable falsifier in theses.yaml. No model involved.

This is the half of the daily brief that CAN be scheduled. The judgement half
(reading news against a thesis) needs a model, and `claude -p` cannot run
unattended — its OAuth session expires on a schedule we do not control, which
would silently break the most important job in the system.

So: this runs daily and shouts only when a numeric rule fires or nears. The
narrative brief stays a manual `/market-brief` invocation.

DESIGN RULE: a falsifier with no evaluator is REPORTED, never skipped. Silently
returning "not triggered" for a rule that was never evaluated is the worst
possible failure for a pre-commitment device.

EVALUATORS ARE NOT IN THIS FILE. They are bespoke by design, one per falsifier,
and so they describe a real portfolio. They live in $VERTIGO_HOME/evaluators.py
beside theses.yaml; see examples/vertigo-home/evaluators.py for the contract.
"""
import hashlib, inspect, json, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import date
import yaml, yfinance as yf
from yf_util import series

HOME = os.environ.get("VERTIGO_HOME", os.path.expanduser("~/.vertigo"))

# HYSTERESIS, added 2026-08-18. A rule ENTERS the closing band at 0.90 and only
# leaves it below 0.85. One threshold would flap: one rule measured 0.9042 that
# day, 0.42 percentage points inside the band, so a 0.5% move in P/E flipped it
# out and back. Reporting "entered the band" every other morning on a wobble is
# how a monitor teaches its reader to stop looking at it.
NEAR = 0.90        # enter "closing"
NEAR_EXIT = 0.85   # leave it

# Yesterday's status per falsifier. Without it the table can say WHERE each rule
# stands but never that anything MOVED, which is the part worth a glance.
# Regenerable, rewritten daily, and gitignored in ~/.vertigo for that reason.
STATE_FILE = "falsifier-state.json"
HISTORY_DAYS = 120   # ~6 months of weekdays, a few KB

# THE DECISION LOG. Append-only, committed, and the ONLY record of what a rule
# actually read when it moved.
#
# Why it cannot wait for the refactor, and why it stores `reading` verbatim:
# _info() above returns yf.Ticker(t).info, a LIVE snapshot with no history
# endpoint. Half of the original evaluators read from it. Their measurements — trailing P/E, price/book, net margin — are gone
# the moment Yahoo updates. Prices are recoverable from history; these are not.
#
# It lives in ~/briefs (committed) and NOT in ~/.vertigo, where
# falsifier-state.json is gitignored and survives only in an Actions cache.
DECISIONS_FILE = "decisions.jsonl"
BRIEFS_DIR = os.environ.get("BRIEFS_DIR", os.path.expanduser("~/briefs"))


# NOTE: every evaluator returns a 4-tuple (hit, now, trigger, proximity), where
# `now` is today's measurement and `trigger` is the written condition it is
# measured against. The unevaluable path returns (None, reason, None, None).
#
# `now` and `trigger` were ONE prose string until 2026-08-18. Rendered as
# "P/S 1.65x (trigger >2.0), net margin 9.9% (escape >12%)" the reader had to
# parse today's numbers back out of the rule every time. They are two different
# things and now occupy two columns.
#
# Returning the wrong arity here raises ValueError in the caller — safe, because
# it is caught and reported as unevaluable, but the diagnostic degrades to "not
# enough values to unpack". Caught by scripts/test_falsifier_triggers.py, never
# by a live run: the live run always had data.


def _info(t):
    try:
        return yf.Ticker(t).info or {}
    except Exception:
        return {}


def _px(t, cur="USD"):
    s = series(t, period="5d", adjust=False, quiet=True)
    return float(s.iloc[-1]) if s is not None and len(s) else None


def _q_avg(t, quarters=2):
    """Last N quarterly averages, newest first."""
    s = series(t, period="2y", adjust=False, quiet=True)
    if s is None or not len(s):
        return []
    q = s.resample("QE").mean()
    return [float(v) for v in list(q)[-quarters:]][::-1]


# --- evaluators: bespoke by design, one per falsifier -----------------------
#
# Loaded from $VERTIGO_HOME/evaluators.py, which must define:
#
#   EVALUATORS     {falsifier_id: fn(falsifier_dict) -> 4-tuple}
#   DISTANCE_KIND  {falsifier_id: "above" | "below" | "baseline_pct"}
#
# The module is given a `data` attribute before it runs: this module, so an
# evaluator calls `data._info(t)`, `data._px(t)` and `data._q_avg(t)`. Going
# through the attribute rather than importing the helpers is what lets a test
# stub the network by monkeypatching this module.
#
# DISTANCE TO TRIGGER. `prox` buckets a rule into clear/closing but never says
# what has to HAPPEN, and a bar built from prox alone is misleading: a P/S of
# 1.62 against a 2.0 trigger renders as "81%", which reads like imminence when
# P/S must actually rise 24%. The honest answer is the move the metric still
# has to make, which is comparable across rules and directly evaluable.
#
# Derivable from prox alone once the rule's direction is known, so the 4-tuple
# contract is untouched:
#   above         prox = current/trigger      need = 1/prox - 1
#   below         prox = trigger/current      need = prox - 1
#   baseline_pct  prox = worst_leg / -0.25    need = 0.75/(1 + -0.25*prox) - 1
#                 (e.g. two metals must both sit 25% below a fixed baseline,
#                  so the binding leg is the one furthest from -25%.)
#
# Every EVALUATORS id must appear in DISTANCE_KIND — test_falsifier_triggers.py
# enforces it, because a new rule silently defaulting to a direction would
# print a confident number with the wrong sign.
EVALUATORS_FILE = "evaluators.py"


def load_evaluators(home):
    """Return (EVALUATORS, DISTANCE_KIND) from home/evaluators.py, or two empty
    dicts when there is no such file. With no file every `auto` falsifier is
    reported as having no evaluator — loud, which is the point."""
    import importlib.util
    path = os.path.join(home, EVALUATORS_FILE)
    if not os.path.exists(path):
        return {}, {}
    spec = importlib.util.spec_from_file_location("vertigo_evaluators", path)
    mod = importlib.util.module_from_spec(spec)
    mod.data = sys.modules[__name__]
    spec.loader.exec_module(mod)
    return dict(getattr(mod, "EVALUATORS", {})), dict(getattr(mod, "DISTANCE_KIND", {}))


EVALUATORS, DISTANCE_KIND = load_evaluators(HOME)


def required_move(fid, prox):
    """Fraction the measured metric must still travel for the rule to fire.
    Negative means it must fall. None when it cannot be expressed honestly."""
    kind = DISTANCE_KIND.get(fid)
    if kind is None or prox is None:
        return None
    try:
        if kind == "above":
            return (1.0 / prox - 1.0) if prox > 0 else None
        if kind == "below":
            return prox - 1.0
        if kind == "baseline_pct":
            denom = 1.0 + (-0.25 * prox)
            return (0.75 / denom - 1.0) if denom > 0 else None
    except ZeroDivisionError:
        return None
    return None


def fmt_move(need):
    if need is None:
        return "—"
    # Past 200% the number stops meaning anything to a reader; say so instead.
    if abs(need) > 2.0:
        return "far"
    return f"{need*100:+.0f}%"


def rule_hash(f):
    """Identity of the RULE AS WRITTEN. Covers `statement`, `trigger` and
    `baseline`, so tightening a threshold — or moving the baseline a rule is
    measured against — changes the hash even though the prose did not.

    Without this a rule can be rewritten underneath a commitment device while
    its status stays `clear`, and nothing in the record would ever say so —
    which is precisely what the device exists to prevent.
    """
    payload = json.dumps(
        {"statement": f.get("statement"), "trigger": f.get("trigger"),
         "baseline": f.get("baseline")},
        sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def evaluator_hash(fid):
    """Identity of the CODE that reads the rule. A rule whose text is untouched
    still means something different if its evaluator changed."""
    fn = EVALUATORS.get(fid)
    if fn is None:
        return ""
    try:
        return hashlib.sha1(inspect.getsource(fn).encode()).hexdigest()[:12]
    except (OSError, TypeError):
        return ""


def decisions_path():
    return os.path.join(BRIEFS_DIR, DECISIONS_FILE)


def load_decision_keys(path):
    """Every key already recorded, as built at the call site.

    Deliberately NOT restated as a tuple here: an earlier version of this
    docstring named four fields while the code built six, so it documented the
    exact broken key the comment at the call site exists to warn against.

    The daily job RETRIES — on 2026-08-24 it ran twice, 06:02 and 08:03, both
    successful — so a second pass over the same transition is the normal case,
    not an edge case. `since` only advances when the status changes, so the
    retry recomputes an identical key and dedupes against this set.
    """
    keys = set()
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue      # a torn line must not hide the rest
                keys.add((e.get("fid"), e.get("rule_hash"),
                          e.get("evaluator_hash"), e.get("status"),
                          e.get("since"), e.get("reason")))
    except FileNotFoundError:
        pass                      # first run: no file yet, not an error
    return keys


def append_decision(path, entry):
    """One line, flushed and fsynced. Returns False on failure — the caller
    turns that into exit 3, because a monitor that silently stops recording is
    the failure this whole project exists to prevent."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except Exception as e:
        print(f"  decisions: could not append ({type(e).__name__}: {e})",
              file=sys.stderr)
        return False


def load_state(home):
    """{falsifier_id: {status, since}}. Fail-soft: a missing or corrupt file
    means today reads as a baseline, never as everything having just moved."""
    try:
        with open(os.path.join(home, STATE_FILE)) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(home, state):
    path = os.path.join(home, STATE_FILE)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=0, sort_keys=True)
        os.replace(tmp, path)
    except Exception as e:
        print(f"  state: could not write {STATE_FILE} ({type(e).__name__})",
              file=sys.stderr)


def classify(hit, prox, prev_status):
    """Status today, given the reading and where this rule stood yesterday.
    The prev_status argument is what makes the band sticky."""
    if hit is None:
        return "unevaluable"
    if hit:
        return "triggered"
    if prox is None:
        return "clear"
    edge = NEAR_EXIT if prev_status == "closing" else NEAR
    return "closing" if prox >= edge else "clear"


MOVED = {
    ("clear", "closing"): "**entered the band today**",
    ("closing", "clear"): "backed off today",
    ("closing", "triggered"): "**TRIPPED today**",
    ("clear", "triggered"): "**TRIPPED today**",
    ("triggered", "closing"): "no longer tripped",
    ("triggered", "clear"): "no longer tripped",
}


def movement(prev, status, since, today):
    """One short note per row. Silent for rules that are clear and stayed
    clear — seven unchanged 'still fine' notes would bury the one that moved."""
    if prev is None:
        return ""                      # first observation is a baseline
    if prev != status:
        if status == "unevaluable":
            return "**stopped being checkable today**"
        if prev == "unevaluable":
            return "checkable again today"
        return MOVED.get((prev, status), "changed today")
    if status == "clear":
        return ""
    if since:
        try:
            d = date.fromisoformat(since)
            return "since today" if d == today else f"since {d:%-d %b}"
        except ValueError:
            pass
    return ""


def _cell(s):
    """A pipe inside a cell silently ends the column. Exception text is the
    realistic source of one, and it arrives on the row that matters most."""
    return str(s).replace("|", "\\|").replace("\n", " ")


def main():
    d = yaml.safe_load(open(os.path.join(HOME, "theses.yaml")))
    today = date.today()
    prev_state = load_state(HOME)
    new_state = {}
    fired, near, ok, broken = [], [], [], []
    dpath = decisions_path()
    seen_decisions = load_decision_keys(dpath)
    append_failed = []

    def record(f, company, fid, now, trigger, hit, prox):
        was = prev_state.get(fid) or {}
        prev = was.get("status")
        status = classify(hit, prox, prev)
        # `since` survives while the status does, so an unchanged row can say
        # how long it has stood there rather than only that it has not moved.
        since = was.get("since") if prev == status else today.isoformat()
        need = required_move(fid, prox)
        rhash, ehash = rule_hash(f), evaluator_hash(fid)

        # THE DECISION LOG fires on an EVENT, not on a run. Two kinds count:
        #   status   — the rule moved (clear→closing, closing→triggered, ...)
        #   rule     — the rule itself was rewritten while its status held
        # The second is why rule_hash is in the key. Without it you could
        # tighten a threshold on a rule sitting at `clear` and the record of
        # your own commitment would never mention it.
        #
        # A first observation is a baseline, not an event: `was` is empty on a
        # rule's first ever run, and there is nothing yet to have moved.
        reason = None
        if was:
            if prev != status:
                reason = "status"
            elif was.get("rule") and was["rule"] != rhash:
                reason = "rule_changed"
            elif was.get("evaluator") and was["evaluator"] != ehash:
                reason = "evaluator_changed"
        if reason:
            # `reason` and the evaluator hash are IN the key, not merely in the
            # payload. Without them: a rule logs clear->closing on Monday as
            # (fid, R1, closing, Mon); you edit its evaluator on Tuesday while
            # the status, `since` and rule text all hold; the recomputed key is
            # identical, so the evaluator change dedupes against Monday's entry
            # and is never recorded at all.
            key = (fid, rhash, ehash, status, since, reason)
            if key not in seen_decisions:
                entry = {
                    "date": today.isoformat(),
                    "kind": "auto_falsifier",
                    # D3 recipe 1 of 3: an auto falsifier already HAS a stable
                    # id in theses.yaml. Only news alerts need a headline hash.
                    "alert_id": fid,
                    "fid": fid, "company": company,
                    "rule_hash": rhash, "evaluator_hash": ehash,
                    "status": status, "prev_status": prev, "since": since,
                    # Verbatim, because it cannot be fetched again later.
                    "reading": now,
                    "trigger": trigger,
                    "need": None if need is None else round(need, 4),
                    "reason": reason,
                    # Filled by the human, or by `--review` (T10). Present as
                    # nulls so the shape of a row never changes.
                    "action_taken": None, "note": None,
                    "price_at_review": None, "return_since_alert": None,
                    "outcome": None,
                }
                if append_decision(dpath, entry):
                    seen_decisions.add(key)
                else:
                    append_failed.append(fid)
        # Rolling history of the distance, so "how close has this rule been
        # getting" becomes answerable. One point per run, last value of the day
        # wins, capped so the file stays small.
        hist = [h for h in (was.get("history") or []) if h and h[0] != today.isoformat()]
        if need is not None:
            hist.append([today.isoformat(), round(need, 4)])
        if fid in append_failed:
            # DO NOT ADVANCE. The transition happened, the entry did not land,
            # and `reading` cannot be fetched back — `_info()` is a live
            # snapshot with no history endpoint. Advancing here would make the
            # next run see the NEW status as its baseline, detect no
            # transition, and never retry: one failed write would consume the
            # event permanently. The Actions cache saves state `if: always()`,
            # so even the 07:15 retry would inherit the advanced value.
            #
            # Holding the previous state instead means the retry re-detects the
            # same transition, recomputes an identical `since`, and writes the
            # entry it owes. Today's history point is given up with it; that is
            # regenerable arithmetic, and the reading is not.
            new_state[fid] = was
        else:
            new_state[fid] = {"status": status, "since": since,
                              "need": None if need is None else round(need, 4),
                              "rule": rhash, "evaluator": ehash,
                              "history": hist[-HISTORY_DAYS:]}
        row = (company, fid, now, trigger,
               movement(prev, status, since, today), need)
        {"triggered": fired, "closing": near,
         "unevaluable": broken, "clear": ok}[status].append(row)

    for t in d["theses"]:
        for f in t["falsifiers"]:
            if f["check"] != "auto":
                continue
            ev = EVALUATORS.get(f["id"])
            if ev is None:
                record(f, t["company"], f["id"],
                       "NO EVALUATOR — rule is not being checked", "not checked",
                       None, None)
                continue
            try:
                hit, now, trigger, prox = ev(f)
            except Exception as e:
                record(f, t["company"], f["id"], f"{type(e).__name__}: {e}",
                       "not checked", None, None)
                continue
            if hit is None:
                record(f, t["company"], f["id"], now, "not checked", None, None)
                continue
            record(f, t["company"], f["id"], now, trigger, hit, prox)

    save_state(HOME, new_state)

    print(f"## `ƒ` Falsifier monitor — {date.today():%A %-d %B %Y}\n")
    print(f"`ƒ` **{len(fired)} triggered · {len(near)} closing · {len(ok)} clear · "
          f"{len(broken)} unevaluable**\n")
    print("*Every value in this table was computed by Python from live market "
          "data. No model was involved, here or in choosing what to show.*\n")

    # One table, worst first. Four sections meant scrolling to answer "did
    # anything fire", and split today's reading from the rule it is judged by.
    print("|  | Holding | Today | Must move | Falsifier trips at |")
    print("|:--:|---|---|--:|---|")
    for rows, mark in ((fired, "🔴"), (near, "🟠"), (broken, "⚠️"), (ok, "✅")):
        # Closest first inside each group. Unknown distances sort last rather
        # than to the top, where they would read as urgent.
        rows = sorted(rows, key=lambda r: abs(r[5]) if r[5] is not None else 9e9)
        for co, fid, now, trigger, moved, need in rows:
            # Movement rides in the "Today" cell rather than a fifth column:
            # this table is read on a phone, and a column that is empty on
            # seven rows out of eight costs width every day to say nothing.
            cell = _cell(now) + (f"<br>*{_cell(moved)}*" if moved else "")
            print(f"| {mark} | **{_cell(co)}**<br>`{_cell(fid)}` "
                  f"| {cell} | {_cell(fmt_move(need))} | {_cell(trigger)} |")
    print()

    n_manual = sum(1 for t in d["theses"] for f in t["falsifiers"] if f["check"] == "manual")
    print(f"`ƒ` 🔴 triggered · 🟠 closing (within {(1-NEAR)*100:.0f}% of the trigger) "
          "· ✅ clear · ⚠️ could not be evaluated, so it is **not** clear.")
    print("`ƒ` **Must move** = how far the measured value still has to travel for the rule to fire, as a percentage of today's level. Negative means it must fall.")
    print(f"`ƒ` {n_manual} manual falsifiers require a filing and are NOT checked here.")

    # EXIT CONTRACT. 2 already meant "needs attention" and run_daily_brief.sh
    # treats it as non-fatal, so a lost decision-log write needed its own code:
    # 3 is a DATA-INTEGRITY failure and turns the job red. The brief itself is
    # fine and still prints above; what failed is the recording of it, and that
    # is the one thing no later run can reconstruct.
    if append_failed:
        print(f"  decisions: {len(append_failed)} entr(y/ies) NOT recorded: "
              f"{', '.join(append_failed)}", file=sys.stderr)
        return 3
    return 2 if (fired or broken) else 0


if __name__ == "__main__":
    sys.exit(main())
