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

## How it is laid out

```
scripts/
  run_daily_brief.sh        the daily job: gather → falsifiers → regime → AI → email
  market_brief_gather.py    positions, prices, news selection (ƒ)
  falsifier_monitor.py      evaluates `check: auto` falsifiers (ƒ)
  regime_strip.py           VIX, credit spreads, curve, rates, drawdown… (ƒ)
  market_brief_newsletters.py  reads newsletters over IMAP for General news
  brief_ai.py               the AI half (OpenAI API)
  send_brief.py             renders and emails the result
  check_brief_landed.sh     optional local watchdog: re-dispatches a missing brief
skills/market-brief/        the same brief as an interactive Claude Code skill
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

Credentials go in `~/.config/vertigo/`, a directory that is in no repository:

- `openai.env`: `OPENAI_API_KEY=…`
- `newsletter.env`: IMAP/SMTP settings for a Gmail app password. See the
  docstring of `scripts/market_brief_newsletters.py`.

## Running it

```bash
pip install -r requirements.txt
python3 scripts/market_brief_gather.py --hours 24     # JSON for the AI half
python3 scripts/falsifier_monitor.py                  # the ƒ falsifier table
bash scripts/run_daily_brief.sh                       # the whole brief
```

The brief is written to `~/briefs/YYYY-MM-DD.md` (`BRIEFS_DIR`). If that
directory is a git repository, the brief is committed and pushed there. Keep
that repository **private**, because briefs contain position values.

Set `NEWSLETTER_SENDERS` to a comma-separated list of the newsletter domains
you subscribe to.

### On GitHub Actions

`.github/workflows/daily-brief.yml` runs the job on a schedule. It does
nothing until you configure it:

- repository variables `VERTIGO_STATE_REPO` and `BRIEFS_REPO`: `owner/name` of
  two **private** repositories, one holding your `~/.vertigo` files and one
  receiving the briefs
- secrets `VERTIGO_PAT` (a token that can read the first repository and push to
  the second), `OPENAI_API_KEY`, `IMAP_USER`, `IMAP_APP_PASSWORD`,
  `IMAP_MAILBOX` and `BRIEF_MAIL_TO`

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
