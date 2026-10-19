"""scripts/check_brief_landed.sh — detection, and now recovery.

On 2026-08-27 all three crons across two workflows dropped together (05:30 and
07:15 for the brief, 03:00 for the tests) while every workflow_dispatch and
push that day succeeded. daily-brief.yml calls its second cron a RETRY; it is
not one, because it rides the mechanism that failed. The watchdog detected the
miss correctly and could only alarm, and its alarm could not even say what
GitHub thought had happened:

    2026-08-27T09:36:44+02:00 ALARM — no 2026-08-27.md ... | last run: (gh unavailable)

`gh` is in /usr/local/bin; launchd runs with PATH=/usr/bin:/bin:/usr/sbin:/sbin
and does not read a shell config. `git` is in /usr/bin, which is why only the
diagnostic went blind and the file check kept working.

Shell, so these drive it through bash against real git repositories, with `gh`,
`osascript` and (for the Monday rule) `date` stood in for on PATH. A test that
reached the real `gh` would dispatch a real run and send a real email.
"""
import os
import shutil
import subprocess
import textwrap
from datetime import date

import pytest

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "check_brief_landed.sh")

STAMP = date.today().isoformat()


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture
def briefs(tmp_path):
    """A briefs repo with an origin, so `git fetch origin master` resolves."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "--initial-branch=master")

    repo = tmp_path / "briefs"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True,
                   capture_output=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("x")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "push", "-q", "origin", "master")
    return repo


@pytest.fixture
def stubs(tmp_path, briefs):
    """gh / osascript / date stubs, first on PATH."""
    d = tmp_path / "bin"
    d.mkdir()

    (d / "osascript").write_text("#!/bin/bash\nexit 0\n")

    # Records every invocation, and can be told what `run list` returns, what
    # `workflow run` exits with, and whether a dispatch produces the brief.
    (d / "gh").write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo "$*" >> "{d}/gh.calls"
        if [ "$1" = "run" ] && [ "$2" = "list" ]; then
          case "$*" in
            *'select(.createdAt'*) echo "${{GH_STUB_TODAY_RUNS:-}}" ;;
            *) echo "${{GH_STUB_LAST_RUN:-completed success at 2026-08-26T05:58:49Z}}" ;;
          esac
          exit 0
        fi
        if [ "$1" = "workflow" ] && [ "$2" = "run" ]; then
          [ -n "${{GH_STUB_CREATES_BRIEF:-}}" ] && echo "brief" > "{briefs}/{STAMP}.md"
          exit "${{GH_STUB_DISPATCH_RC:-0}}"
        fi
        exit 0
        """))

    # Passes everything through except +%u, so STAMP and the wall clock stay real.
    (d / "date").write_text(textwrap.dedent("""\
        #!/bin/bash
        if [ "$1" = "+%u" ] && [ -n "${FAKE_DOW:-}" ]; then echo "$FAKE_DOW"; exit 0; fi
        exec /bin/date "$@"
        """))

    for f in ("osascript", "gh", "date"):
        os.chmod(d / f, 0o755)
    return d


def run(briefs, stubs, tmp_path, **env):
    e = dict(os.environ)
    e["PATH"] = f"{stubs}:{e['PATH']}"
    e.update({
        "BRIEFS_REPO": str(briefs),
        "VERTIGO_LOG_DIR": str(tmp_path / "log"),
        "BRIEF_WATCHDOG_WAIT_SEC": "2",
        "BRIEF_WATCHDOG_RECOVER_SEC": "2",
        "BRIEF_WATCHDOG_POLL_SEC": "1",
        "FAKE_DOW": "4",                       # a Thursday unless overridden
    })
    e.update({k: str(v) for k, v in env.items()})
    p = subprocess.run(["bash", SCRIPT], env=e, capture_output=True, text=True)
    log = tmp_path / "log" / "brief-watchdog.log"
    calls = stubs / "gh.calls"
    return (p.returncode,
            log.read_text() if log.exists() else "",
            calls.read_text() if calls.exists() else "")


# --------------------------------------------------------------- happy path

def test_brief_present_never_touches_gh(briefs, stubs, tmp_path):
    (briefs / f"{STAMP}.md").write_text("brief")
    rc, log, calls = run(briefs, stubs, tmp_path)
    assert rc == 0
    assert "ok —" in log
    assert calls == ""


def test_weekend_does_nothing(briefs, stubs, tmp_path):
    rc, log, calls = run(briefs, stubs, tmp_path, FAKE_DOW=6)
    assert rc == 0 and calls == ""


def test_a_non_git_briefs_dir_is_skipped(tmp_path, stubs, briefs):
    plain = tmp_path / "plain"
    plain.mkdir()
    rc, log, calls = run(plain, stubs, tmp_path)
    assert rc == 0
    assert "not a git repo" in log


# ------------------------------------------------------------------ dispatch

def test_a_missing_brief_dispatches_the_workflow(briefs, stubs, tmp_path):
    rc, log, calls = run(briefs, stubs, tmp_path)
    assert "workflow run daily-brief.yml" in calls
    assert "DISPATCHED" in log


def test_dispatch_asks_for_24_hours_on_a_weekday(briefs, stubs, tmp_path):
    _, _, calls = run(briefs, stubs, tmp_path, FAKE_DOW=4)
    assert "hours=24" in calls


def test_dispatch_asks_for_72_hours_on_a_monday(briefs, stubs, tmp_path):
    """`gh workflow run` with no -f still sends the input's declared default of
    '24', which lands in BRIEF_HOURS and overrides run_daily_brief.sh's own
    Monday rule. Omitting the flag would drop a whole weekend of news."""
    _, _, calls = run(briefs, stubs, tmp_path, FAKE_DOW=1)
    assert "hours=72" in calls
    assert "hours=24" not in calls


def test_a_run_already_in_flight_is_not_duplicated(briefs, stubs, tmp_path):
    """The scheduler being LATE is not the scheduler being absent."""
    rc, log, calls = run(briefs, stubs, tmp_path, GH_STUB_TODAY_RUNS="in_progress")
    assert "workflow run" not in calls
    assert "not dispatching" in log


def test_a_queued_run_is_not_duplicated_either(briefs, stubs, tmp_path):
    _, log, calls = run(briefs, stubs, tmp_path, GH_STUB_TODAY_RUNS="queued")
    assert "workflow run" not in calls
    assert "not dispatching" in log


def test_no_dispatch_escape_hatch(briefs, stubs, tmp_path):
    rc, log, calls = run(briefs, stubs, tmp_path, BRIEF_WATCHDOG_NO_DISPATCH="1")
    assert "workflow run" not in calls
    assert "would dispatch" in log
    assert rc == 1


# ------------------------------------------------------------------ recovery

def test_recovery_when_the_dispatch_produces_the_brief(briefs, stubs, tmp_path):
    rc, log, calls = run(briefs, stubs, tmp_path, GH_STUB_CREATES_BRIEF="1")
    assert rc == 0
    assert "RECOVERED" in log
    assert "ALARM" not in log


def test_alarm_when_the_dispatch_produces_nothing(briefs, stubs, tmp_path):
    rc, log, _ = run(briefs, stubs, tmp_path)
    assert rc == 1
    assert "ALARM" in log
    assert "dispatch: yes" in log


def test_alarm_when_the_dispatch_itself_fails(briefs, stubs, tmp_path):
    rc, log, _ = run(briefs, stubs, tmp_path, GH_STUB_DISPATCH_RC=1)
    assert rc == 1
    assert "dispatch FAILED" in log
    assert "dispatch: none" in log


# ------------------------------------------------------- the PATH regression

def test_the_alarm_carries_what_github_said(briefs, stubs, tmp_path):
    """The 2026-08-27 alarm could only say '(gh unavailable)'."""
    rc, log, _ = run(briefs, stubs, tmp_path,
                     GH_STUB_LAST_RUN="completed failure at 2026-08-27T05:58:00Z")
    assert "completed failure at 2026-08-27T05:58:00Z" in log
    assert "(gh unavailable)" not in log


def _minimal_bin(tmp_path):
    """A bin directory holding exactly what the script shells out to, and no gh.

    Reusing /usr/bin is not safe for that one test: GitHub runners have gh
    there, so "no gh anywhere" quietly became "an unauthenticated gh".
    """
    d = tmp_path / "minbin"
    d.mkdir(exist_ok=True)
    # `bash` is here because the test invokes the script as `bash SCRIPT`, so
    # the interpreter has to be resolvable on this PATH too.
    for tool in ("bash", "cat", "git", "mkdir", "mv", "sed", "sleep", "tail", "wc"):
        real = shutil.which(tool)
        assert real, f"{tool} not found on this system"
        link = d / tool
        if not link.exists():
            link.symlink_to(real)
    assert shutil.which("gh", path=str(d)) is None
    return str(d)


def test_gh_missing_is_named_and_does_not_pretend_to_dispatch(briefs, stubs, tmp_path):
    """No gh anywhere: say so instead of silently doing nothing.

    This test dispatched six real workflow runs on 2026-08-27. Removing the
    stub makes `command -v gh` fail, the script widens PATH to look for the
    real one, and it FOUND it — /usr/local/bin/gh, live credentials and all.
    They no-opped on BRIEF_SKIP_IF_EXISTS, which was the only thing standing
    between a unit test and six brief emails.

    So the widen list is now overridable and this points it at an empty
    directory. NO_DISPATCH is deliberately NOT set as a second layer: it is
    checked first, so it would mask the very branch under test. The assertion
    below is the guard instead — "cannot dispatch" can only be logged when gh
    was genuinely not found, so a regression in GH_PATHS turns this red.

    PATH is built from symlinks rather than reusing /usr/bin, because a GitHub
    runner ships `gh` AT /usr/bin/gh. Removing the stub while keeping /usr/bin
    on PATH found the runner's unauthenticated gh and tried to dispatch with
    it — green on a Mac, red in CI, and red for the right reason: this test is
    genuinely sensitive to whether gh is reachable, which is its whole point.
    """
    os.remove(stubs / "gh")
    empty = tmp_path / "nowhere"
    empty.mkdir()
    ee = dict(os.environ)
    ee["PATH"] = f"{stubs}:{_minimal_bin(tmp_path)}"
    ee.update({"BRIEFS_REPO": str(briefs),
               "VERTIGO_LOG_DIR": str(tmp_path / "log"),
               "BRIEF_WATCHDOG_WAIT_SEC": "2", "BRIEF_WATCHDOG_RECOVER_SEC": "2",
               "BRIEF_WATCHDOG_POLL_SEC": "1", "FAKE_DOW": "4",
               "BRIEF_WATCHDOG_GH_PATHS": str(empty)})
    p = subprocess.run(["bash", SCRIPT], env=ee, capture_output=True, text=True)
    log = (tmp_path / "log" / "brief-watchdog.log").read_text()
    assert p.returncode == 1
    assert "cannot dispatch — gh not on PATH" in log
    assert "(gh not on PATH)" in log
    assert "ALARM" in log


def test_finished_runs_today_do_not_trigger_a_recovery_wait(briefs, stubs, tmp_path):
    """RUN_STATE is the joined statuses of every run created today, so on any
    day that already had one it reads "completed,completed" — non-empty, and
    nothing pending. Gating the recovery poll on that held the job open for
    twelve minutes waiting on a job that had already ended.

    Every unit test passed with that bug in place; running the real script
    against the real API is what found it.
    """
    _, log, _ = run(briefs, stubs, tmp_path,
                    GH_STUB_TODAY_RUNS="completed,completed",
                    BRIEF_WATCHDOG_NO_DISPATCH="1")
    assert "would dispatch" in log
    assert "waiting up to" not in log
    assert "ALARM" in log


def test_a_dispatch_does_wait_for_it(briefs, stubs, tmp_path):
    _, log, _ = run(briefs, stubs, tmp_path, GH_STUB_TODAY_RUNS="completed")
    assert "DISPATCHED" in log
    assert "waiting up to" in log
