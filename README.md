# vertigo brief

A morning brief that holds the news against the investment thesis you wrote
*before* the news arrived.

Every weekday it reads your positions and your written theses, pulls the news
for each holding, checks every machine-checkable falsifier, and emails you one
page. The page has two halves, and each block is labelled with the half it
came from:

- **`ƒ` (deterministic):** a positions table, a falsifier monitor and a macro
  regime strip. Python computes every number from live market data. No model
  is involved.
- **`AI` (judgement):** a model reads the day's headlines and newsletters
  against your thesis and falsifier text. It reports which events collide with
  what you wrote and cites each one. It never produces a number and never
  gives buy or sell advice.

This is a personal tool, published as-is. It assumes a single user, a
EUR-based book and Yahoo Finance data.

## Quick start

Paste this into Claude Code. It works the same in Codex or Cursor.

```
Set up the vertigo brief for me. Clone https://github.com/julschmidtJJL/vertigo-brief,
then read skills/vertigo-setup/SKILL.md in it and follow it step by step.
Interview me for my holdings and for my own thesis and two falsifiers per
stock; write down my words and never write a thesis yourself. Stay inside the
repository and ~/.vertigo, and do not change any global agent configuration.
```

The agent installs the dependencies into a `venv/` in the repository, copies
the example portfolio to `~/.vertigo`, asks you for your positions and for
the thesis behind each pick, writes the falsifier checks you mark `auto`, runs
the test suite to confirm every one of them has a check, and produces a first
brief on disk. Email, newsletters and the scheduled run come after that, and
only if you want them. [`skills/vertigo-setup/SKILL.md`](skills/vertigo-setup/SKILL.md)
is the whole procedure, if you would rather do it by hand.

You need an OpenAI API key. You do not need any newsletters.

## How it is laid out

```
scripts/
  run_daily_brief.sh        the daily job: gather → falsifiers → regime → AI → email
  market_brief_gather.py    positions, prices, news selection (ƒ)
  falsifier_monitor.py      evaluates `check: auto` falsifiers (ƒ)
  regime_strip.py           VIX, credit spreads, curve, rates, drawdown… (ƒ)
  market_brief_newsletters.py  reads newsletters over IMAP for General news (optional)
  brief_ai.py               the AI half (OpenAI API)
  send_brief.py             renders and emails the result (optional)
  check_brief_landed.sh     optional local watchdog: re-dispatches a missing brief
skills/market-brief/        the same brief as an interactive Claude Code skill
skills/vertigo-setup/       the setup procedure the quick-start prompt follows
examples/vertigo-home/      an invented example portfolio — start here
tests/                      offline test suite
```

## Your data lives outside the repo

Everything personal sits in `~/.vertigo/` (or wherever `VERTIGO_HOME` points),
never in this repository:

| file | what it is |
|---|---|
| `positions.csv` | holdings: ISIN, ticker, shares, average cost, sleeve (`stock`/`etf`) |
| `cash.csv` | uninvested cash, for the total |
| `theses.yaml` | one thesis per stock pick, with two falsifiers each, `auto` or `manual` |
| `evaluators.py` | one function per `auto` falsifier, registered by id |
| `news.yaml` | per-holding news query, language and aliases |
| `etf-macro.yaml` | macro drivers for the index-fund sleeve |

Start from the example:

```bash
cp -r examples/vertigo-home ~/.vertigo
```

Then replace every row with your own. The example companies are real listed
companies so that the prices resolve, but every thesis, position and number in
`examples/` is invented and none of it is a view on those companies.

Credentials go in `~/.config/vertigo/`, a directory that is in no repository.
Each file must be `chmod 600`, or it is refused. Every key can also come from
the environment instead, which is how the GitHub Actions run gets them.

## Three tiers

Only the first is required. Each one adds a file and nothing else changes.

| tier | you set | you get |
|---|---|---|
| minimal | `openai.env`: `OPENAI_API_KEY` | the brief as a markdown file in `~/briefs/`. General news is omitted and the footer says so. |
| + email | `mail.env`: `SMTP_USER`, `SMTP_PASSWORD`, optionally `SMTP_HOST`, `SMTP_PORT` (default `smtp.gmail.com:587`; 465 means TLS), `SMTP_FROM`, `BRIEF_MAIL_TO` | the same brief, emailed as HTML |
| + newsletters | `newsletter.env`: `IMAP_USER`, `IMAP_APP_PASSWORD`, optionally `IMAP_HOST`, `IMAP_MAILBOX`; and `NEWSLETTER_SENDERS` | a General news section, written by the model from the newsletters in that mailbox |

Email and newsletters are independent. Without `mail.env` the brief is
emailed from the newsletter mailbox, if there is one, which is how the job was
first set up; without either, the email step logs `email: not configured` and
the run is still green.

`NEWSLETTER_SENDERS` is a comma-separated list of sender domains. It has no
default: without it, no mailbox is read. To see who is actually sending:

```bash
python3 scripts/market_brief_newsletters.py --hours 72 --discover
```

## Running it

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
python3 scripts/market_brief_gather.py --hours 24     # JSON for the AI half
python3 scripts/falsifier_monitor.py                  # the ƒ falsifier table
bash scripts/run_daily_brief.sh                       # the whole brief
```

The brief is written to `~/briefs/YYYY-MM-DD.md` (`BRIEFS_DIR`). If that
directory is a git repository, the brief is committed and pushed there. Keep
that repository **private**, because briefs contain position values.
`BRIEF_MAIL=0` skips the email step for a run. The script uses `venv/bin/python3`
when it exists, otherwise `python3` (`VERTIGO_PYTHON` overrides both).

### On GitHub Actions

`.github/workflows/daily-brief.yml` runs the job on a schedule. It does
nothing until you configure it:

- run it from a **private** copy of this repository. The run log is printed in
  the Actions output, and Actions logs on a public repository are public. A
  fork of a public repository cannot be made private, so push a clone to a new
  private repository instead.
- repository variables `VERTIGO_STATE_REPO` and `BRIEFS_REPO`: `owner/name` of
  two **private** repositories, one holding your `~/.vertigo` files and one
  receiving the briefs
- secrets `VERTIGO_PAT` (a token that can read the first repository and push to
  the second) and `OPENAI_API_KEY`

That is the minimal tier. For email add the secrets `SMTP_USER`,
`SMTP_PASSWORD` and `BRIEF_MAIL_TO` (and the variables `SMTP_HOST`, `SMTP_PORT`
if not Gmail); for newsletters add the secrets `IMAP_USER`,
`IMAP_APP_PASSWORD` and `IMAP_MAILBOX` and the variable `NEWSLETTER_SENDERS`.
The workflow checks that all three repositories are private before it reads
anything, and stops if one is not.

## What it assumes

What a stranger still needs to know before setting it up:

- **A EUR book.** Average cost is entered in EUR and every value is reported
  in EUR. Holdings priced in USD, CAD or HKD are converted at the day's rate;
  other currencies are not supported.
- **Yahoo Finance data.** Prices, valuation fields and macro series come from
  Yahoo through `yfinance`, which is unofficial and occasionally rate-limited
  or wrong. The run waits for a working Yahoo session and fails if there is
  none. The regime strip also reads FRED.
- **OpenAI only.** The judgement half calls the OpenAI API (`gpt-5.5` by
  default, `BRIEF_MODEL` overrides it) and costs API credit on every run.
  There is no switch for another provider.
- **Your own theses.** The brief compares the news with what you wrote. With
  no thesis for a holding, it says so and has nothing to compare against.
- **One user.** One book, one set of theses, one recipient.

## What is not here

This repository is the brief automation extracted from a larger private
project, with fresh history. Left out on purpose: the web app, backend and
analysis engine built earlier in 2026, the weekly performance scoreboard, the database and backfill scripts, my
own positions, theses and falsifier evaluators, and every brief the job has
produced. Comments cite design decisions as `D-n`; the design document they
refer to stayed private, but each comment states the reasoning it needs.

## Tests

```bash
pip install pytest
pytest -q tests
```

The suite is offline. By default it checks the example portfolio. Set
`VERTIGO_HOME=~/.vertigo` to also check that every `auto` falsifier in your
own `theses.yaml` has an evaluator.
