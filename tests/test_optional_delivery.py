"""Newsletters and email are optional, and independent of each other.

The brief has three tiers of setup:

    minimal      OPENAI_API_KEY only. Markdown file on disk, General news
                 omitted, no email.
    + email      SMTP_USER / SMTP_PASSWORD (or, for the original one-account
                 setup, the newsletter mailbox's IMAP credentials).
    + newsletters  IMAP credentials and NEWSLETTER_SENDERS.

Until this file existed, email reused the IMAP credentials and nothing else,
so no newsletter mailbox meant no email; and NEWSLETTER_SENDERS defaulted to
the author's own subscriptions. These tests pin the decoupling, and drive
run_daily_brief.sh end to end in each tier with every network step stood in
for, so a tier that degrades loudly, or not at all, fails here first.
"""
import os
import smtplib
import subprocess
import sys
import textwrap

import pytest

import market_brief_newsletters as nl
import send_brief

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "scripts", "run_daily_brief.sh")

GMAIL_PW = "abcdefghijklmnop"          # the shape of an app password, not one
MAIL_KEYS = ("IMAP_USER", "IMAP_APP_PASSWORD", "IMAP_MAILBOX", "IMAP_HOST",
             "IMAP_PORT", "NEWSLETTER_SENDERS", "SMTP_USER", "SMTP_PASSWORD",
             "SMTP_HOST", "SMTP_PORT", "SMTP_FROM", "BRIEF_MAIL_TO", "BRIEF_MAIL")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No mail setting from the developer's own shell may leak in, and no
    credential file from their own ~/.config/vertigo may be read."""
    for k in MAIL_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(send_brief, "MAIL_ENV", str(tmp_path / "mail.env"))
    monkeypatch.setattr(send_brief, "DEFAULT_ENV", str(tmp_path / "newsletter.env"))
    return tmp_path


# --- send_brief: which account sends ----------------------------------------

def test_smtp_settings_win_over_the_newsletter_mailbox():
    c = send_brief.mail_config({}, {
        "SMTP_USER": "out@example.org", "SMTP_PASSWORD": "pw",
        "IMAP_USER": "in@gmail.com", "IMAP_APP_PASSWORD": GMAIL_PW,
        "SMTP_HOST": "smtp.example.org", "SMTP_PORT": "465"})
    assert (c["user"], c["pw"], c["source"]) == ("out@example.org", "pw", "SMTP")
    assert (c["host"], c["port"]) == ("smtp.example.org", 465)
    assert c["to"] == "out@example.org"


def test_imap_credentials_still_send_when_no_smtp_is_set():
    """The original setup: one Gmail account reads the newsletters and sends
    the brief. Existing secrets must keep working unchanged."""
    c = send_brief.mail_config({}, {
        "IMAP_USER": "in@gmail.com", "IMAP_APP_PASSWORD": "abcd efgh ijkl mnop",
        "BRIEF_MAIL_TO": "me@example.org"})
    assert (c["user"], c["pw"], c["source"]) == ("in@gmail.com", GMAIL_PW, "IMAP")
    assert (c["host"], c["port"]) == ("smtp.gmail.com", 587)
    assert c["to"] == "me@example.org"


def test_half_an_smtp_pair_never_borrows_the_imap_secret():
    c = send_brief.mail_config({}, {
        "SMTP_USER": "out@example.org",
        "IMAP_USER": "in@gmail.com", "IMAP_APP_PASSWORD": GMAIL_PW})
    assert c["user"] == "out@example.org"
    assert c["pw"] == ""


def test_file_wins_over_environment():
    c = send_brief.mail_config({"SMTP_USER": "file@x", "SMTP_PASSWORD": "f"},
                               {"SMTP_USER": "env@x", "SMTP_PASSWORD": "e"})
    assert (c["user"], c["pw"]) == ("file@x", "f")


def test_mail_env_is_read_before_newsletter_env(tmp_path):
    a, b = tmp_path / "mail.env", tmp_path / "newsletter.env"
    a.write_text("SMTP_USER=out@example.org\n")
    b.write_text(f"SMTP_USER=old@example.org\nIMAP_USER=in@gmail.com\n")
    os.chmod(a, 0o600)
    os.chmod(b, 0o600)
    env, err = send_brief.load_files([str(a), str(b)])
    assert err is None
    assert env == {"SMTP_USER": "out@example.org", "IMAP_USER": "in@gmail.com"}


def test_missing_files_are_not_an_error(tmp_path):
    env, err = send_brief.load_files([str(tmp_path / "nope.env")])
    assert (env, err) == ({}, None)


# --- send_brief: what main() does in each case -------------------------------

class FakeSMTP:
    calls = []

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.calls.append(("connect", type(self).__name__, host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        FakeSMTP.calls.append(("starttls",))

    def login(self, user, pw):
        FakeSMTP.calls.append(("login", user, pw))

    def send_message(self, msg):
        FakeSMTP.calls.append(("send", msg["From"], msg["To"]))


class FakeSMTP_SSL(FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.calls = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP_SSL)
    return FakeSMTP.calls


def run_send(monkeypatch, tmp_path):
    md = tmp_path / "2026-10-19.md"
    md.write_text("# Brief\n\n## `AI` Judgement\n\n*note*\n\nNothing today.\n")
    monkeypatch.setattr(sys, "argv", ["send_brief.py", str(md)])
    return send_brief.main()


def test_nothing_configured_is_exit_2_and_connects_to_nothing(clean_env, smtp, monkeypatch):
    assert run_send(monkeypatch, clean_env) == send_brief.NOT_CONFIGURED
    assert smtp == []


def test_own_smtp_account_over_tls(clean_env, smtp, monkeypatch):
    for k, v in {"SMTP_USER": "out@example.org", "SMTP_PASSWORD": "Any-Shape-1",
                 "SMTP_HOST": "smtp.example.org", "SMTP_PORT": "465",
                 "SMTP_FROM": "Brief <out@example.org>",
                 "BRIEF_MAIL_TO": "me@example.org"}.items():
        monkeypatch.setenv(k, v)
    assert run_send(monkeypatch, clean_env) == 0
    assert smtp == [("connect", "FakeSMTP_SSL", "smtp.example.org", 465),
                    ("login", "out@example.org", "Any-Shape-1"),
                    ("send", "Brief <out@example.org>", "me@example.org")]


def test_newsletter_mailbox_fallback_uses_gmail_starttls(clean_env, smtp, monkeypatch):
    monkeypatch.setenv("IMAP_USER", "in@gmail.com")
    monkeypatch.setenv("IMAP_APP_PASSWORD", GMAIL_PW)
    assert run_send(monkeypatch, clean_env) == 0
    assert smtp[:3] == [("connect", "FakeSMTP", "smtp.gmail.com", 587),
                        ("starttls",), ("login", "in@gmail.com", GMAIL_PW)]


def test_gmail_shape_check_refuses_before_login(clean_env, smtp, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "out@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "not-an-app-password")
    assert run_send(monkeypatch, clean_env) == 1
    assert smtp == []


def test_half_configured_is_a_failure_not_off(clean_env, smtp, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "out@example.org")
    assert run_send(monkeypatch, clean_env) == 1
    assert smtp == []


# --- newsletters: none unless asked for --------------------------------------

def test_no_default_sender_list():
    assert nl.parse_senders(None) == []
    assert nl.parse_senders("") == []
    assert nl.parse_senders(" A.com, ,b.de ") == ["a.com", "b.de"]


def run_fetch(monkeypatch, tmp_path, *args):
    import io, json
    monkeypatch.setattr(sys, "argv", ["market_brief_newsletters.py",
                                      "--env", str(tmp_path / "newsletter.env"), *args])
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    nl.main()
    return json.loads(buf.getvalue())


@pytest.fixture
def no_imap(monkeypatch):
    """Records connection attempts and refuses them, so no test reaches Gmail."""
    seen = []

    def refuse(host, port, timeout=None):
        seen.append((host, port))
        raise ConnectionRefusedError

    monkeypatch.setattr(nl.imaplib, "IMAP4_SSL", refuse)
    return seen


def test_no_senders_means_no_login_even_with_a_mailbox(clean_env, no_imap, monkeypatch):
    monkeypatch.setenv("IMAP_USER", "in@gmail.com")
    monkeypatch.setenv("IMAP_APP_PASSWORD", GMAIL_PW)
    out = run_fetch(monkeypatch, clean_env)
    assert out["ok"] is False and out["configured"] is False
    assert no_imap == []


def test_senders_from_the_credential_file_are_used(clean_env, no_imap, monkeypatch):
    f = clean_env / "newsletter.env"
    f.write_text(f"IMAP_USER=in@gmail.com\nIMAP_APP_PASSWORD={GMAIL_PW}\n"
                 "NEWSLETTER_SENDERS=example-paper.com\n")
    os.chmod(f, 0o600)
    out = run_fetch(monkeypatch, clean_env)
    assert "could not reach" in out["error"]
    assert no_imap == [("imap.gmail.com", 993)]


def test_discover_runs_without_a_sender_list(clean_env, no_imap, monkeypatch):
    """--discover is how the list gets written in the first place."""
    monkeypatch.setenv("IMAP_USER", "in@gmail.com")
    monkeypatch.setenv("IMAP_APP_PASSWORD", GMAIL_PW)
    run_fetch(monkeypatch, clean_env, "--discover")
    assert no_imap == [("imap.gmail.com", 993)]


# --- run_daily_brief.sh, end to end, per tier --------------------------------
#
# A stand-in python3 answers for every step that would touch Yahoo or OpenAI,
# and hands the newsletter fetcher and the sender to the real interpreter, so
# their degradation is the real code's. Mail and IMAP hosts point at a closed
# local port: a configured tier is ATTEMPTED and fails fast, which is the
# distinction under test (attempted vs. switched off).

def _closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def runner(tmp_path):
    stub = tmp_path / "bin" / "python3"
    stub.parent.mkdir()
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        REAL="{sys.executable}"
        if [ "$1" = "-c" ]; then
          case "$2" in *yfinance*) exit 0 ;; esac
          exec "$REAL" "$@"
        fi
        case "$(basename "$1")" in
          falsifier_monitor.py) printf '## `ƒ` Falsifier monitor\\n\\n0 triggered · 0 closing\\n' ;;
          regime_strip.py) printf '## `ƒ` Regime strip\\n' ;;
          market_brief_gather.py)
            while [ $# -gt 0 ]; do
              [ "$1" = "--table-out" ] && printf '## `ƒ` Positions\\n' > "$2"; shift
            done
            echo '{{"companies": []}}' ;;
          brief_ai.py) cp "$2" "{tmp_path}/prompt.txt"; echo "Nothing today." ;;
          market_brief_newsletters.py)
            if [ -n "${{STUB_NEWSLETTERS:-}}" ]; then
              echo '{{"ok": true, "count": 1, "mailbox": "x / INBOX", "newsletters": [{{"source": "example-paper.com", "text": "t"}}]}}'
            else exec "$REAL" "$@"; fi ;;
          *) exec "$REAL" "$@" ;;
        esac
        """))
    stub.chmod(0o755)

    def run(**extra):
        env = {k: v for k, v in os.environ.items()
               if k not in MAIL_KEYS and k != "OPENAI_API_KEY"}
        env.update({
            "HOME": str(tmp_path / "home"),          # no ~/.config/vertigo
            "VERTIGO_PYTHON": str(stub),
            "VERTIGO_HOME": str(tmp_path / "vhome"),
            "BRIEFS_DIR": str(tmp_path / "briefs"),
            "VERTIGO_LOG_DIR": str(tmp_path / "log"),
            "BRIEF_HOURS": "24",
        })
        env.update(extra)
        for d in ("home", "vhome"):
            (tmp_path / d).mkdir(exist_ok=True)
        p = subprocess.run(["bash", RUNNER], env=env, capture_output=True,
                           text=True, timeout=180)
        briefs = list((tmp_path / "briefs").glob("*.md"))
        return {
            "rc": p.returncode,
            "brief": briefs[0].read_text() if briefs else "",
            "log": (tmp_path / "log" / "daily-brief.log").read_text(),
            "prompt": (tmp_path / "prompt.txt").read_text(),
        }
    return run


def test_minimal_tier_writes_the_brief_quietly(runner):
    r = runner()
    assert r["rc"] == 0
    assert "Nothing today." in r["brief"]
    assert "No newsletters configured — General news omitted." in r["brief"]
    assert "General news" not in r["prompt"].split("OUTPUT ONLY")[1].split("Noise")[0]
    assert "email: not configured" in r["log"]
    assert "WARN" not in r["log"]


def test_email_tier_attempts_delivery_without_a_newsletter_mailbox(runner):
    r = runner(SMTP_USER="out@example.org", SMTP_PASSWORD="pw",
               SMTP_HOST="127.0.0.1", SMTP_PORT=str(_closed_port()))
    assert r["rc"] == 0
    assert "No newsletters configured" in r["brief"]
    assert "WARN: brief email failed" in r["log"]
    assert "email: not configured" not in r["log"]


def test_newsletter_tier_attempts_the_mailbox(runner):
    r = runner(IMAP_USER="in@gmail.com", IMAP_APP_PASSWORD=GMAIL_PW,
               IMAP_HOST="127.0.0.1", IMAP_PORT=str(_closed_port()),
               NEWSLETTER_SENDERS="example-paper.com", BRIEF_MAIL="0")
    assert r["rc"] == 0
    assert "could not reach 127.0.0.1" in r["log"]
    assert "No newsletters read this run" in r["brief"]


def test_newsletter_tier_offers_general_news_when_mail_arrived(runner):
    r = runner(STUB_NEWSLETTERS="1", BRIEF_MAIL="0")
    assert r["rc"] == 0
    assert "### General news" in r["prompt"]
    assert "=== NEWSLETTERS" in r["prompt"]
    assert "General news read from 1 newsletter(s)" in r["brief"]
