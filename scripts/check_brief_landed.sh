#!/bin/bash
# Alarm when the daily brief did not land. Optional, local (macOS launchd).
#
# REPOINTED 2026-08-21, when generation moved to GitHub Actions. It used to
# check for a file in the local ~/briefs, which the Mac wrote. GitHub writes to
# the REMOTE now and this Mac may not have pulled for days, so the old check
# would have alarmed every single morning while everything was fine — and an
# alert that cries wolf daily is worse than none, because you stop reading it.
#
# WHY IT STILL EXISTS at all, given GitHub emails on workflow failure: GitHub
# cannot email you about a run that never STARTED. Scheduled workflows are
# dropped under load, and a job that never fires produces no failure to report.
# That silence is exactly what this asks about.
#
# Deliberately dumb: is today's brief on origin/master? Nothing else. A checker
# with its own logic to get wrong is a second thing that can fail silently.
#
# IT NOW RECOVERS, not just reports (2026-08-27). That morning all three crons
# across two workflows dropped together — 05:30 and 07:15 for the brief, 03:00
# for the tests — while every workflow_dispatch and push that day ran fine.
# daily-brief.yml's second cron is described as a RETRY, and it is not: it
# rides the same mechanism that failed, so it cannot cover a correlated drop.
# `workflow_dispatch` from here is a genuinely different path, and it is the
# one that was demonstrably working while the scheduler was not.
#
# This puts the Mac back in the loop, deliberately as a SMALL dependency: it
# does not generate the brief, it asks GitHub to. Asleep at 09:00 means launchd
# fires on wake and the brief is late rather than absent, which is strictly
# better than the morning that prompted this.
set -uo pipefail

# launchd starts jobs with PATH=/usr/bin:/bin:/usr/sbin:/sbin and does not
# read your shell config — the same reason keys belong in .zshenv. `git` lives
# in /usr/bin so the file check always worked; `gh` is in /usr/local/bin and
# was not found, which is why the 2026-08-27 alarm could only say
# "(gh unavailable)" — the one piece of context it exists to attach.
#
# Widen the PATH only if `gh` is genuinely missing, rather than unconditionally
# prepending — an unconditional prepend would shadow a `gh` the caller put in
# front on purpose, which is how the tests stand a stub in for the real thing.
#
# The search list is overridable, and that is not decoration. The test for "no
# gh anywhere" removes its stub, and the widen then found the REAL
# /usr/local/bin/gh and dispatched six live workflow runs on 2026-08-27. They
# all no-opped on BRIEF_SKIP_IF_EXISTS, so nothing was sent — the skip guard
# was the only thing between a unit test and six brief emails. A test must be
# able to point this somewhere empty.
GH_PATHS="${BRIEF_WATCHDOG_GH_PATHS:-/usr/local/bin:/opt/homebrew/bin}"
command -v gh >/dev/null 2>&1 || PATH="$GH_PATHS:$PATH"
export PATH

BRIEFS="${BRIEFS_REPO:-$HOME/briefs}"
LOG_DIR="${VERTIGO_LOG_DIR:-$HOME/Library/Logs/vertigo}"
WLOG="$LOG_DIR/brief-watchdog.log"
WAIT_MIN="${BRIEF_WATCHDOG_WAIT_MIN:-25}"
BRANCH="${BRIEFS_BRANCH:-master}"
# owner/name of the repo whose Actions run the brief. Defaults to this
# checkout's GitHub remote; set VERTIGO_GH_REPO when they differ.
REPO_SLUG="${VERTIGO_GH_REPO:-$(git -C "$(dirname "$0")/.." remote get-url origin 2>/dev/null \
  | sed -E 's#^.*github\.com[:/]##; s#\.git$##')}"
# How long to wait for a dispatched run to produce the file. A cold brief takes
# ~3 minutes on a runner; 12 leaves room for a queue without holding the job
# open all morning.
RECOVER_MIN="${BRIEF_WATCHDOG_RECOVER_MIN:-12}"
# Escape hatch for tests and for a morning when you do not want it acting.
NO_DISPATCH="${BRIEF_WATCHDOG_NO_DISPATCH:-}"
# Seconds between polls, and the two deadlines in seconds. Knobs only so the
# tests can drive the whole flow in seconds instead of half an hour; nothing
# should change them in anger. The minutes stay the interface — the alarm text
# quotes them, and a human thinks in minutes.
POLL_SEC="${BRIEF_WATCHDOG_POLL_SEC:-60}"
WAIT_SEC="${BRIEF_WATCHDOG_WAIT_SEC:-$(( WAIT_MIN * 60 ))}"
RECOVER_SEC="${BRIEF_WATCHDOG_RECOVER_SEC:-$(( RECOVER_MIN * 60 ))}"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y-%m-%d)"

log() { echo "$(date -Iseconds) $*" >> "$WLOG"; }
[ -f "$WLOG" ] && [ "$(wc -c < "$WLOG")" -gt 262144 ] && { tail -c 65536 "$WLOG" | sed '1d' > "$WLOG.tmp"; mv "$WLOG.tmp" "$WLOG"; }

# The brief only runs on weekdays, so neither does this.
[ "$(date +%u)" -gt 5 ] && exit 0
[ -d "$BRIEFS/.git" ] || { log "skip — $BRIEFS is not a git repo"; exit 0; }

# Never prompt: no terminal here, and a prompt is a hang.
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=15}"

landed() {
  # Local copy counts too — if it is here, it exists, however it arrived.
  [ -s "$BRIEFS/$STAMP.md" ] && return 0
  git -C "$BRIEFS" fetch -q origin "$BRANCH" 2>/dev/null || return 2   # 2 = could not check
  git -C "$BRIEFS" cat-file -e "origin/$BRANCH:$STAMP.md" 2>/dev/null
}

# WALL CLOCK, not an iteration count. `sleep 60` does not advance a counter
# while macOS has timers suspended — that bug let this run 1h03m against a
# 25-minute limit on 2026-08-21 and never alarm.
poll_for_brief() {                                  # $1 = seconds
  local deadline=$(( $(date +%s) + $1 )) rc=1
  while [ "$(date +%s)" -lt "$deadline" ]; do
    landed; rc=$?
    [ "$rc" -eq 0 ] && return 0
    sleep "$POLL_SEC"
  done
  return "$rc"
}

poll_for_brief "$WAIT_SEC"; rc=$?

pull_local() {
  # Keep the local copy current while we are here, so reading it offline works.
  git -C "$BRIEFS" pull --ff-only -q origin "$BRANCH" 2>/dev/null || true
}

if [ "$rc" -eq 0 ]; then
  pull_local
  log "ok — $STAMP.md on origin/$BRANCH"
  exit 0
fi

if [ "$rc" -eq 2 ]; then
  # Could not reach the remote. That is not evidence the brief is missing, and
  # alarming on it would fire every morning spent on a train. Visible in the
  # log, silent on screen.
  log "UNKNOWN — could not fetch origin/$BRANCH, not alarming"
  exit 0
fi

# ------------------------------------------------------------------ recover
# What does GitHub think happened? Context first, because whether a run EXISTS
# decides whether dispatching is the right move or a duplicate.
GH_STATUS="(gh not on PATH)"
RUN_STATE=""
if command -v gh >/dev/null 2>&1; then
  GH_STATUS="$(gh run list --repo "$REPO_SLUG" \
    --workflow=daily-brief.yml --limit 1 --json status,conclusion,createdAt \
    -q '.[0] | "\(.status) \(.conclusion // "") at \(.createdAt)"' 2>/dev/null || echo "(query failed)")"
  # Anything already queued or running today means the scheduler is merely
  # late, not absent. Dispatching on top of that buys nothing: daily-brief.yml
  # serialises on a concurrency group, so the second run would just wait and
  # then no-op on BRIEF_SKIP_IF_EXISTS.
  RUN_STATE="$(gh run list --repo "$REPO_SLUG" --workflow=daily-brief.yml \
    --limit 5 --json status,createdAt \
    -q "[.[] | select(.createdAt | startswith(\"$STAMP\")) | .status] | join(\",\")" 2>/dev/null || echo "")"
fi

dispatched=""
in_flight=""
case "$RUN_STATE" in
  *in_progress*|*queued*|*requested*|*waiting*|*pending*)
    in_flight="yes"
    log "waiting — a run for $STAMP is already $RUN_STATE, not dispatching"
    ;;
  *)
    if [ -n "$NO_DISPATCH" ]; then
      log "would dispatch — suppressed by BRIEF_WATCHDOG_NO_DISPATCH"
    elif ! command -v gh >/dev/null 2>&1; then
      log "cannot dispatch — gh not on PATH"
    else
      # Mirrors run_daily_brief.sh:62-67 ON PURPOSE, and the duplication is the
      # point: `gh workflow run` with no -f still sends the input's DECLARED
      # DEFAULT of '24', which lands in BRIEF_HOURS and overrides the script's
      # own Monday rule. Omitting the flag here would silently drop a whole
      # weekend of news from every Monday recovery.
      [ "$(date +%u)" = "1" ] && HOURS=72 || HOURS=24
      if gh workflow run daily-brief.yml --repo "$REPO_SLUG" \
           -f hours="$HOURS" -f force=false >/dev/null 2>&1; then
        dispatched="yes"
        log "DISPATCHED daily-brief.yml (hours=$HOURS) — no $STAMP.md after ${WAIT_MIN}m | last run: $GH_STATUS"
      else
        log "dispatch FAILED — gh workflow run returned non-zero | last run: $GH_STATUS"
      fi
    fi
    ;;
esac

# Give the dispatched (or already-running) job time to land the file. Gate on
# in_flight, NOT on RUN_STATE being non-empty: RUN_STATE is the joined statuses
# of every run created today, so on any day with a finished run it reads
# "completed,completed,..." — non-empty, and nothing pending. Waiting on that
# is a 12-minute hold for a job that already ended. Caught by running the real
# script against the real API; every unit test had passed.
if [ -n "$dispatched" ] || [ -n "$in_flight" ]; then
  log "waiting up to ${RECOVER_MIN}m for the run to land $STAMP.md"
  if poll_for_brief "$RECOVER_SEC"; then
    pull_local
    log "RECOVERED — $STAMP.md on origin/$BRANCH after dispatch"
    osascript -e "display notification \"Brief for $STAMP recovered after the schedule missed\" with title \"Vertigo\"" 2>/dev/null || true
    exit 0
  fi
fi

log "ALARM — no $STAMP.md on origin/$BRANCH after ${WAIT_MIN}m | last run: $GH_STATUS | dispatch: ${dispatched:-none}"

MSG="No brief for $STAMP.

Nothing on origin/$BRANCH of the briefs repo.
Last Actions run: $GH_STATUS
Recovery dispatch: ${dispatched:-not sent}

Check: gh run list --repo $REPO_SLUG --workflow=daily-brief.yml"

osascript -e "display notification \"No brief for $STAMP\" with title \"Vertigo brief MISSING\"" 2>/dev/null || true
osascript <<AS 2>/dev/null || true
tell application "System Events"
  display dialog "$MSG" with title "Vertigo brief MISSING" buttons {"OK"} default button 1 with icon caution giving up after 1800
end tell
AS
exit 1
