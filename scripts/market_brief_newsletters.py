#!/usr/bin/env python3
"""Fetch the morning newsletters over IMAP and hand them to the brief as text.

The point is the 30-60 minutes a morning spent reading these by hand. The model
reads them instead, writes the general-news section in its own words, and joins
whatever it finds to the theses. Nothing here summarises or scores; it fetches.

WHY IMAP AND AN APP PASSWORD, not the Gmail API and OAuth: the daily brief runs
unattended on a CI runner, with no browser, no MCP and no interactive session. An
OAuth flow needs a refresh token that expires on somebody else's schedule, which
is the failure a scheduled job cannot recover from. An app password is
a static credential that either works or does not, and it fails loudly.

CREDENTIALS — never in git, never in this file, never logged:

    ~/.config/vertigo/newsletter.env      chmod 600

    IMAP_USER=the-address-the-newsletters-go-to@gmail.com
    IMAP_APP_PASSWORD=<the 16 characters Google gives you>
    # optional (defaults shown):
    IMAP_HOST=imap.gmail.com
    IMAP_PORT=993
    IMAP_MAILBOX=INBOX

Generate the app password at https://myaccount.google.com/apppasswords (needs
2-Step Verification on). It is EXACTLY 16 LOWERCASE LETTERS, shown in four
groups of four; the spaces are decorative and are stripped here. Anything else
in that field is not the password, and the shape is checked before any login is
attempted. Revoking it there kills this and nothing else.

In CI the same variables arrive as environment secrets instead of the file.

Usage:
    python3 newsletters.py --hours 24
    python3 newsletters.py --hours 72 --discover   # who is actually sending?
"""
import argparse, datetime, email, email.utils, html, imaplib, json, os, re, sys
from email.header import decode_header, make_header

# Deliberately NOT in ~/.vertigo. That directory became a git repo on
# 2026-08-18 so the conviction journal gets history and off-machine backup, and
# a live credential must not sit inside a directory that gets committed and
# pushed. .gitignore would cover it; being absent cannot be bypassed.
DEFAULT_ENV = os.path.expanduser("~/.config/vertigo/newsletter.env")
LEGACY_ENV = os.path.expanduser("~/.vertigo/newsletter.env")

# Domains whose mail counts as a newsletter. Anything else in the mailbox is
# ignored, so this account can carry ordinary mail without it reaching a brief.
# Run --discover to see what is actually arriving and extend the list.
# NEWSLETTER_SENDERS (comma-separated domains) replaces the default.
SENDERS = [d.strip() for d in os.environ.get(
    "NEWSLETTER_SENDERS", "handelsblatt.com,wiwo.de,wirtschaftswoche.de").split(",")
    if d.strip()]

# Per-newsletter character cap. A Handelsblatt morning briefing runs long, and
# several of them plus 37KB of holdings JSON is a lot of prompt for a job on a
# 600s watchdog. Truncation is announced in the payload so the model never
# treats a cut-off newsletter as a complete one.
# Raised 2026-08-19 once real sizes were known: the Handelsblatt daily briefing
# runs past 14k and was being cut mid-issue, which loses the back half of the
# one newsletter this feature exists for. A day's mail measured ~36k total
# against a 272k context, so the old caps were protecting nothing.
IMAP_TIMEOUT = 45      # seconds per socket operation
MAX_CHARS = 24000
MAX_TOTAL = 120000


def load_env(path):
    """KEY=VALUE lines. Refuses a world- or group-readable credential file."""
    out = {}
    if not os.path.exists(path):
        return out, f"no credential file at {path}"
    try:
        mode = os.stat(path).st_mode & 0o077
        if mode:
            return {}, (f"{path} is readable by others (chmod 600 it) — "
                        "refusing to load a credential from it")
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:
        return {}, f"could not read {path}: {type(e).__name__}"
    return out, None


def hdr(msg, name):
    try:
        return str(make_header(decode_header(msg.get(name, "") or "")))
    except Exception:
        return msg.get(name, "") or ""


def body_text(msg):
    """Prefer text/plain. Fall back to stripped HTML — most newsletters send
    both, but some send HTML only and that half must not be silently empty."""
    plain, rich = [], []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        if "attachment" in (part.get("Content-Disposition") or ""):
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            txt = raw.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        (plain if ctype == "text/plain" else rich).append(txt)

    text = "\n".join(plain).strip()
    if not text and rich:
        h = "\n".join(rich)
        h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
        h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", h)
        h = re.sub(r"<[^>]+>", " ", h)
        text = html.unescape(h)

    # Tracking-pixel URLs and click-wrappers are most of a newsletter's bytes
    # and none of its meaning. Dropping them buys back prompt budget for prose.
    text = re.sub(r"https?://\S{80,}", "[link]", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--env", default=DEFAULT_ENV)
    ap.add_argument("--discover", action="store_true",
                    help="list every sender in the window and exit, so the "
                         "SENDERS allowlist can be set from what actually arrives")
    # Diagnostics. A newsletter that is not in INBOX is the normal failure —
    # a Gmail filter that skips the inbox, or spam — and hunting it by editing
    # the credential file means copying a password around. These two flags
    # exist so that never has to happen.
    ap.add_argument("--mailbox", help="override IMAP_MAILBOX for this run")
    ap.add_argument("--list-mailboxes", action="store_true",
                    help="print every folder on the server and exit")
    args = ap.parse_args()

    def out(payload, code=0):
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
        return code

    # Refuse to read a credential from inside the backed-up repo, and say so
    # loudly rather than quietly working — a file recreated at the old path
    # would otherwise sit one `git add -f` away from being pushed to GitHub.
    if args.env == DEFAULT_ENV and os.path.exists(LEGACY_ENV):
        return out({"ok": False, "newsletters": [],
                    "error": f"a credential file still exists at {LEGACY_ENV}, "
                             "which is inside the git-backed ~/.vertigo repo. "
                             f"Move it to {DEFAULT_ENV} and delete the old one.",
                    "hint": "refusing to run while a secret sits in a repo directory"})

    env, err = load_env(args.env)

    def cfg(key, default=None):
        """File first, then the environment, then the default. EVERY setting
        needs this, not just the credentials: on 2026-08-21 the first CI run
        read IMAP_MAILBOX from the file only, found no file, and silently fell
        back to INBOX — where the newsletters are not, because a Gmail filter
        archives them. The brief was built from 1 newsletter instead of 9 and
        nothing reported a problem."""
        v = env.get(key) or os.environ.get(key)
        return v if v not in (None, "") else default

    user = cfg("IMAP_USER")
    pw = (cfg("IMAP_APP_PASSWORD") or "")
    pw = pw.replace(" ", "")          # Google displays it in groups of four
    host = cfg("IMAP_HOST", "imap.gmail.com")
    # Configurable so that forwarding the newsletters to any other IMAP mailbox
    # (iCloud, Fastmail, mailbox.org) is a config change and not a code change.
    # 993 is IMAPS everywhere; the override exists for the provider that is not.
    try:
        port = int(cfg("IMAP_PORT", "993"))
    except ValueError:
        port = 993
    box = args.mailbox or cfg("IMAP_MAILBOX", "INBOX")

    # Fail SOFT and explain. A missing credential must degrade the general-news
    # section, never take down a brief whose ƒ half is fine.
    # A missing file is only an error if the environment did not supply the
    # credential instead. In CI there is no file and never will be — the values
    # arrive as secrets. `err` still explains WHY the file was unusable when
    # nothing else filled the gap.
    if not user or not pw:
        return out({"ok": False, "newsletters": [],
                    "error": err or "IMAP_USER or IMAP_APP_PASSWORD not set",
                    "hint": f"see the docstring in {__file__}"})

    # SHAPE CHECK BEFORE ANY LOGIN. A Gmail app password is exactly 16
    # lowercase letters. Checking that locally costs nothing and turns a
    # malformed credential into an answer instead of a mystery.
    #
    # Why this exists: on 2026-08-19 the stored value was 44 characters with
    # mixed case, digits, a slash and two dots — some other key pasted into the
    # field. Gmail answers a malformed password with the same
    # `[AUTHENTICATIONFAILED] Invalid credentials` it uses for a rate limit, so
    # a day was spent waiting out a throttle that was never there. Never send a
    # value that cannot possibly be right, and never let it look like a throttle.
    if not re.fullmatch(r"[a-z]{16}", pw):
        shape = f"{len(pw)} chars"
        if not pw.islower():
            shape += ", not all lowercase"
        if any(c.isdigit() for c in pw):
            shape += ", contains digits"
        if not pw.isalpha():
            shape += ", contains non-letters"
        return out({"ok": False, "newsletters": [],
                    "error": f"IMAP_APP_PASSWORD is not shaped like a Gmail app "
                             f"password ({shape}); expected exactly 16 lowercase "
                             f"letters. Not attempting a login.",
                    "likely_cause": "the wrong value was pasted into the field — "
                                    "this is NOT a rate limit and waiting will "
                                    "not fix it. Google shows app passwords in "
                                    "four groups of four; spaces are stripped "
                                    "here, anything else is not the password."})

    cutoff = datetime.datetime.now(datetime.timezone.utc) - \
        datetime.timedelta(hours=args.hours)
    # IMAP SINCE has date granularity only, so widen by a day and filter on the
    # real timestamp below. Narrowing here would silently drop this morning's
    # send in some timezones.
    since = (cutoff - datetime.timedelta(days=1)).strftime("%d-%b-%Y")

    # SOCKET TIMEOUT, not optional. On 2026-08-20 the scheduled job hung for
    # 2h50m in a bare read(): the TCP connection to Gmail was ESTABLISHED and
    # the server simply stopped sending. imaplib defaults to a blocking socket
    # with no timeout, so the whole brief waited forever and no ƒ half was ever
    # written. A stalled peer must cost one step, never the run.
    try:
        M = imaplib.IMAP4_SSL(host, port, timeout=IMAP_TIMEOUT)
    except Exception as e:
        return out({"ok": False, "newsletters": [],
                    "error": f"could not reach {host}:{port}: {type(e).__name__}"})
    try:
        try:
            M.login(user, pw)
        except imaplib.IMAP4.error as e:
            # Surface the server's own words, with the password scrubbed in case
            # the reply quotes the command. Hiding it entirely was a mistake:
            # "wrong password" and "temporarily blocked, try later" are the same
            # message to the reader, and only one of them is worth acting on.
            # Gmail throttles repeated logins — several runs in a few minutes is
            # enough — and answers a VALID app password with a refusal.
            said = str(e).replace(pw, "<redacted>") if pw else str(e)
            rate = any(w in said.lower() for w in
                       ("too many", "rate", "limit", "try again", "temporar",
                        "unavailable", "webalert", "blocked"))
            return out({"ok": False, "newsletters": [],
                        "error": ("IMAP login rejected: " + said),
                        "likely_cause": (
                            "Gmail is throttling logins, not a bad password — "
                            "wait ~15 minutes and retry before changing anything"
                            if rate else
                            "app password wrong or revoked, 2-Step Verification "
                            "off, or Gmail temporarily blocked this address after "
                            "repeated logins — wait 15 minutes and retry before "
                            "regenerating the password")})
        if args.list_mailboxes:
            typ, boxes = M.list()
            names = []
            for b in (boxes or []):
                s = b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)
                m = re.search(r'"([^"]*)"$', s) or re.search(r'(\S+)$', s)
                names.append(m.group(1) if m else s)
            return out({"ok": True, "mailboxes": sorted(names)})

        # imaplib does NOT quote the mailbox name, so anything with a space —
        # "[Gmail]/All Mail", or any label the user typed with a space in it —
        # is parsed as two arguments and the server answers BAD. Quote it here.
        sel = box if box.startswith('"') else (f'"{box}"' if " " in box else box)
        try:
            typ, _ = M.select(sel, readonly=True)  # readonly: never mark as read
        except imaplib.IMAP4.error as e:
            return out({"ok": False, "newsletters": [],
                        "error": f"could not open mailbox {box!r}: {e} — "
                                 "run --list-mailboxes to see what exists"})
        if typ != "OK":
            return out({"ok": False, "newsletters": [],
                        "error": f"mailbox {box!r} does not exist — "
                                 "run --list-mailboxes to see what does"})
        typ, data = M.search(None, f'(SINCE {since})')
        ids = data[0].split() if typ == "OK" and data and data[0] else []

        seen_senders, items, total = {}, [], 0
        for num in reversed(ids):                     # newest first
            typ, raw = M.fetch(num, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            frm = hdr(msg, "From")
            addr = email.utils.parseaddr(frm)[1].lower()
            try:
                when = email.utils.parsedate_to_datetime(msg.get("Date"))
                when = when.astimezone(datetime.timezone.utc) if when.tzinfo \
                    else when.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            if when < cutoff:
                continue

            if args.discover:
                seen_senders.setdefault(addr, {"from": frm, "count": 0,
                                               "last_subject": hdr(msg, "Subject")})
                seen_senders[addr]["count"] += 1
                continue

            # Never ingest our own output. Today the allowlist is publisher
            # domains only so this cannot happen, but a brief that fed itself
            # would be a slow, confusing loop and the guard costs one line.
            # send_brief.py stamps every brief with this header.
            if msg.get("X-Vertigo-Brief"):
                continue
            if not any(d in addr for d in SENDERS):
                continue
            if total >= MAX_TOTAL:
                continue

            text = body_text(msg)
            truncated = len(text) > MAX_CHARS
            text = text[:MAX_CHARS]
            total += len(text)
            items.append({
                "source": addr.split("@")[-1],
                "from": frm,
                "subject": hdr(msg, "Subject"),
                "at": when.isoformat(),
                "truncated": truncated,
                "text": text,
            })
    finally:
        try:
            M.close()
        except Exception:
            pass
        M.logout()

    if args.discover:
        return out({"ok": True, "window_hours": args.hours,
                    "senders": sorted(seen_senders.values(),
                                      key=lambda s: -s["count"])})

    items.sort(key=lambda i: i["at"], reverse=True)
    return out({
        "ok": True,
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "window_hours": args.hours,
        "mailbox": f"{user} / {box}",
        "allowlist": SENDERS,
        "count": len(items),
        "newsletters": items,
    })


if __name__ == "__main__":
    sys.exit(main())
