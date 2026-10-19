#!/usr/bin/env python3
"""Gather holdings + news for the morning brief.

Deterministic half of /market-brief: reads the positions the user actually holds,
joins whatever thesis exists for each, pulls recent news, writes JSON to stdout.
All judgement happens in the skill, not here.

REPOINTED 2026-08-17. Two changes, both from measurement:

1. Universe was a database WATCHLIST that overlapped the real holdings by two
   names. It monitored companies that were not owned and ignored most of the
   ones that were, including the largest. Universe is now
   ~/.vertigo/positions.csv, stock sleeve only. ETFs have no company thesis to
   falsify.

2. News source was Finnhub — US-only free tier, covering 3 of the 9 holdings and
   requiring an API key. Google News RSS covered 9/9, keyless. Finnhub is
   dropped, which removes the last API key from this tool.

EXTENDED 2026-08-18, all three from reading a live brief rather than the code:

3. `<source>` never parsed. The tag regex was `<source>`; Google emits
   `<source url="...">Reuters</source>`. 0 of 43 items carried an outlet, which
   also disabled the " - Outlet" strip, so every headline kept its suffix.

4. Relevance filter. Google matches loosely enough that one holding's whole
   feed was about other companies. Off-topic items are dropped and COUNTED, and a
   company left with nothing is named in `no_company_news` — the brief must be
   able to tell "nothing important happened" from "we found no coverage".

5. Cross-day memory. Windows overlap, so the same story is served on
   consecutive days; the brief had no way to know and reclassified one holding
   twice off one set of articles. Items now carry `seen_before` / `first_seen`,
   matched on URL *and* normalised headline so the same story from a different
   outlet is still recognised. State lives in ~/.vertigo/news-seen.json.

6. Per-company search settings live in ~/.vertigo/news.yaml, not in this file.
   See examples/vertigo-home/news.yaml for the schema.

Runs fully offline-capable except for the news fetch: no Supabase, no auth.

Usage:  python3 gather.py [--hours 24] [--vertigo-home ~/.vertigo]
                          [--no-write-seen]
"""
import argparse, csv, datetime, glob, html, json, os, re, sys, time
# yf_util lives beside this file. Was a hardcoded absolute path while
# this script lived outside the repo; now that it is in scripts/ the sibling
# directory is the right answer and survives the repo being cloned anywhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import urllib.error, urllib.parse, urllib.request

DEFAULT_HOME = os.path.expanduser("~/.vertigo")

# Rolling record of every item already served to a brief, so the next run can
# say which stories it has shown before. 14 days is well past the longest
# window (72h) and keeps the file to a few hundred rows.
SEEN_FILE = "news-seen.json"
SEEN_KEEP_DAYS = 14

# A holding that moved this far gets a WIDER news window when the normal one
# came back empty. `when:Nd` is a hard cutoff and the story driving a move is
# routinely a day older than the move. Measured 2026-08-26: one holding fell
# 18%, the largest move in the book, and the 24h feed returned zero items
# while the same query at 48h returned five about the takeover fight its
# falsifier is written against. The brief said "the cause is unclear".
# The threshold matches the MOVE RULE's own >= 4% in run_daily_brief.sh: the
# window widens for exactly the moves the model is required to explain.
MOVE_ESCALATE_PCT = 4.0
ESCALATE_HOURS = 72

# Surfaced in the JSON so a silently-ignored thesis file cannot hide on stderr.
_WARNINGS = []

# Google News needs a browser User-Agent (the exact inverse of FRED, which
# tarpits one — see fred_util.py).
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Per-company search settings, keyed by ISIN, read from ~/.vertigo/news.yaml
# (see examples/vertigo-home/news.yaml). `query` is the news query; `lang`
# picks the Google News edition — a company with only local-language coverage
# reads better in that edition.
#
# NO `OR` in a query. Measured 2026-08-17: combining OR with the `when:` time
# filter silently returns ZERO results — `"Acme Corp" when:7d` gives 7 items,
# `"Acme Corp" OR "Acme Corporation" when:7d` gives 0. It fails quietly, which
# is the worst way for a query to be wrong.
#
# A query may be a LIST. Since OR is unusable here, a company whose coverage
# runs under more than one name needs more than one fetch. Measured 2026-08-26:
# a holding whose coverage ran under its listed SUBSIDIARY's name returned 0
# on-topic items at 24h and at 72h for three days running, while the
# subsidiary's name returned 12. The subsidiary was already in `aliases`; the
# query list simply never asked for it. The alias list and the query list have
# to agree, or the filter accepts a name nothing ever fetches.
#
# `aliases` exist because Google News matches the query LOOSELY. Measured
# 2026-08-18: the largest holding returned three items (a chair being sacked
# at an oil major, a wood-composites market forecast, a hedge-fund 13F) and
# not one of them named the company. All three reached the brief, which filed
# it under "Noise". "Noise" claims we looked and found nothing important; the
# true claim was "no company-specific news". Those are different claims and
# only one is honest.
#
# So an item counts only if its headline names the company. OPERATING BRANDS
# count too: news about a grocer's store banner IS news about the grocer, and a
# listed-name-only filter would trade one silent failure for another.
NEWS_FILE = "news.yaml"
NEWS = {}      # isin -> (query or [queries], lang)
ALIASES = {}   # isin -> [lowercase names that count as naming the company]

EDITIONS = {"en": ("en", "US", "US:en"), "de": ("de", "DE", "DE:de")}


def load_news_config(home):
    """~/.vertigo/news.yaml -> (NEWS, ALIASES, THESIS_TICKER_ALIAS).

    Missing file or missing entry is not an error: a holding with no entry is
    searched under its name from positions.csv and matched on that name alone.
    A file that exists but does not parse IS reported, because it means every
    holding silently fell back to the weaker default.
    """
    path = os.path.join(home, NEWS_FILE)
    if not os.path.exists(path):
        return {}, {}, {}
    try:
        import yaml
        d = yaml.safe_load(open(path)) or {}
    except Exception as e:
        _WARNINGS.append(f"{NEWS_FILE} did not parse ({type(e).__name__}) — "
                         "every holding is searched under its plain name this run")
        return {}, {}, {}
    news, aliases = {}, {}
    for isin, c in (d.get("companies") or {}).items():
        c = c or {}
        if c.get("query"):
            news[isin] = (c["query"], c.get("lang", "en"))
        if c.get("aliases"):
            aliases[isin] = [str(a).lower() for a in c["aliases"]]
    return news, aliases, d.get("thesis_ticker_alias") or {}


# 13F churn. One large-cap holding returned 96 items on 2026-08-18 and the eight
# newest — the only eight that fit the cap — were all machine-generated
# position-change stories ("Example Bancorp Sells 74,666 Shares of Acme Inc.
# $ACME"). The one item that bore on the falsifier, a bank note on consumer
# spending with the company's earnings as the barometer, sat below the cut. This genre is news about
# somebody else's portfolio, never about the company, and it is generated in
# enough volume to crowd out everything else.
#
# Patterns are deliberately narrow. "Acme Completes Acquisition of a
# Controlling 51% Stake in Beta Energy" is real corporate news and must
# survive, so every pattern here requires the SHARE-TRADING form (a firm buying
# or selling the stock), not the word "stake" or "acquires" on its own.
# Patterns requiring the holding to be the OBJECT (see is_churn). Extended
# 2026-08-26: the original set caught 1 of 11 churn items on one holding and 1
# of 8 on another, because MarketBeat's generator has many more shapes than
# "Shares Purchased By" — "Takes Position in", "Takes $1.33 Million Position
# in", "Purchases New Stake in", "Makes New Investment in", "Invests $20.76
# Million in", "681,087 Shares in Acme Inc. $ACME Purchased by", and
# "Purchases Shares of 5,528" (the count AFTER the noun, which the original
# numeric pattern could not match).
CHURN_OBJECT = [
    r"\bshares?\s+(sold|purchased|bought|acquired)\s+by\b",
    r"\bshares?\s+in\b.{0,60}?\b(purchased|acquired|bought|sold)\s+by\b",
    r"\b(boosts?|lifts?|lowers?|raises?|trims?|reduces?|increases?|grows?|cuts?)\s+"
    r"(its\s+)?(stock\s+)?(holdings|position|stake)\s+in\b",
    r"\b(holdings|position|stake)\s+(boosted|lifted|trimmed|lowered|raised|reduced)\s+by\b",
    r"\bhas\s+\$[\d.,]+\s*(million|billion)?\s+(stock\s+)?(holdings|position)\s+in\b",
    r"\b(sells?|buys?|acquires?|purchases?)\s+(new\s+)?[\d,]+\s+shares\s+(of|in)\b",
    r"\b(buys?|acquires?|purchases?)\s+(new\s+)?shares\s+(of|in)\b",
    r"\btakes?\s+(an?\s+)?(new\s+)?(\$[\d.,]+\s*(million|billion)?\s+)?(position|stake)\s+in\b",
    r"\b(purchases?|buys?|acquires?)\s+(an?\s+)?new\s+(position|stake)\s+in\b",
    r"\bmakes?\s+(an?\s+)?new\s+(\$[\d.,]+\s*(million|billion)?\s+)?investments?\s+in\b",
    r"\binvests?\s+\$[\d.,]+\s*(million|billion)?\s+in\b",
    r"\b[\d,]{3,}\s+shares\s+in\b",
    r"\bunloads\b.{0,40}\bstock\b",
]
# Safe without the object test: no operating company describes itself this way.
CHURN_ANY = [r"\bshort interest\b"]
_CHURN_OBJECT_RE = [re.compile(p, re.I) for p in CHURN_OBJECT]
_CHURN_ANY_RE = [re.compile(p, re.I) for p in CHURN_ANY]


def is_churn(headline, aliases=()):
    """13F churn: somebody else's fund reporting a trade in this holding's stock.

    The discriminator against real corporate M&A is STRUCTURAL, not lexical. In
    churn the holding is the OBJECT — "Advisors LLC Takes Position in Acme".
    In real news it is the SUBJECT — "Acme Takes Control of Beta Energy",
    "Acme closes US$1.2 billion deal to acquire 51% controlling stake".
    Both sentences contain a buying verb and the word stake; only one of them is
    about somebody else's portfolio. So a pattern in CHURN_OBJECT counts only
    when an alias appears AFTER the match, which is what "the holding is being
    bought" looks like in a headline.

    `aliases` defaults to empty for the legacy single-argument call, which then
    only applies the CHURN_ANY patterns. Callers with aliases should pass them.
    """
    h = headline.lower()
    if any(r.search(h) for r in _CHURN_ANY_RE):
        return True
    for r in _CHURN_OBJECT_RE:
        m = r.search(h)
        if m and any(re.search(rf"\b{re.escape(a)}\b", h[m.end():]) for a in aliases):
            return True
    return False


# Retail-deal SEO and local incident reporting. Both genres genuinely name the
# company, so `names_company` cannot help — they are simply not news about the
# BUSINESS. Measured 2026-08-26: of 80 on-topic items for one retailer, 18 were
# product deals ("Acme's 'Flattering' $17 Cropped Cardigan Is 'Fantastic
# Quality'") and 20 were local incidents (police reports from store car
# parks). Together with 11 churn items that is
# 49 of 80, and the eight the model actually saw were drawn from that majority.
#
# Deliberately narrow. The bare word "deal" is NOT a pattern here: "Acme
# closes US$1.2 billion deal to acquire 51%" can be the most important company
# news in the book that week. Same for "investigation" and "lawsuit" — a regulatory
# probe is news, a shoplifting arrest is not.
PROMO = [
    r"\bon sale\b", r"\b\d{1,2}%\s*off\b", r"\bis (now )?just \$\d", r"\bonly \$\d",
    r"\bflash deal\b", r"\bbest .{0,24}deals\b",
    r"\b(labor day|black friday|prime day|cyber monday) deals?\b",
    r"\bdrops? .{0,24}\bdeal\b", r"\bclearance\b", r"\bshop .{0,30}\bmore\b",
    r"\$\d{1,3}\s+(\w+\s+){0,2}"
    r"(sneakers|cardigan|comforter|sofa|chair|shed|light|headphones|glider|decor|"
    r"laptop|mattress|blender|cookware|leggings|jeans|boots|tv)\b",
    r"\b(bestselling|top-rated|must-have)\b",
    r"\bvs\.?\s+\w+:\s*which is better\b",
]
# Local incidents. A crime in a car park is not a fact about the business, and
# these outpublish company coverage because there are thousands of stores.
LOCAL = [
    r"\bpolice\b", r"\barrest(ed|s)?\b", r"\bstabb(ed|ing)\b", r"\bshoot(ing|er)\b",
    r"\brobbery\b", r"\bjailed\b", r"\bvoyeurism\b", r"\bparking lot\b",
    r"\baccused of\b", r"\ballegedly\b", r"\bsuspect\b",
    r"\bkilled\b", r"\bcrash(ing|es)?\b", r"\bfatal\b",
    r"\bzoning\b", r"\bproperty (tax|assessment)\b", r"\bcemetery\b", r"\btowed\b",
]
_PROMO_RE = [re.compile(p, re.I) for p in PROMO]
_LOCAL_RE = [re.compile(p, re.I) for p in LOCAL]


def is_promo(headline):
    return any(r.search(headline) for r in _PROMO_RE)


def is_local(headline):
    return any(r.search(headline) for r in _LOCAL_RE)


def names_company(headline, aliases):
    """Word-boundary match, not substring: a three-letter alias would
    otherwise hit inside unrelated words."""
    h = headline.lower()
    return any(re.search(rf"\b{re.escape(a)}\b", h) for a in aliases)


def story_key(headline):
    """Normalised headline, used to recognise the same story from a different
    outlet. Same key the in-run syndication dedupe uses, reused across days."""
    return re.sub(r"[^a-z0-9]+", " ", headline.lower()).strip()[:70]


# Words that carry no signal when comparing two headlines about the same
# holding. The company's own name is stripped separately, per-holding: it
# appears in EVERY headline in that feed and so contributes similarity that
# means nothing.
_STOP = set("""a an the and or of in on at to for from with by is are was were be been its it
this that as after before over under new more most than then us up down out about into amid
says say said will could would can may might not no has have had who what why how still yet
also""".split())


def _stem(w):
    """Crude suffix strip. Without it "profit jumps" and "profit jumped" are
    different stories, which is how six retellings of one earnings release
    survived as six items."""
    for suf in ("ing", "ed", "es", "s"):
        if len(w) - len(suf) >= 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _content(headline, aliases=()):
    w = {_stem(x) for x in re.findall(r"[a-z0-9]+", headline.lower())
         if x not in _STOP and len(x) > 2}
    for a in aliases:
        w -= {_stem(x) for x in re.findall(r"[a-z0-9]+", a.lower())}
    return w


# Merge only on strong overlap, and never through a headline too short to carry
# evidence. Measured 2026-08-26: at 0.5 with no length floor, the three-token
# headline of the form "<Company> <ticker> HK" matched both a price-target story
# and the half-year profit story, bridging them into one cluster. At these
# settings every multi-item cluster across seven holdings was inspected by hand
# and none was wrong. It under-merges instead, which is the cheap direction:
# a missed merge wastes a slot, a wrong merge deletes a story silently.
SAME_STORY_OVERLAP = 0.6
SAME_STORY_MIN_TOKENS = 4


def same_story(a, b, aliases=()):
    ta, tb = _content(a, aliases), _content(b, aliases)
    if len(ta) < SAME_STORY_MIN_TOKENS or len(tb) < SAME_STORY_MIN_TOKENS:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= SAME_STORY_OVERLAP


def dedupe_stories(items, aliases=()):
    """Collapse retellings into one item carrying `retellings`.

    google_news already drops near-identical headlines within a call, which is
    far too weak: one acquisition arrived as six items with six different
    wordings, one set of half-year results as six, and one payments story as
    eight. A cap of 8 filled with one story reads to
    the model as eight independent sources.
    """
    out = []
    for it in items:
        for k in out:
            if any(same_story(it["headline"], m, aliases) for m in k["_cluster"]):
                k["_cluster"].append(it["headline"])
                k["retellings"] = len(k["_cluster"]) - 1
                break
        else:
            k = dict(it)
            k["_cluster"] = [it["headline"]]
            k["retellings"] = 0
            out.append(k)
    return out


# What a story about the BUSINESS sounds like, as opposed to one about the
# share price or the car park. Used only for ranking, never for dropping.
_EVENT_RE = re.compile(
    r"earnings|results|quarter|q[1-4]\b|guidance|outlook|forecast|revenue|profit|margin"
    r"|sales|dividend|buyback|acquisition|acquire|merger|takeover|divest|spin-?off"
    r"|ceo|cfo|chair|board|executive|resign|depart|appoint|lawsuit|sued|court|regulator"
    r"|antitrust|probe|investigation|fine|sanction|strike|union|layoff|jobs|plant|mine"
    r"|output|production|capacity|downgrade|upgrade|price target|rating|valuation"
    r"|overvalued|undervalued|multiple|traffic|comparable|comps|market share|tariff"
    r"|launch|expands?|partnership|funding|capex|flood|outage|halt|recall|approval"
    r"|fast-?track|phase i|trial|patent|refinanc|restructur|takeover", re.I)

# How hard a near-duplicate is pushed down the ranking. Set above the value of
# a single thesis-term hit so that a second telling of a story already picked
# never outranks a first telling of a different one.
DIVERSITY_PENALTY = 4.0


def rank_news(items, thesis_terms, aliases=(), cap=8):
    """Choose which items reach the model, and in what order.

    Until 2026-08-26 this was `kept[:8]` — newest first. That is the wrong
    selector for a name with real volume, because the genres that publish
    continuously win a recency race by construction: on 2026-08-26 one
    retailer returned 80 on-topic items and six of the eight newest were 13F
    churn, while a story on why it was cutting prices by $2.9 billion and a
    valuation piece sat below the cut. The brief filed it under "nothing worth
    reporting".

    Ranked by contact with what THE USER WROTE first, then by whether it sounds like
    a business event, then by how widely it was retold, with recency only as a
    tiebreak. Each pick is penalised by its similarity to what is already
    picked, so the slots spread across distinct stories rather than eight
    angles on one.

    NOTE the bias this introduces: ranking by overlap with the user's own
    thesis text favours news phrased in their own vocabulary, which is a mild confirmation
    loop. It is bounded — the event and diversity terms are independent of the
    thesis, large moves route through the MOVE RULE regardless of news, and
    nothing here DROPS an item, it only orders them. Worth revisiting at the
    week-8 review.
    """
    pool, picked = list(items), []
    while pool and len(picked) < cap:
        best, best_s = None, None
        for it in pool:
            content = _content(it["headline"], aliases)
            s = 3.0 * len(thesis_terms & content)
            s += 2.0 * len(set(m.lower() for m in _EVENT_RE.findall(it["headline"])))
            s += 0.5 * min(it.get("retellings", 0), 4)
            if picked:
                sim = max(
                    (len(content & _content(q["headline"], aliases))
                     / max(1, min(len(content), len(_content(q["headline"], aliases))))
                     for q in picked), default=0.0)
                s -= DIVERSITY_PENALTY * sim
            if best_s is None or s > best_s:
                best, best_s = it, s
        picked.append(best)
        pool.remove(best)
    return picked


def thesis_terms(entry):
    """Content words from the thesis and every falsifier statement."""
    txt = " ".join([entry.get("thesis") or "", entry.get("future_thesis") or ""]
                   + [f.get("statement", "") for f in (entry.get("falsifiers") or [])])
    return _content(txt)


def load_etf_macro(home):
    """~/.vertigo/etf-macro.yaml — macro drivers for the passive sleeve.

    Kept OUT of theses.yaml on purpose. That file is a conviction journal whose
    schema is thesis, falsifiers, conviction, canon; an ETF would leave nearly
    all of it blank, and blank fields invite someone to fill them with a thesis
    an index fund does not have. Same driver schema, different file, joined by
    ISIN exactly as theses.yaml is.
    """
    path = os.path.join(home, "etf-macro.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        d = yaml.safe_load(open(path)) or {}
    except Exception as e:
        _WARNINGS.append(f"etf-macro.yaml did not parse ({type(e).__name__}) — "
                         "the ETF sleeve has no macro drivers this run")
        return {}
    return d.get("etfs") or {}


def load_seen(home):
    """{key: first-seen ISO date}. Keys are URLs and normalised headlines."""
    path = os.path.join(home, SEEN_FILE)
    if not os.path.exists(path):
        return {}
    try:
        d = json.load(open(path))
        return d if isinstance(d, dict) else {}
    except Exception as e:
        _WARNINGS.append(f"{SEEN_FILE} did not parse ({type(e).__name__}) — "
                         "every item will look new this run")
        return {}


def save_seen(home, seen, today):
    """Prune past the retention window, then write. Atomic: a half-written
    state file would make a whole run's worth of stories look new."""
    cutoff = (today - datetime.timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    keep = {k: v for k, v in seen.items() if v >= cutoff}
    path = os.path.join(home, SEEN_FILE)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(keep, fh, indent=0, sort_keys=True)
        os.replace(tmp, path)
    except Exception as e:
        _WARNINGS.append(f"could not write {SEEN_FILE} ({type(e).__name__}) — "
                         "tomorrow's brief will not know what today showed")

# The legacy thesis export is keyed by ticker and can predate the current
# listings, so a holding may need an explicit bridge (`thesis_ticker_alias` in
# news.yaml, e.g. a Munich listing to the home-exchange ticker). Everything
# else simply has no thesis yet — which the brief must say plainly rather than
# hide.
THESIS_TICKER_ALIAS = {}


def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        rows = [l for l in fh if l.strip() and not l.startswith("#")]
    return list(csv.DictReader(rows))


def load_theses_yaml(home):
    """~/.vertigo/theses.yaml — the canonical conviction journal (D-1).

    Joined by ISIN (D-3), not ticker: the same company trades under different
    symbols across venues, and joining by ticker needed an alias bridge. Returns {} if the file or a YAML parser is absent, so
    the JSON export remains the fallback.
    """
    path = os.path.join(home, "theses.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml
    except ImportError:
        msg = "theses.yaml present but PyYAML is not installed — theses ignored"
        print(msg, file=sys.stderr)
        _WARNINGS.append(msg)
        return {}
    try:
        data = yaml.safe_load(open(path)) or {}
    except Exception as e:
        msg = f"theses.yaml EXISTS BUT DID NOT PARSE ({type(e).__name__}: {e})"
        print(msg, file=sys.stderr)
        _WARNINGS.append(msg)
        return {}
    out = {}
    for t in (data.get("theses") or []):
        if t.get("isin"):
            out[t["isin"]] = t
    return out


def load_theses(home):
    """Newest local thesis export. Local-first per D-1: no Supabase at runtime."""
    files = sorted(glob.glob(os.path.join(home, "theses_export_*.json")))
    if not files:
        return {}
    try:
        data = json.load(open(files[-1]))
    except Exception:
        return {}
    by_ticker = {}
    for t in (data.get("theses") or []):
        tk = t.get("ticker")
        if tk and tk not in by_ticker:      # newest wins; export is date-ordered
            by_ticker[tk] = t
    return by_ticker


def google_news(query, lang, hours, tries=3):
    """Google News RSS, time-filtered SERVER-side.

    The feed is ordered by RELEVANCE, not date, and returns up to 100 items
    reaching back weeks. Filtering client-side therefore discards most of a
    window: a 48h cutoff over a relevance-ordered feed returned zero items for
    the largest holding while it was in the news. `when:Nd` makes Google do the
    filtering, so everything that comes back is already in range.
    """
    hl, gl, ceid = EDITIONS.get(lang, EDITIONS["en"])
    days = max(1, (hours + 23) // 24)
    q = f"{query} when:{days}d"
    url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}"
           f"&hl={hl}&gl={gl}&ceid={ceid}")
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            xml = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
            break
        except Exception as e:
            if n == tries - 1:
                return None, f"{type(e).__name__}"
            time.sleep(3 * (n + 1))
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        # `\b[^>]*>` because Google emits `<source url="...">Reuters</source>`.
        # A plain `<source>` never matched, so `source` was silently "" on every
        # item since this file was written — which also disabled the " - Outlet"
        # strip below, leaving the outlet glued to every headline. Measured
        # 2026-08-18: 0 of 43 items carried a source.
        def grab(tag):
            m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", item, re.S)
            if not m:
                return ""
            # unescape: RSS carries `&amp;`, and a headline reading
            # "TSX &amp; TSXV small caps" is what then lands in the brief.
            return html.unescape(
                re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip()
        title, link, pub, src = grab("title"), grab("link"), grab("pubDate"), grab("source")
        when = None
        try:
            when = datetime.datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")
            when = when.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            pass
        if when is not None and when < cutoff:
            continue
        # Google appends " - Source" to titles; strip it, we have the source.
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3]
        out.append({"headline": title, "source": src, "url": link,
                    "at": when.isoformat() if when else None,
                    "_when": when})
    # Dedupe syndicated repeats: the same wire story appears from several
    # outlets with identical or near-identical headlines.
    seen, uniq = set(), []
    for it in out:
        k = re.sub(r"[^a-z0-9]+", " ", it["headline"].lower()).strip()[:70]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    uniq.sort(key=lambda x: (x["_when"] is None, x["_when"]), reverse=False)
    uniq = sorted([u for u in uniq if u["_when"]], key=lambda x: x["_when"], reverse=True) \
           + [u for u in uniq if not u["_when"]]
    for u in uniq:
        u.pop("_when", None)
    return uniq, None


def fetch_news(query, lang, hours):
    """Run one or several queries and merge the results.

    `google_news` dedupes syndicated repeats WITHIN a call; two calls for the
    same company can still return the same wire story, so the same story_key
    dedupe is applied across calls here. Order stays newest-first overall.
    An error is returned only when EVERY query failed: one dead query must not
    blank a company that another query covered.
    """
    queries = [query] if isinstance(query, str) else list(query)
    merged, errors, keys = [], [], set()
    for q in queries:
        items, err = google_news(q, lang, hours)
        if err:
            errors.append(f"{q}: {err}")
            continue
        for it in items:
            k = story_key(it["headline"])
            if k in keys:
                continue
            keys.add(k)
            merged.append(it)
    if errors and len(errors) == len(queries):
        return None, "; ".join(errors)
    # Same idiom google_news uses: sort the dated items newest-first and put
    # the undated ones after, rather than sorting None against a timestamp.
    dated = sorted([m for m in merged if m["at"]], key=lambda x: x["at"], reverse=True)
    return dated + [m for m in merged if not m["at"]], None


def position_value(shares, price_series, price_currency, fx_series):
    """One row's EUR value, the bar it is priced from, and the bar the rate came from.

    Returns `(value, price_date, fx_date)`, any of which may be None.

    Split out of `main` for the same reason `positions_table` was: while this
    lived inline in a loop wrapped around live fetches, the rule it enforces was
    unreachable from a test. It was mutation-testing that made the case —
    replacing the `fx_asof` call at the call site with the original
    `.iloc[-1]` left the entire suite green, because the helper and the warning
    were covered and the wiring between them was not.

    `fx_series` is the series for `price_currency`, or None. EUR rows need none
    and report the price date as their FX date, so a caller comparing the two
    sees no spurious mismatch.
    """
    from yf_util import fx_asof
    if price_series is None or not len(price_series):
        return None, None, None
    price_date = price_series.index[-1].date()
    if price_currency == "EUR":
        rate, fx_date = 1.0, price_date
    else:
        rate, fx_date = fx_asof(fx_series, price_date)
    if not rate:
        return None, price_date, fx_date
    return float(shares) * float(price_series.iloc[-1]) * rate, price_date, fx_date


def positions_table(positions, cash_eur, today):
    """Render the `ƒ` positions table. Returns `(markdown, warnings)`.

    Pure: every price is already on `positions` from the fetch loops in `main`,
    so this touches no network and no clock beyond the `today` passed in. Split
    out from `main` because the session-disclosure rule below is the part that
    has to be tested, and it could not be reached while it lived inline.

    SESSION DISCLOSURE. Every row is priced at its own last bar, which is right:
    a broker screen does the same, and pinning this table to one shared date is
    a known bug (pinning once showed a small-cap at EUR 0.174 against 0.258 in
    the broker app). What was missing is saying so. Yahoo drops the
    European venues for a day at a time: on 2026-08-22 and again on 2026-08-25
    there was no bar for .DE/.MU/.AS/.PA while US, .TO and .HK listings had one, so
    ten of thirteen rows reported the session before last under a header that
    read "last session close to close", on a Tuesday.
    """
    from yf_util import session_spread
    warnings = []
    rows, tot = [], {"stock": 0.0, "etf": 0.0}
    for p in sorted(positions, key=lambda x: -(x.get("_mkt") or 0)):
        mv, mv1 = p.get("_mkt"), p.get("_move_1d")
        if mv:
            tot[p.get("sleeve", "stock")] = tot.get(p.get("sleeve", "stock"), 0.0) + mv
        delta = (mv - mv / (1 + mv1 / 100)) if (mv and mv1 is not None) else None
        rows.append((p.get("name") or p["ticker"], mv, mv1, delta,
                     p.get("sleeve"), p.get("broker"),
                     p.get("_mkt_asof"), p.get("_bar_last"), p.get("_fx_asof")))
    invested = sum(tot.values())
    newest, stale_dates = session_spread(r[6] for r in rows)

    # The day-delta is the one number here that claims a single shared day, so
    # it is the one number a mixed session makes false. On 2026-08-25 it read
    # +41 by adding three Fri->Mon deltas to ten Thu->Fri ones. Suppressed
    # rather than relabelled: a sum across two sessions has no honest label
    # short of naming both, and a number that silently flatters is worse than
    # one that is absent.
    mixed = bool(stale_dates)
    day_delta = None if mixed else sum(r[3] for r in rows if r[3] is not None)
    if mixed:
        warnings.append(
            f"positions table spans more than one session: newest bar {newest}, "
            "also holding " + ", ".join(str(d) for d in stale_dates)
            + " — day-delta total suppressed")

    # A row whose value and percent came from different last bars is a separate
    # defect: they are two fetches (period=5d for value, period=1mo for the
    # move) and nothing forces them onto the same bar.
    for nm, _mv, _m1, _d, _s, _b, a_val, a_mov, a_fx in rows:
        if a_val and a_mov and a_val != a_mov:
            warnings.append(f"{nm}: value priced {a_val} but move measured to "
                            f"{a_mov} — two fetches disagreed")
        # After fx_asof this should not fire: the rate is chosen FOR the price
        # date. It still can, when a venue traded on a day its currency pair
        # published no bar, and then the row really is a cross-date product.
        if a_val and a_fx and a_val != a_fx:
            warnings.append(f"{nm}: priced {a_val} but converted at the "
                            f"{a_fx} rate — no FX bar on the price date")

    out = [f"\n## `ƒ` Positions — {today:%A %-d %B %Y}\n"]
    # Never call an unsettled bar a close. A bar dated today is the session in
    # progress, not its close: the 07:15 UTC retry cron fires at 09:15 CEST,
    # fifteen minutes after Xetra opens, so this is a production path and not
    # just an artefact of ad-hoc runs.
    if not newest:
        asof_txt = "close"
    elif newest >= today:
        asof_txt = f"{newest} intraday, not yet a close,"
    else:
        asof_txt = f"close {newest}"
    out.append(f"*{asof_txt} against the prior close · EUR · price only, "
               "excludes dividends and fees*\n")
    out.append("| position | value | last session | | broker |")
    out.append("|---|---:|---:|---:|:--|")
    for name, mv, mv1, d, sleeve, broker, a_val, _a_mov, _a_fx in rows:
        if not mv:
            out.append(f"| {name[:34]} | — | — | | {broker or ''} |")
            continue
        # Mark only the laggards. On an aligned day the table reads exactly as
        # it always has; on a split day the dagger is the whole point.
        lag = "†" if (a_val and newest and a_val != newest) else ""
        m = "—" if mv1 is None else f"{mv1:+.2f}%{lag}"
        dd = "" if d is None else f"{d:+,.0f}"
        out.append(f"| {name[:34]} | {mv:,.0f} | {m} | {dd} | {broker or ''} |")
    out.append(f"| **Stock picks** | **{tot.get('stock', 0):,.0f}** | | | |")
    out.append(f"| **ETF sleeve** | **{tot.get('etf', 0):,.0f}** | | | |")
    out.append(f"| Cash | {cash_eur:,.0f} | | | |")
    dtot = "**—**" if day_delta is None else f"**{day_delta:+,.0f}**"
    out.append(f"| **Total** | **{invested + cash_eur:,.0f}** | | {dtot} | |")
    out.append("\n`ƒ` every number computed by Python from live prices; "
               "no model was involved.")
    if stale_dates:
        out.append("`ƒ` † priced to an older close ("
                   + ", ".join(str(d) for d in stale_dates)
                   + f"), not {newest}: the data provider published no newer "
                     "bar for those venues.")
        out.append("`ƒ` **The day total is suppressed** because these rows do "
                   "not share one session, and a sum across two days is not a "
                   "day's move.")
    return "\n".join(out) + "\n", warnings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--vertigo-home", default=DEFAULT_HOME)
    ap.add_argument("--table-out",
                    help="also write the ƒ positions table (markdown) to this path")
    ap.add_argument("--no-write-seen", action="store_true",
                    help="mark stories seen-before but do not record this run "
                         "(use for ad-hoc runs that should not affect tomorrow)")
    args = ap.parse_args()
    home = os.path.expanduser(args.vertigo_home)
    _news, _aliases, _bridge = load_news_config(home)
    NEWS.update(_news)
    ALIASES.update(_aliases)
    THESIS_TICKER_ALIAS.update(_bridge)

    positions = load_csv(os.path.join(home, "positions.csv"))
    if not positions:
        print(json.dumps({"error": f"no positions.csv under {home}"}), file=sys.stdout)
        return 1
    picks = [p for p in positions if p.get("sleeve") == "stock"]
    theses_yaml = load_theses_yaml(home)      # canonical, joined by ISIN
    theses = load_theses(home)                # legacy export, joined by ticker

    # MARKET weight, not cost weight (D-12a). Cost weight measures a past
    # decision; market weight measures current exposure. A position bought
    # small years ago can be a large share of the sleeve today — ranking by
    # cost buries it.
    try:
        from yf_util import series as _series
        # 1mo, not 5d. The rate has to be available ON THE ROW'S PRICE DATE,
        # and a price can lag by several sessions when Yahoo drops a venue, so
        # a five-day FX window can fail to reach back far enough. Costs nothing.
        _fx = {c: _series(pair, period="1mo", adjust=False, quiet=True)
               for c, pair in {"USD": "USDEUR=X", "CAD": "CADEUR=X", "HKD": "HKDEUR=X"}.items()}
        for p in positions:
            s_ = _series(p["ticker"], period="5d", adjust=False, quiet=True)
            if s_ is None:
                p["_mkt"] = None
                p["_mkt_asof"] = None
                p["_fx_asof"] = None
                continue
            # `_mkt_asof` is recorded, not just used: Yahoo drops the
            # European venues for a day at a time, so "the last bar" is not the
            # same session for every row and the table has to say so rather
            # than imply one shared day. The FX leg is part of this row's
            # price, not a separate reading — see position_value and fx_asof.
            p["_mkt"], p["_mkt_asof"], p["_fx_asof"] = position_value(
                p["shares"], s_, p["price_currency"], _fx.get(p["price_currency"]))
    except Exception:
        for p in positions:
            p["_mkt"] = None
            p["_mkt_asof"] = None
            p["_fx_asof"] = None
    # Price move over the window, computed by Python. the model was classifying
    # one holding inconsistently because "TSX:S Jumps 6.90%" is, to it, a headline
    # STRING. Everything it classified reliably was explicit in the text
    # ("reports August 21", "FDA Fast-Track"); the one case needing an inference
    # from a claimed price move was the one that flip-flopped. A move is a fact —
    # so compute it and stop asking a model to infer it.
    try:
        from yf_util import series as _s2
        _days = max(2, (args.hours + 23) // 24 + 1)
        for p in positions:
            h = _s2(p["ticker"], period="1mo", adjust=False, quiet=True)
            if h is None or len(h) < 2:
                p["_move"] = None; p["_move_1d"] = None; p["_bar_last"] = None
                p["_bar_prev"] = None; continue
            # The two bars `_move_1d` spans. This is a SECOND fetch (period=1mo
            # against the value loop's 5d), so it can land on a different last
            # bar than the value column did; `_mkt_asof` vs `_bar_last` is
            # checked at render time rather than assumed equal.
            p["_bar_last"] = h.index[-1].date()
            p["_bar_prev"] = h.index[-2].date()
            past = h[h.index <= h.index[-1] - __import__("pandas").Timedelta(days=_days)]
            base = float(past.iloc[-1]) if len(past) else float(h.iloc[0])
            p["_move"] = round((float(h.iloc[-1]) / base - 1) * 100, 1)
            # LAST SESSION, close to prior close — what a broker screen shows.
            # `_move` spans the news window (~2 days, more over a weekend) and
            # is what the MOVE RULE tests; it is not "how did this move
            # yesterday", which is the question the positions table answers.
            p["_move_1d"] = round((float(h.iloc[-1]) / float(h.iloc[-2]) - 1) * 100, 2)
    except Exception:
        for p in positions:
            p["_move"] = None; p["_move_1d"] = None
            p["_bar_last"] = None; p["_bar_prev"] = None
    # TWO denominators, because conflating them misled for two days. Every
    # "57% of the sleeve" in the brief meant 57% of the STOCK sleeve, i.e. a
    # quarter of the money — and the reader hears "my portfolio". When the ETF
    # sleeve is the larger half and the single largest position is an index
    # fund, the brief was speaking about
    # the smaller half while sounding like it spoke about the whole.
    priced_all = [p for p in positions if p.get("_mkt")]
    total_book = sum(p["_mkt"] for p in priced_all) or None
    priced = [p for p in picks if p.get("_mkt")]
    total_mkt = sum(p["_mkt"] for p in priced) or None
    total_cost = sum(float(p["shares"]) * float(p["avg_price_eur"]) for p in picks) or 1.0

    seen = load_seen(home)
    today = datetime.date.today()
    today_iso = today.isoformat()

    companies, gaps, no_news = [], [], []
    for p in picks:
        isin, tk = p["isin"], p["ticker"]
        y = theses_yaml.get(isin)
        th = theses.get(THESIS_TICKER_ALIAS.get(tk, tk), {})
        cost = float(p["shares"]) * float(p["avg_price_eur"])
        entry = {
            "company": p.get("name") or tk,
            "isin": isin,
            "ticker": tk,
            "broker": p.get("broker"),
            "sleeve_weight_pct": (round(p["_mkt"] / total_mkt * 100, 1)
                                  if p.get("_mkt") and total_mkt else None),
            # Share of the WHOLE book, ETFs included. This is the honest number
            # to weight a decision by; sleeve_weight_pct is stock-relative and
            # runs about 2.4x larger.
            "portfolio_weight_pct": (round(p["_mkt"] / total_book * 100, 1)
                                     if p.get("_mkt") and total_book else None),
            "cost_weight_pct": round(cost / total_cost * 100, 1),
            "weight_basis": "market" if (p.get("_mkt") and total_mkt) else "cost (price unavailable)",
            "price_move_pct": p.get("_move"),   # over the news window, EUR-neutral
            "thesis": (y.get("thesis") if y else (th.get("business_model_thesis") or "")).strip(),
            "future_thesis": (th.get("future_thesis") or "").strip(),
            # theses.yaml carries STRUCTURED falsifiers with a check mode; the
            # legacy export has a single free-text field.
            "falsifiers": (y.get("falsifiers") if y else
                           ([{"id": "legacy", "check": "manual",
                              "statement": th["falsifier"]}] if th.get("falsifier") else [])),
            # Macro sensitivity map (2026-08-18). `inferred` means DRAFTED BY A
            # MODEL AND NOT YET REVIEWED, so it is filtered out here rather than
            # trusted to a prompt: an unreviewed guess must never reach a brief
            # looking like something the user wrote. Only `stated` (their own
            # words) and `confirmed` (drafted, then endorsed) survive.
            "macro_sensitivity": [
                d for d in ((y.get("macro_sensitivity") if y else None) or [])
                if d.get("basis") in ("stated", "confirmed")
            ],
            # Set when a holding deliberately has NO macro exposure. Carries the
            # reasoning, so the brief can say "nothing macro bears on this"
            # instead of inventing a channel to fill the silence.
            "macro_no_exposure": (y.get("macro_no_exposure") if y else None),
            "canon": (y.get("canon") if y else None),
            "conviction": (y.get("conviction") if y else None),
            # Only surface the legacy export's confidence/verdict when there is
            # NO theses.yaml entry. Otherwise a stale April machine-verdict
            # ("pass") leaks into a brief written against a 2026-08-17 thesis —
            # observed 2026-08-17, the model cited "verdict is pass".
            "confidence": None if y else th.get("confidence"),
            "verdict": None if y else th.get("verdict"),
            "horizon": (y.get("horizon") if y else th.get("horizon")),
            "thesis_date": (str(y.get("written")) if y else th.get("thesis_date")),
            "thesis_source": "theses.yaml" if y else ("export" if th else None),
        }
        q, lang = NEWS.get(isin, (entry["company"], "en"))
        window = args.hours
        items, err = fetch_news(q, lang, window)
        aliases = ALIASES.get(isin) or [entry["company"].lower()]
        # Widen for a large move that the normal window cannot explain. Only
        # when nothing ON TOPIC came back: a move with news already attached
        # needs no wider net, and widening it would resurface stories the
        # reader saw yesterday for no gain.
        if (not err
                and abs(entry["price_move_pct"] or 0) >= MOVE_ESCALATE_PCT
                and window < ESCALATE_HOURS
                and not [it for it in items
                         if names_company(it["headline"], aliases)
                         and not is_churn(it["headline"])]):
            wider, werr = fetch_news(q, lang, ESCALATE_HOURS)
            if not werr and wider:
                items, window = wider, ESCALATE_HOURS
        entry["news_window_hours"] = window
        # Say so in the payload rather than letting a 72h item look like a 24h
        # one. The model is told to date these explicitly.
        entry["news_window_widened"] = window != args.hours
        if err:
            gaps.append({"company": entry["company"], "isin": isin, "reason": err})
            entry["news"] = None
            entry["news_fetched"] = 0
            entry["news_offtopic"] = 0
            entry["news_churn"] = 0
        else:
            on_topic = [it for it in items if names_company(it["headline"], aliases)]
            not_churn = [it for it in on_topic if not is_churn(it["headline"], aliases)]
            # Two more genres that name the company without being about the
            # business. Counted separately from churn so the payload can say
            # which kind of noise a holding attracts.
            kept = [it for it in not_churn
                    if not is_promo(it["headline"]) and not is_local(it["headline"])]
            entry["news_fetched"] = len(items)
            entry["news_offtopic"] = len(items) - len(on_topic)
            entry["news_churn"] = len(on_topic) - len(not_churn)
            entry["news_promo_or_local"] = len(not_churn) - len(kept)
            # A company whose entire feed was off-topic has NO news. Say that
            # plainly; do not let it fall through as "nothing worth reporting".
            if items and not kept:
                if not on_topic:
                    why = "every item fetched was about another company"
                elif not not_churn:
                    why = "every item was 13F position-change churn"
                else:
                    why = ("every item was 13F churn, a retail-deal listing or a "
                           "local incident — none about the business")
                no_news.append({"company": entry["company"], "isin": isin,
                                "fetched": len(items), "reason": why})
            elif not items:
                no_news.append({"company": entry["company"], "isin": isin,
                                "fetched": 0,
                                "reason": f"feed returned nothing in {window}h"})
            # seen_before is stamped BEFORE clustering, not after:
            # dedupe_stories returns copies, so a flag set on `kept` afterwards
            # would never reach the items that actually go in the payload.
            # Every item is recorded as seen, including the retellings that get
            # merged away, so tomorrow does not treat them as new.
            for it in kept:
                # Windows overlap, so yesterday's story is served again today.
                # first-seen STRICTLY BEFORE today, so a manual run earlier the
                # same day cannot make the scheduled run think it is stale.
                k = "h:" + story_key(it["headline"])
                first = min([d for d in (seen.get(it["url"]), seen.get(k)) if d],
                            default=None)
                it["seen_before"] = bool(first and first < today_iso)
                it["first_seen"] = first or today_iso
                seen.setdefault(it["url"], today_iso)
                seen.setdefault(k, today_iso)
            # Collapse retellings BEFORE the cap, then rank rather than taking
            # the newest 8. Both were the same bug: the cap was being spent on
            # whatever published most recently, which is churn and syndication
            # rather than news.
            clustered = dedupe_stories(kept, aliases)
            entry["news_distinct_stories"] = len(clustered)
            entry["news_retellings_merged"] = len(kept) - len(clustered)
            chosen = rank_news(clustered, thesis_terms(entry), aliases, cap=8)
            for it in chosen:
                it.pop("_cluster", None)
            entry["news"] = chosen
        companies.append(entry)
        time.sleep(1.0)

    if not args.no_write_seen:
        save_seen(home, seen, today)

    # The passive sleeve. Deliberately NO news and NO thesis: an index fund has
    # no business to report on, and a headline digest is the thing this tool
    # exists not to be. It carries weight and macro exposure only — which is
    # what makes "30-year yields hit 5.33%" land on a large index position
    # instead of floating free, as it did on 2026-08-19.
    etf_macro = load_etf_macro(home)
    etfs = []
    for p in positions:
        if p.get("sleeve") == "stock":
            continue
        e = etf_macro.get(p["isin"]) or {}
        etfs.append({
            "name": p.get("name") or p["ticker"],
            "isin": p["isin"], "ticker": p["ticker"], "sleeve": p.get("sleeve"),
            "broker": p.get("broker"),
            "portfolio_weight_pct": (round(p["_mkt"] / total_book * 100, 1)
                                     if p.get("_mkt") and total_book else None),
            "price_move_pct": p.get("_move"),
            "macro_sensitivity": [d for d in (e.get("drivers") or [])
                                  if d.get("basis") in ("stated", "confirmed")],
            "note": e.get("note"),
        })
    etfs.sort(key=lambda e: -(e["portfolio_weight_pct"] or 0))

    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "window_hours": args.hours,
        "universe": "held stock picks (positions.csv, sleeve=stock)",
        "news_source": "Google News RSS (keyless)",
        "companies": sorted(companies,
                            key=lambda e: -(e["sleeve_weight_pct"] or e["cost_weight_pct"])),
        "etfs": etfs,
        "warnings": _WARNINGS,
        "news_gaps": gaps,
        "no_company_news": no_news,
        "counts": {
            "companies": len(companies),
            "with_news": sum(1 for e in companies if e.get("news")),
            "offtopic_dropped": sum(e.get("news_offtopic") or 0 for e in companies),
            "churn_dropped": sum(e.get("news_churn") or 0 for e in companies),
            "seen_before": sum(1 for e in companies for n in (e.get("news") or [])
                               if n.get("seen_before")),
            "with_thesis": sum(1 for e in companies if e["thesis"]),
            "with_falsifier": sum(1 for e in companies if e["falsifiers"]),
            "macro_drivers": sum(len(e["macro_sensitivity"]) for e in companies),
            "etfs": len(etfs),
            "etf_weight_pct": round(sum(e["portfolio_weight_pct"] or 0 for e in etfs), 1),
            "etf_macro_drivers": sum(len(e["macro_sensitivity"]) for e in etfs),
            "with_no_macro_exposure": sum(1 for e in companies if e["macro_no_exposure"]),
            "auto_checkable_falsifiers": sum(
                1 for e in companies for f in (e["falsifiers"] or [])
                if f.get("check") == "auto"),
        },
    }
    # ƒ POSITIONS TABLE. Written here rather than by a separate script because
    # the prices and FX are already fetched — a second script would double the
    # network calls to print the same numbers.
    #
    # Why it exists: until 2026-08-21 a price move reached the reader only if it
    # carried news or tripped the MOVE RULE, so a holding that fell 8% quietly
    # never appeared at all. The owner was still checking two broker accounts
    # every morning to find out what the book was worth. This is that check.
    if args.table_out:
        try:
            cash = load_csv(os.path.join(home, "cash.csv"))
            cash_eur = sum(float(c.get("amount_eur") or 0) for c in cash)
            md, tbl_warnings = positions_table(
                positions, cash_eur, datetime.date.today())
            _WARNINGS.extend(tbl_warnings)
            with open(args.table_out, "w") as fh:
                fh.write(md)
        except Exception as e:                       # noqa: BLE001
            _WARNINGS.append(f"positions table failed ({type(e).__name__}: {e})")

    # default=str: theses.yaml carries real dates (falsifier baselines), and
    # PyYAML returns datetime.date objects that json cannot serialise. Without
    # this the dump crashes PART WAY THROUGH, emitting ~40KB of truncated,
    # invalid JSON — which looks like output but parses as nothing.
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
