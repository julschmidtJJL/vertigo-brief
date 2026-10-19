---
name: vertigo-setup
description: Set up the vertigo brief for a new user — install it, interview them for their holdings and their own written theses, write the files in ~/.vertigo, and produce a first brief locally. Use when someone asks to set up, install or configure the vertigo brief.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
---

# Set up the vertigo brief

You are setting up a daily brief that holds the news against investment theses
the user wrote **before** the news arrived. That last part is the whole
product. Read the rules below before you start, then work through the steps in
order. Stop and ask whenever a step needs a decision only the user can make.

## Rules

1. **Never write a thesis or a falsifier yourself.** Not a draft, not a
   suggestion, not "something like…". You ask, the user answers, you
   transcribe their words. You may ask follow-up questions to make a
   falsifier checkable ("which number, and above what level?"), but the
   condition and the reason are theirs. Read every thesis back to them and
   write it only when they confirm it. A holding the user has no thesis for
   gets no entry: the brief then reports it as "no thesis — nothing to
   falsify", which is the honest answer. A thesis written by a model after the
   fact defeats the brief.
2. **Touch only this repository and `~/.vertigo`.** Do not install anything
   globally, do not edit any CLAUDE.md, shell profile or agent config, and do
   not create files elsewhere except where a step below says so and the user
   agrees. Python packages go into a `venv/` inside the repository.
3. **Never handle a secret.** API keys and passwords are typed by the user
   into a file or a prompt you do not read. Give them the command; do not ask
   them to paste a key into the chat, and never print a credential file.
4. **Nothing personal goes into git.** `.gitignore` already excludes the
   personal files, but `~/.vertigo` is outside the repository anyway. Do not
   copy personal files into the repository, and do not commit or push
   anything without asking.
5. **No advice.** You are recording what the user holds and believes. Do not
   comment on whether a holding or a thesis is a good idea.

Tell the user up front what the brief needs, so they can stop early if it does
not fit: a book valued in **EUR**, holdings that **Yahoo Finance** prices
(foreign listings in USD, CAD or HKD are converted; other currencies are not),
and an API key from **OpenAI or Anthropic** (either one) for the judgement half. Newsletters and email are
optional.

## 1. Install

If you are not already inside a clone of this repository, ask the user where
to put it (default: the current directory) and clone it there.

```bash
cd vertigo-brief
python3 -m venv venv
venv/bin/pip install -r requirements.txt pytest
```

`scripts/run_daily_brief.sh` uses `venv/bin/python3` when it exists.

## 2. Start from the example

```bash
ls ~/.vertigo 2>/dev/null && echo "EXISTS"
```

If `~/.vertigo` already exists, stop and ask: it may be a previous setup. Do
not overwrite it. Otherwise:

```bash
cp -r examples/vertigo-home ~/.vertigo
```

The example is an invented portfolio. Every row in it gets replaced in the
next steps; keep the comment headers, which document each file's columns.

## 3. Holdings: `positions.csv` and `cash.csv`

Ask for each position, one at a time or as a pasted broker export:

- the **Yahoo Finance ticker** (`SIE.DE`, `CAT`, `BN.PA`) and the **ISIN**. If
  they give only one, look up the other and confirm it with them.
- **shares**
- **average cost per share in EUR**, as their broker reports it
- the **trading currency** of that ticker: EUR, USD, CAD or HKD
- **sleeve**: `stock` for an individual pick, `etf` for an index fund
- optionally the broker and a display name

Write the rows under the existing header in `~/.vertigo/positions.csv`, then
check that every ticker resolves:

```bash
venv/bin/python3 - <<'EOF'
import csv, os, yfinance as yf
path = os.path.expanduser("~/.vertigo/positions.csv")
for r in csv.DictReader(l for l in open(path) if not l.startswith("#")):
    h = yf.Ticker(r["ticker"]).history(period="5d")
    print(r["ticker"], "ok" if len(h) else "NO PRICE — check the ticker")
EOF
```

Ask for uninvested cash per broker and write `~/.vertigo/cash.csv`. Zero is a
valid answer.

## 4. Theses: `theses.yaml`

Only for the `stock` sleeve. For each pick, ask, and write down **their
words**:

1. **Why do you hold it?** That is the `thesis`. A sentence or two. If they
   have never written it down, they can say it now; it is still theirs. If
   they would rather not, skip the holding (rule 1).
2. **What would prove you wrong?** Ask for **two falsifiers**. For each one:
   - the `statement`, in their words, as "If …, then …"
   - `check: auto` if it is a number the market reports daily (a price, a
     valuation ratio, a commodity price, a margin) and they can give a
     threshold. `check: manual` if it needs reading a filing, an earnings
     release or the news. When unsure, it is `manual`.
   - for `auto`, the exact condition: which figure, which threshold, and any
     baseline date and values the condition is measured from
   - a short kebab-case `id` you propose and they accept, prefixed by a few
     letters of the ticker (`sie-below-entry`)
3. **When did you write this?** Use today's date for `written` unless they
   have an older written version, and ask for `horizon` and a `conviction`
   from 1 to 5.

Leave `macro_sensitivity` out unless the user names one themselves; it is
optional, and an `inferred` entry is filtered out of the brief anyway.

Follow the schema of the example file exactly (the header comment explains
each field), replace all its entries, and show the user the finished file
before moving on.

For the `etf` sleeve, set `~/.vertigo/etf-macro.yaml` to `etfs: {}` unless the
user wants to name macro drivers for a fund. The same rule applies: their
drivers, their words.

## 5. News queries: `news.yaml`

For each `stock` holding, write an entry keyed by ISIN: a Google News `query`
(the company name, quoted if it is ambiguous; a list for several names, never
`OR`), `lang` (`en` or `de`), and lowercase `aliases` (the company's names and
major brands). Show the user the list. A holding with no entry is searched
under its name from `positions.csv`.

## 6. Evaluators: `evaluators.py`

Only for falsifiers marked `auto`. Rewrite `~/.vertigo/evaluators.py` following
the contract in the docstring of the example: one function per `auto`
falsifier, registered in `EVALUATORS` under the falsifier's `id`, and a
direction in `DISTANCE_KIND`. Use only the `data._info`, `data._px` and
`data._q_avg` helpers. The function encodes the condition the user stated, no
more and no less. Show each function to the user with the statement it
encodes and ask them to confirm it matches.

Then check coverage. This must pass before you continue:

```bash
VERTIGO_HOME=~/.vertigo venv/bin/python3 -m pytest -q tests
```

`test_every_auto_falsifier_has_an_evaluator` and the distance tests are the
ones that read the user's files. Fix any failure by correcting
`evaluators.py`, or by asking the user whether a falsifier should be `manual`.
Never change a falsifier's meaning to make a test pass.

Then run the monitor once against live data and show the user the table:

```bash
venv/bin/python3 scripts/falsifier_monitor.py
```

## 7. API key (OpenAI or Anthropic)

Ask the user which key they have: OpenAI or Anthropic (Claude). One is
enough. Then ask them to create the key file themselves, in their own terminal.
For OpenAI:

```bash
mkdir -p ~/.config/vertigo && touch ~/.config/vertigo/openai.env && chmod 600 ~/.config/vertigo/openai.env
```

and then add one line `OPENAI_API_KEY=…` with an editor. For Anthropic:

```bash
mkdir -p ~/.config/vertigo && touch ~/.config/vertigo/anthropic.env && chmod 600 ~/.config/vertigo/anthropic.env
```

and then add one line `ANTHROPIC_API_KEY=…`. The file must be `chmod 600` or
it is refused. Alternatively the variable can be set in the environment of the
shell that runs the brief. Wait until they say it is done.

The provider follows the key that is present. If both are, OpenAI is used
unless `BRIEF_AI_PROVIDER=anthropic` is set. The models default to `gpt-5.5`
and `claude-opus-5-5` (`BRIEF_MODEL` overrides either); the call costs API
credit on every run. The log line `brief_ai: <provider> <model> …` in
`daily-brief.log` shows which one ran.

## 8. First run, no email

Tell the user the brief is written to `~/briefs/` and its log to
`~/.local/state/vertigo/` unless they want `BRIEFS_DIR` and `VERTIGO_LOG_DIR`
elsewhere. Then:

```bash
BRIEF_MAIL=0 bash scripts/run_daily_brief.sh
```

It takes a few minutes. Open today's file in the briefs directory and walk the
user through it: the `ƒ` blocks (positions, falsifier monitor, regime strip)
are computed by Python; the `AI` block is the model's reading of the news
against their theses. The footer says "No newsletters configured — General
news omitted." That is expected at this tier. If anything failed, read
`daily-brief.log` before guessing.

This is a working setup. Everything below is optional; ask which, if any, the
user wants.

## 9. Optional: email delivery

The brief can be emailed as HTML from any SMTP account. The user creates
`~/.config/vertigo/mail.env` (chmod 600) themselves, with:

```
SMTP_USER=the-sending-address@example.com
SMTP_PASSWORD=…
SMTP_HOST=smtp.example.com      # default smtp.gmail.com
SMTP_PORT=587                   # 465 for TLS, otherwise STARTTLS
BRIEF_MAIL_TO=where-to-read-it@example.com
```

For Gmail the password is an app password (16 lowercase letters, from
https://myaccount.google.com/apppasswords, needs 2-Step Verification). Test it
without sending, then for real:

```bash
venv/bin/python3 scripts/send_brief.py ~/briefs/$(date +%F).md --dry-run
venv/bin/python3 scripts/send_brief.py ~/briefs/$(date +%F).md
```

## 10. Optional: newsletters

If the user receives morning newsletters by email, the brief can read them and
write a General news section. They need an IMAP mailbox the newsletters arrive
in (a Gmail app password works) in `~/.config/vertigo/newsletter.env`, created
by them, chmod 600; the keys are in the docstring of
`scripts/market_brief_newsletters.py`. Then find out who is sending:

```bash
venv/bin/python3 scripts/market_brief_newsletters.py --hours 72 --discover
```

Ask which senders count, and have the user add
`NEWSLETTER_SENDERS=domain-one.com,domain-two.de` to that file. Nothing is
read without that line. If `mail.env` does not exist, the brief is emailed
from this mailbox instead.

## 11. Optional: run it every weekday on GitHub Actions

Explain the shape before doing anything, and confirm each step that creates
something on GitHub:

- The workflow must run from a **private** copy of this repository: the run
  log is printed in the Actions output, and Actions logs on a public
  repository are public. A fork of a public repository cannot be made
  private, so create a new private repository and push this clone to it. The
  workflow refuses to run from a public repository.
- Two more **private** repositories: one holding the contents of `~/.vertigo`
  (the state repo) and one that receives the briefs. The workflow refuses
  public ones.
- In the private code copy, repository variables `VERTIGO_STATE_REPO` and
  `BRIEFS_REPO` (`owner/name`), optionally `NEWSLETTER_SENDERS`, `SMTP_HOST`
  and `SMTP_PORT`.
- Secrets: `VERTIGO_PAT` (a fine-grained token with contents read on the
  state repo and read/write on the briefs repo), `OPENAI_API_KEY` or
  `ANTHROPIC_API_KEY` (with both, the variable `BRIEF_AI_PROVIDER=anthropic`
  picks Claude), and
  optionally `SMTP_USER`, `SMTP_PASSWORD`, `BRIEF_MAIL_TO`, `IMAP_USER`,
  `IMAP_APP_PASSWORD`, `IMAP_MAILBOX`. The user sets each one with
  `gh secret set NAME`, which prompts for the value, or in the repository
  settings. You do not see the values.

To make `~/.vertigo` the state repo: `git init` in it, check that `git status`
lists no credential file, commit, and push to the private state repository.
Ask before each push. Then trigger one run with
`gh workflow run daily-brief.yml` and read its log with the user.

## Done

Summarise for the user what is set up, which tier they are on, where the
brief and the log are written, and that the brief is only as good as the
theses: when a view changes, they edit `~/.vertigo/theses.yaml` themselves,
with a new `written` date.
