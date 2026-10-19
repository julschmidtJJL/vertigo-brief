"""scripts/lib/briefs_push.sh — the only thing standing between a written
artifact and a deleted runner workspace.

Both runners used to carry their own copy of three git commands, and the copies
had drifted. All three defects are covered here:

  1. `git add "$STAMP.md"` names ONE file, so anything else written into the
     repo was silently never staged. `git add` also aborts the WHOLE add on a
     single non-matching pathspec, so naively adding an optional file would
     have staged none of the others.
  2. No fetch or rebase before pushing. The two workflows declare separate
     concurrency groups, so a manually dispatched scoreboard can overlap a
     scheduled brief and the loser gets a non-fast-forward.
  3. A failed push was a WARN. On an ephemeral runner "committed locally" is
     data loss with a reassuring log line.

Shell, so these tests drive it through bash against real git repositories
rather than mocking git.
"""
import os
import subprocess
import textwrap
import time

import pytest

HELPER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "lib", "briefs_push.sh")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


def call(workdir, repo, msg, *files, env=None):
    """Source the helper and invoke it, the way the runners do."""
    script = textwrap.dedent(f"""
        . "{HELPER}"
        briefs_push "{repo}" "{msg}" {' '.join(files)}
    """)
    e = dict(os.environ, LOG="/dev/null")
    e.update(env or {})
    return subprocess.run(["bash", "-c", script], cwd=str(workdir),
                          capture_output=True, text=True, env=e)


@pytest.fixture
def repos(tmp_path):
    """A bare origin and a clone, as the runner sees it after actions/checkout."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    clone = tmp_path / "briefs"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    git(clone, "config", "user.email", "t@example.com")
    git(clone, "config", "user.name", "t")
    (clone / "seed").write_text("seed\n")
    git(clone, "add", "seed")
    git(clone, "commit", "-q", "-m", "seed")
    git(clone, "push", "-q", "origin", "HEAD")
    return {"origin": origin, "clone": clone, "tmp": tmp_path}


class TestHappyPath:
    def test_stages_commits_and_pushes(self, repos):
        (repos["clone"] / "2026-08-24.md").write_text("# brief\n")
        r = call(repos["tmp"], repos["clone"], "brief: 2026-08-24", "2026-08-24.md")
        assert r.returncode == 0, r.stderr

        log = git(repos["origin"], "log", "--oneline", "-1").stdout
        assert "brief: 2026-08-24" in log

    def test_every_named_file_reaches_the_remote(self, repos):
        """The defect this replaced: only the first file was ever named."""
        (repos["clone"] / "2026-08-24.md").write_text("# brief\n")
        (repos["clone"] / "decisions.jsonl").write_text('{"fid":"x"}\n')
        assert call(repos["tmp"], repos["clone"], "brief: x",
                    "2026-08-24.md", "decisions.jsonl").returncode == 0

        files = git(repos["origin"], "ls-tree", "--name-only", "HEAD").stdout.split()
        assert "2026-08-24.md" in files
        assert "decisions.jsonl" in files


class TestAbsentFiles:
    def test_an_absent_optional_file_does_not_block_the_others(self, repos):
        """decisions.jsonl is written only when a rule moved, so most mornings
        it is absent. `git add` aborts the whole add on one bad pathspec, so a
        naive implementation would have staged NOTHING on a quiet day."""
        (repos["clone"] / "2026-08-24.md").write_text("# brief\n")
        r = call(repos["tmp"], repos["clone"], "brief: x",
                 "2026-08-24.md", "decisions.jsonl")
        assert r.returncode == 0

        assert "2026-08-24.md" in git(repos["origin"], "ls-tree",
                                      "--name-only", "HEAD").stdout

    def test_nothing_to_stage_is_success_not_failure(self, repos):
        r = call(repos["tmp"], repos["clone"], "brief: x", "does-not-exist.md")
        assert r.returncode == 0

    def test_nothing_to_commit_is_success(self, repos):
        """The retry re-stages what the first attempt already pushed. That is
        the normal case on a weekday, not an error."""
        (repos["clone"] / "2026-08-24.md").write_text("# brief\n")
        assert call(repos["tmp"], repos["clone"], "b", "2026-08-24.md").returncode == 0
        assert call(repos["tmp"], repos["clone"], "b", "2026-08-24.md").returncode == 0


class TestTheRace:
    def test_a_non_fast_forward_is_rebased_and_retried(self, repos):
        """The scoreboard, dispatched manually, pushes while the daily brief is
        mid-run. Before the rebase the brief's push was rejected and its commit
        died with the runner."""
        other = repos["tmp"] / "other"
        subprocess.run(["git", "clone", "-q", str(repos["origin"]), str(other)],
                       check=True)
        git(other, "config", "user.email", "o@example.com")
        git(other, "config", "user.name", "o")
        (other / "scoreboard-2026-08-23.md").write_text("# scoreboard\n")
        git(other, "add", "scoreboard-2026-08-23.md")
        git(other, "commit", "-q", "-m", "scoreboard: 2026-08-23")
        assert git(other, "push", "-q", "origin", "HEAD").returncode == 0

        # Our clone is now behind and does not know it.
        (repos["clone"] / "2026-08-24.md").write_text("# brief\n")
        r = call(repos["tmp"], repos["clone"], "brief: 2026-08-24", "2026-08-24.md")
        assert r.returncode == 0, r.stderr

        files = git(repos["origin"], "ls-tree", "--name-only", "HEAD").stdout.split()
        assert "2026-08-24.md" in files            # ours landed
        assert "scoreboard-2026-08-23.md" in files  # theirs survived


class TestFailureIsLoud:
    def test_an_unreachable_remote_returns_non_zero(self, repos):
        """The whole point. A WARN here used to be the last word, and on a
        runner the workspace is deleted minutes later."""
        git(repos["clone"], "remote", "set-url", "origin",
            str(repos["tmp"] / "nowhere.git"))
        (repos["clone"] / "2026-08-24.md").write_text("# brief\n")

        r = call(repos["tmp"], repos["clone"], "brief: x", "2026-08-24.md",
                 env={"BRIEFS_PUSH_ATTEMPTS": "1"})
        assert r.returncode != 0

    def test_a_non_git_directory_is_skipped_not_failed(self, repos):
        """Local runs against a plain ~/briefs must not start failing."""
        plain = repos["tmp"] / "plain"
        plain.mkdir()
        (plain / "2026-08-24.md").write_text("# brief\n")
        assert call(repos["tmp"], plain, "brief: x", "2026-08-24.md").returncode == 0


class TestTheDeadline:
    """master wrapped the push in `run_step 120` (run_daily_brief.sh:553) and the
    first version of this helper dropped it. Actions caps a job at 30 minutes;
    launchd caps nothing, and this repo already lost 2h04m to an unbounded hang."""

    def _hang(self, tmp_path, secs, tag):
        """Run one deadline against a stub that never returns.

        Output goes to a FILE, matching production. Both callers invoke this
        helper with `>> "$LOG"`, and $LOG is a file. That matters for what this
        test can measure: a killed process's grandchildren (git push spawns
        ssh) may briefly outlive the group kill, and if the caller is reading a
        PIPE it blocks on that fd until they exit — which measures the stub's
        lifetime, not the deadline's. Against a file there is no such coupling,
        so the elapsed time here is the deadline's own.
        `test_the_work_is_actually_stopped` covers the reaping.
        """
        fake_git = tmp_path / f"slowgit-{tag}"
        fake_git.write_text("#!/bin/sh\nsleep 60\n")
        fake_git.chmod(0o755)
        log = tmp_path / f"run-{tag}.log"

        script = textwrap.dedent(f"""
            . "{HELPER}"
            _bp_deadline {secs} "{fake_git}" push >> "{log}" 2>&1
        """)
        t0 = time.monotonic()
        r = subprocess.run(["bash", "-c", script],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=dict(os.environ, LOG=str(log)))
        return r.returncode, time.monotonic() - t0, log.read_text()

    def test_a_hung_git_call_is_killed_and_reported(self, repos, tmp_path):
        """The argument is honoured, proved by COMPARING two deadlines.

        A single run cannot prove it. The deadline is counted against
        `date +%s` and `stop` is computed from floor(now), so a call asking for
        N is killed somewhere in [N-1, N]: when the helper's first `date` lands
        late in a second, the loop exits after ONE `sleep 1`. Measured
        2026-08-26 by sweeping the start offset across a second boundary — a 2s
        deadline returns in 1.1s at offsets around .82 and .92, and in 2.1s
        everywhere else. That is the helper working as specified, and it is
        what the old `assert 2 <= elapsed < 6` was hitting under load (elapsed
        1.1s, rc 124, log correct). A one-second clock never promised the
        precision that bound asked for, and [1, 2] for a 2s deadline overlaps
        what a deadline hardcoded to 1 would produce — so no single-run lower
        bound buys the property either.

        The gap does. A deadline that ignores its argument — the mutant the old
        lower bound existed for, per `a bare upper bound of 15 also passes for
        a deadline that ignores its argument and hardcodes 10` — moves both
        numbers together and cannot open one. Load moves them together too,
        which is why this survives a loaded run.
        """
        short_rc, short_elapsed, short_log = self._hang(tmp_path, 2, "short")
        long_rc, long_elapsed, long_log = self._hang(tmp_path, 8, "long")

        assert short_rc == 124, "a hung git call must report the timeout code"
        assert long_rc == 124, "a hung git call must report the timeout code"
        assert "exceeded 2s" in short_log
        assert "exceeded 8s" in long_log

        # Capped, so a deadline that overshoots its argument is still caught.
        # Generous, because load only ever pushes these UP.
        assert short_elapsed < 6, f"2s deadline took {short_elapsed:.1f}s"
        assert long_elapsed < 12, f"8s deadline took {long_elapsed:.1f}s"

        # The property the old lower bound was buying. Expected gap is ~6s
        # (>=4.8s even at the [N-1, N] floor); 3 leaves room for load without
        # leaving room for a constant.
        assert long_elapsed - short_elapsed >= 3, (
            "deadline did not honour its argument: 2s took "
            f"{short_elapsed:.1f}s, 8s took {long_elapsed:.1f}s")

    def test_the_work_is_actually_stopped_not_just_abandoned(self, repos, tmp_path):
        """The kill must reach grandchildren. `git push` spawns `ssh`; killing
        only the direct child leaves that ssh running against the remote."""
        fake_git = tmp_path / "slowgit"
        marker = tmp_path / "still-running"
        # A DETACHED GRANDCHILD. The earlier version of this test had the direct
        # child touch the marker, which a pid-only kill already prevents — so it
        # passed against exactly the bug it was named for. Only a process-group
        # kill reaps this one, which is the real `git push` spawns `ssh` shape.
        fake_git.write_text(f"#!/bin/sh\n( sleep 5; touch {marker} ) &\nsleep 30\n")
        fake_git.chmod(0o755)
        log = tmp_path / "run.log"

        script = textwrap.dedent(f"""
            . "{HELPER}"
            _bp_deadline 1 "{fake_git}" push >> "{log}" 2>&1
        """)
        subprocess.run(["bash", "-c", script],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=dict(os.environ, LOG=str(log)))
        time.sleep(12)                       # well past the stub's own 8s
        assert not marker.exists(), \
            "the stub outlived the deadline — descendants were not reaped"

    def test_a_fast_call_is_not_penalised(self, repos, tmp_path):
        script = textwrap.dedent(f"""
            . "{HELPER}"
            _bp_deadline 10 true
        """)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env=dict(os.environ, LOG="/dev/null"))
        assert r.returncode == 0


class TestTheFatalPaths:
    """The three properties that survived a mutation of this file undetected on
    2026-08-24, because none of them had a test. Each one removes a `return 1`."""

    def test_a_file_that_exists_but_cannot_be_staged_is_fatal(self, repos):
        """A partial artifact reported green is the failure this helper removes.
        An ignored path is the cheapest real trigger: `git add --` on one exits
        non-zero, exactly as a held index.lock or a full disk would."""
        (repos["clone"] / ".gitignore").write_text("decisions.jsonl\n")
        git(repos["clone"], "add", ".gitignore")
        git(repos["clone"], "commit", "-q", "-m", "ignore")
        (repos["clone"] / "2026-08-24.md").write_text("# brief\n")
        (repos["clone"] / "decisions.jsonl").write_text('{"fid":"x"}\n')

        r = call(repos["tmp"], repos["clone"], "brief: x",
                 "2026-08-24.md", "decisions.jsonl")

        assert r.returncode != 0, "an unstageable file must not report success"
        pushed = git(repos["origin"], "ls-tree", "--name-only", "HEAD").stdout.split()
        assert "2026-08-24.md" not in pushed, "a partial artifact was pushed as green"

    def test_a_rebase_conflict_returns_immediately_and_leaves_no_wedge(self, repos):
        """decisions.jsonl is append-only and shared across daily runs, so a
        stale checkout racing a landed push DOES conflict. The helper must say
        so and stop, not burn its attempts, and must not strand a half-finished
        rebase that would wedge every future run of both jobs."""
        (repos["clone"] / "decisions.jsonl").write_text('{"a":1}\n')
        git(repos["clone"], "add", "decisions.jsonl")
        git(repos["clone"], "commit", "-q", "-m", "seed log")
        git(repos["clone"], "push", "-q", "origin", "HEAD")

        other = repos["tmp"] / "other"
        subprocess.run(["git", "clone", "-q", str(repos["origin"]), str(other)], check=True)
        git(other, "config", "user.email", "o@e.com")
        git(other, "config", "user.name", "o")
        (other / "decisions.jsonl").write_text('{"a":1}\n{"b":2}\n')
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "other run landed first")
        git(other, "push", "-q", "origin", "HEAD")

        (repos["clone"] / "decisions.jsonl").write_text('{"a":1}\n{"c":3}\n')
        log = repos["tmp"] / "conflict.log"
        r = call(repos["tmp"], repos["clone"], "brief: x", "decisions.jsonl",
                 env={"BRIEFS_PUSH_ATTEMPTS": "3", "BRIEFS_PUSH_BACKOFF": "0",
                      "LOG": str(log)})

        assert r.returncode != 0
        text = log.read_text()
        # `returncode != 0` alone is too weak: with the conflict swallowed the
        # loop burns its attempts and still ends non-zero. What must be pinned
        # is that the helper NAMES the broken invariant and stops there.
        assert "REBASE CONFLICT" in text, \
            f"the conflict was swallowed; operator sees only a generic failure:\n{text}"
        assert "FAILED after" not in text, \
            "it burned every attempt instead of stopping at the conflict"
        assert not (repos["clone"] / ".git" / "rebase-merge").exists(), "wedged rebase"
        assert not (repos["clone"] / ".git" / "rebase-apply").exists(), "wedged rebase"
