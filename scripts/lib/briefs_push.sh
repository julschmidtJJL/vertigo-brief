# Stage, commit and push into the briefs repo. Sourced, not executed.
#
#   . "$REPO/scripts/lib/briefs_push.sh"
#   briefs_push "$OUT_DIR" "brief: $STAMP" "$STAMP.md" decisions.jsonl
#
# Returns 0 on success or when there was nothing to commit; non-zero when the
# work could not be pushed. Callers are expected to treat non-zero as fatal.
#
# WHY THIS EXISTS AT ALL. run_daily_brief.sh and a weekly job each carried
# their own copy of these three git commands, and the copies had already drifted
# — the weekly job raised a desktop alert on push failure, the daily brief only
# wrote a WARN. Both copies shared three defects:
#
#   1. `git add "$STAMP.md"` names ONE file. Anything else written into the repo
#      was silently never staged, and on an Actions runner the workspace is
#      destroyed at job end, so "written" meant "lost". No warning, no trace.
#   2. No fetch or rebase before pushing. The two workflows declare SEPARATE
#      concurrency groups, so a manually dispatched scoreboard can overlap a
#      scheduled brief; the loser's push is a non-fast-forward.
#   3. A failed push was a WARN. "Committed locally" on a runner is not a
#      fallback, it is data loss with a reassuring log line.
#
# The rebase is safe because every file has exactly ONE writer. Today: the daily
# job stages $STAMP.md and decisions.jsonl, the weekly job stages
# scoreboard-$STAMP.md, and the markdown artifacts are date-named. Two jobs never
# touch one file, so a rebase never has a conflict to resolve.
#
# THAT INVARIANT IS THE SAFETY ARGUMENT. Anything added to either caller's file
# list must keep it, or this helper will hit a conflict it cannot resolve
# unattended — which is why the conflict path below aborts loudly rather than
# guessing.

BRIEFS_PUSH_ATTEMPTS="${BRIEFS_PUSH_ATTEMPTS:-3}"
BRIEFS_PUSH_TIMEOUT="${BRIEFS_PUSH_TIMEOUT:-120}"
BRIEFS_PUSH_BACKOFF="${BRIEFS_PUSH_BACKOFF:-2}"   # 0 in tests, so retries cost nothing

# WALL-CLOCK DEADLINE for a single git network call.
#
# master wrapped the push in `run_step 120`, removed in the commit that added
# this helper, which left `git push` unbounded.
# run_step cannot simply be reused: it is defined in run_daily_brief.sh only,
# and a second job (a weekly scoreboard, kept private) sources this file too.
#
# Counted against `date`, not a sleep-iteration tally. During macOS system
# sleep timers suspend, so a `sleep 2` spanning an hour of suspend still adds
# 2 — this repo lost 2h04m to exactly that on 2026-08-21. Actions caps a job at
# 30 minutes; launchd caps nothing.
_bp_deadline() {
  local secs="$1"; shift
  # `set -m` gives the background job its OWN process group, so the kill below
  # can target the group rather than just the direct child. git push over SSH
  # spawns an ssh child: killing only git leaves that ssh alive, still holding
  # the inherited log fd. Measured 2026-08-24 with a stub that spawns `sleep
  # 60` — a pid-only kill returned in 2s but the caller's pipe stayed open the
  # full 60. Restored immediately; job control off is the default here.
  local had_m=""; case "$-" in *m*) had_m=1 ;; esac
  set -m
  "$@" &
  local pid=$!
  [ -n "$had_m" ] || set +m
  local stop=$(( $(date +%s) + secs ))
  while kill -0 "$pid" 2>/dev/null && [ "$(date +%s)" -lt "$stop" ]; do
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    _bp_log "push: git call exceeded ${secs}s and was killed"
    return 124
  fi
  wait "$pid"; return $?
}

_bp_log() {
  if [ -n "${LOG:-}" ] && [ -w "$(dirname "$LOG")" ] 2>/dev/null; then
    echo "$*" >> "$LOG"
  else
    echo "$*" >&2
  fi
}

briefs_push() {
  local dir="$1" msg="$2"; shift 2
  local git=/usr/bin/git

  [ -d "$dir/.git" ] || { _bp_log "push: $dir is not a git repo — skipped"; return 0; }

  # A push that PROMPTS is a hang: no terminal here, nobody to answer.
  export GIT_TERMINAL_PROMPT=0

  # Stage only what exists. A named file that is absent is NOT fatal on its own
  # — decisions.jsonl is written only when a rule actually moved, so most
  # mornings there is nothing to add — but it IS reported, because "the file I
  # expected is missing" and "nothing happened today" look identical in a log
  # that says neither. Note also that `git add` aborts the WHOLE add on a single
  # non-matching pathspec, so passing an absent file would silently stage none
  # of the others.
  local f staged=0
  for f in "$@"; do
    if [ -e "$dir/$f" ]; then
      if "$git" -C "$dir" add -- "$f" >> "${LOG:-/dev/null}" 2>&1; then
        staged=$((staged + 1))
      else
        # FATAL, not skipped. A file that EXISTS and cannot be staged is a
        # different event from one that was never written: disk full, a held
        # index.lock, a corrupt index. Counting it as "nothing to stage" would
        # return success and commit-and-push the OTHER files — a partial
        # artifact, reported green, with the missing piece unrecoverable. That
        # is the exact silent-loss class this helper exists to remove.
        _bp_log "push: FAILED to stage $f — it exists but git add refused"
        return 1
      fi
    else
      _bp_log "push: $f not present this run"
    fi
  done
  [ "$staged" -gt 0 ] || { _bp_log "push: nothing to stage"; return 0; }

  # Nothing to commit is success, not failure: the retry run re-stages files
  # the first attempt already committed and pushed.
  if "$git" -C "$dir" diff --cached --quiet; then
    _bp_log "push: nothing to commit"
    return 0
  fi

  "$git" -C "$dir" commit -q -m "$msg" >> "${LOG:-/dev/null}" 2>&1 || {
    _bp_log "push: commit FAILED"; return 1; }

  local n=1
  while [ "$n" -le "$BRIEFS_PUSH_ATTEMPTS" ]; do
    if _bp_deadline "$BRIEFS_PUSH_TIMEOUT" "$git" -C "$dir" push --quiet origin HEAD >> "${LOG:-/dev/null}" 2>&1; then
      [ "$n" -gt 1 ] && _bp_log "push: pushed on attempt $n" || _bp_log "push: pushed"
      return 0
    fi
    if [ "$n" -lt "$BRIEFS_PUSH_ATTEMPTS" ]; then
      _bp_log "push: attempt $n rejected, rebasing onto origin"
      _bp_deadline "$BRIEFS_PUSH_TIMEOUT" "$git" -C "$dir" fetch --quiet origin >> "${LOG:-/dev/null}" 2>&1
      if ! "$git" -C "$dir" rebase --quiet FETCH_HEAD >> "${LOG:-/dev/null}" 2>&1; then
        # Should not happen while every file has one writer. If it does, that
        # invariant broke, and leaving a half-finished rebase behind would wedge
        # every future run of both jobs.
        "$git" -C "$dir" rebase --abort >> "${LOG:-/dev/null}" 2>&1
        _bp_log "push: REBASE CONFLICT in $dir — the one-writer invariant broke"
        return 1
      fi
      sleep "$BRIEFS_PUSH_BACKOFF"
    fi
    n=$((n + 1))
  done

  _bp_log "push: FAILED after $BRIEFS_PUSH_ATTEMPTS attempts — work is committed"
  _bp_log "push: on an ephemeral runner that means it is GONE"
  return 1
}
