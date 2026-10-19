#!/usr/bin/env python3
"""Email a brief as rendered HTML.

TRANSPORT-AGNOSTIC ON PURPOSE. It takes a markdown file and sends it, so the
same script works from launchd on the Mac and from a CI runner if generation
ever moves. Nothing in here knows where it is running.

WHY EMAIL AT ALL, given ~/briefs already renders on GitHub and the mobile app
notifies on push: push is a notification you have to go and open, and this
lands in the place the source newsletters already land. Same mental slot as
Handelsblatt and WiWo — morning reading, not a repo to visit.

WHY HTML, not plain text: a brief is ~12KB with 22 markdown table rows and
URLs up to 410 characters. As plain text that is pipe soup with a wall of
base64 through the middle, which is worse than the GitHub rendering it would
be competing with.

Credentials: its own settings, with the newsletter mailbox as a fallback.
Email delivery and newsletter reading are separate features, so either can be
configured without the other. Each key is read from ~/.config/vertigo/mail.env,
then ~/.config/vertigo/newsletter.env, then the environment.

    SMTP_USER / SMTP_PASSWORD   the account that sends. If neither is set, the
                                newsletter mailbox's IMAP_USER /
                                IMAP_APP_PASSWORD are used instead, which is
                                how the job was first set up: one Gmail
                                account, one app password, SMTP instead of IMAP.
    SMTP_HOST / SMTP_PORT       optional, default smtp.gmail.com:587. Port 465
                                connects over TLS; any other port uses STARTTLS.
    SMTP_FROM                   optional, defaults to the sending account
    BRIEF_MAIL_TO               optional, defaults to the sending account

Exit status: 0 sent, 1 failed, 2 not configured (nothing to send with). The
daily job treats 2 as "email is off", not as a failure.

Usage:  send_brief.py PATH.md [--to addr] [--dry-run]
"""
import argparse, os, re, smtplib, ssl, sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from market_brief_newsletters import load_env, DEFAULT_ENV, IMAP_TIMEOUT

MAIL_ENV = os.path.expanduser("~/.config/vertigo/mail.env")
NOT_CONFIGURED = 2

# Restrained, high-contrast, and readable on a phone. No web fonts, no images,
# no tracking — mail clients strip half of it anyway and the rest is noise.
CSS = """
body{font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
color:#1a1a1a;max-width:46em;margin:0 auto;padding:18px}
h1{font-size:1.5em;margin:.2em 0 .1em} h2{font-size:1.2em;margin:1.4em 0 .3em;
padding-top:.5em;border-top:1px solid #e5e5e5} h3{font-size:1.05em;margin:1.2em 0 .3em}
table{border-collapse:collapse;width:100%;margin:.6em 0;font-size:.9em}
th,td{border:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f6f6f6;font-weight:600}
code{background:#f2f2f2;padding:1px 4px;border-radius:3px;font-size:.88em}
a{color:#0b5cad} em{color:#555}
blockquote{border-left:3px solid #d0d0d0;margin:.6em 0;padding:.2em 0 .2em 1em;color:#444}
"""


def render(md_text):
    import markdown
    return markdown.markdown(
        md_text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])


def subject_for(md_text, stamp):
    """Lead with the brief's own one-liner. A subject reading 'Brief —
    Thursday' tells you nothing you did not know from the timestamp; the
    act-on line is the only part that decides whether to open it now."""
    body = re.sub(r"(?s)^.*?##\s*`?AI`?\s*Judgement", "", md_text, count=1)
    body = re.sub(r"(?s)\*.*?\*", "", body, count=1)          # provenance note
    for line in body.split("\n"):
        line = line.strip()
        if line and not line.startswith(("#", "*", ">", "|", "-")):
            line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
            line = re.sub(r"[*`_]", "", line)
            return f"Brief {stamp} — {line[:110]}"
    return f"Brief {stamp}"


def load_files(paths):
    """Merge KEY=VALUE files, earlier paths winning. A missing file is not an
    error, since every setting can arrive from the environment instead. A file
    that exists and cannot be used is, and the reason is returned."""
    merged, errors = {}, []
    for path in paths:
        if not os.path.exists(path):
            continue
        env, err = load_env(path)
        if err:
            errors.append(err)
        for k, v in env.items():
            merged.setdefault(k, v)
    return merged, "; ".join(errors) or None


def mail_config(env, environ=None):
    """Resolve the sending settings. File first, then the environment, then the
    default, for every key; see the same helper in market_brief_newsletters.py
    for why each setting needs it.

    The account is resolved as a PAIR. A SMTP_USER with the IMAP password, or
    the reverse, would log one account in with another account's secret, so
    the IMAP fallback applies only when neither SMTP key is set."""
    environ = os.environ if environ is None else environ

    def cfg(key, default=None):
        v = env.get(key) or environ.get(key)
        return v if v not in (None, "") else default

    if cfg("SMTP_USER") or cfg("SMTP_PASSWORD"):
        user, pw, source = cfg("SMTP_USER"), cfg("SMTP_PASSWORD"), "SMTP"
    else:
        user, pw, source = cfg("IMAP_USER"), cfg("IMAP_APP_PASSWORD"), "IMAP"
    host = cfg("SMTP_HOST", "smtp.gmail.com")
    try:
        port = int(cfg("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    return {
        "user": user,
        "pw": (pw or "").replace(" ", "") if host == "smtp.gmail.com" else (pw or ""),
        "source": source,
        "host": host,
        "port": port,
        "sender": cfg("SMTP_FROM") or user,
        "to": cfg("BRIEF_MAIL_TO") or user,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--to")
    ap.add_argument("--env", help="read settings from this file only "
                    f"(default: {MAIL_ENV}, then {DEFAULT_ENV})")
    ap.add_argument("--dry-run", action="store_true",
                    help="render and print what would be sent, connect to nothing")
    a = ap.parse_args()

    if not os.path.exists(a.path):
        print(f"send_brief: no such file {a.path}", file=sys.stderr)
        return 1
    md_text = open(a.path).read()
    stamp = os.path.splitext(os.path.basename(a.path))[0]

    env, err = load_files([a.env] if a.env else [MAIL_ENV, DEFAULT_ENV])
    c = mail_config(env)
    user, pw, host, port = c["user"], c["pw"], c["host"], c["port"]
    to = a.to or c["to"]

    msg = EmailMessage()
    msg["Subject"] = subject_for(md_text, stamp)
    msg["From"] = c["sender"] or "vertigo@localhost"
    msg["To"] = to or "unset"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="vertigo.local")
    # So a filter can find these without matching on subject text, and so the
    # newsletter fetcher can never mistake one for a publisher's mail.
    msg["X-Vertigo-Brief"] = stamp
    msg.set_content(md_text)                      # plain-text fallback
    msg.add_alternative(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<style>{CSS}</style></head><body>{render(md_text)}</body></html>",
        subtype="html")

    if a.dry_run:
        print(f"to      : {to}\nsubject : {msg['Subject']}\n"
              f"markdown: {len(md_text)} bytes\nhtml    : {len(render(md_text))} bytes")
        return 0

    # Same as the fetcher: the file is one source, the environment another, and
    # in CI only the second exists. Nothing configured at all is not a failure:
    # email is optional, and the brief is already on disk.
    if not user and not pw and not err:
        print("send_brief: email not configured (no SMTP_USER/SMTP_PASSWORD "
              "or IMAP_USER/IMAP_APP_PASSWORD)", file=sys.stderr)
        return NOT_CONFIGURED
    if not user or not pw:
        pair = "SMTP_USER and SMTP_PASSWORD" if c["source"] == "SMTP" \
            else "IMAP_USER and IMAP_APP_PASSWORD"
        print(f"send_brief: {err or pair + ' must both be set'}", file=sys.stderr)
        return 1
    if host == "smtp.gmail.com" and not re.fullmatch(r"[a-z]{16}", pw):
        # Same guard as the fetcher. Gmail answers a malformed password the
        # same way it answers a rate limit, and a day was lost to that once.
        # Gmail only: other providers' passwords have no fixed shape.
        print(f"send_brief: {c['source']} password is not 16 lowercase letters, "
              "the shape of a Gmail app password; not attempting a login",
              file=sys.stderr)
        return 1

    try:
        if port == 465:
            conn = smtplib.SMTP_SSL(host, port, timeout=IMAP_TIMEOUT,
                                    context=ssl.create_default_context())
        else:
            conn = smtplib.SMTP(host, port, timeout=IMAP_TIMEOUT)
        with conn as s:
            if port != 465:
                s.starttls(context=ssl.create_default_context())
            s.login(user, pw)
            s.send_message(msg)
    except Exception as e:
        # Never echo the password; server replies can quote the command.
        print(f"send_brief: {type(e).__name__}: "
              f"{str(e).replace(pw, '<redacted>') if pw else e}", file=sys.stderr)
        return 1
    print(f"sent {stamp} to {to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
