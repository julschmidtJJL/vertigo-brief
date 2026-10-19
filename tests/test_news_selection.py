"""What reaches the model, and what must never be dropped on the way.

A live run cannot test this — the feed changes hourly — so every case here is
built from a real headline captured from Google News on 2026-08-26. Company,
fund, person and place names are replaced with invented ones; the SHAPE of
each headline, which is what the filters read, is kept verbatim. The labelled
corpus that session measured had 128 on-topic items across nine holdings; of
one retailer's 80, eleven were 13F churn, eighteen were retail-deal listings
and twenty were local incidents. The eight the model actually saw were drawn
from that majority, and the brief filed the retailer under "nothing worth
reporting" the same morning.

The asymmetry that shapes these tests: dropping a real story is expensive and
silent, keeping a junk one is cheap and visible. So the DROP cases are
illustrative and the KEEP cases are the contract.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import market_brief_gather as g  # noqa: E402

MKT = ["marketon", "value club", "valueclub", "flipmart"]
ACU = ["andes group", "andes grupo", "andes copper", "altoro"]
ECO = ["llanos", "refinor", "centra"]


class TestChurnIsAboutSomebodyElsesPortfolio:
    """The holding is the OBJECT of the trade. That is the whole rule."""

    @pytest.mark.parametrize("headline", [
        "681,087 Shares in Marketon Inc. $MKT Purchased by Elm Capital LLC",
        "Oakline Wealth Management Takes Position in Marketon Inc. $MKT",
        "Brookside Advisors LLC Takes $1.33 Million Position in Marketon Inc. $MKT",
        "Summit Ridge Asset Management Inc. Purchases New Stake in Marketon Inc. $MKT",
        "Cobalt Partners LLC Buys New Shares in Marketon Inc. $MKT",
        "60,700 Shares in Marketon Inc. $MKT Acquired by Larch Capital Management LLC",
        "Corwin Family Office LLC Invests $20.76 Million in Marketon Inc. $MKT",
    ])
    def test_the_thirteen_f_generator_is_caught(self, headline):
        assert g.is_churn(headline, MKT), headline

    @pytest.mark.parametrize("headline", [
        "Hollis & Vane Advisors LLC Purchases New Shares in Andes Copper Corporation $ACU",
        "Haven Advisors LLC Makes New Investment in Andes Copper Corporation $ACU",
        "Lakeshore Mutual Wealth Management Co. Purchases Shares of 5,528 Andes Copper Corporation $ACU",
        "Harbor Investment CORP Makes New $6.55 Million Investment in Andes Copper Corporation $ACU",
        "61,305 Shares in Andes Copper Corporation $ACU Purchased by Granite Bank",
    ])
    def test_it_is_caught_under_the_subsidiary_name_too(self, headline):
        assert g.is_churn(headline, ACU), headline

    @pytest.mark.parametrize("headline", [
        # Every one of these contains a buying verb, and several contain the
        # word "stake". They are the company acting, not being traded, and this
        # is the most important company news in the book this week.
        "Llanos closes US$1.2 billion deal to acquire 51% controlling stake in Brazil's Costa Energia",
        "Llanos Takes Control of Costa Energia in $1.2B Brazil Growth Bet",
        "LLANOS: Acquisition of 51% of Costa Energia for US$1.2B boosts reserves and Brazil market presence",
    ])
    def test_the_company_doing_the_acquiring_survives(self, headline):
        assert not g.is_churn(headline, ECO), headline

    def test_a_capex_headline_is_not_churn(self):
        """`invests $N million in` is churn only when the holding is what is
        being bought. A company investing in its own operations is news."""
        assert not g.is_churn(
            "Kestrel invests $500 million in Congo copper mine expansion", ["kestrel"])
        assert g.is_churn(
            "Polaris Investment Solutions Invests $7.01 Million in Kestrel Mining", ["kestrel"])

    def test_the_legacy_single_argument_call_still_works(self):
        assert g.is_churn("Marketon Short Interest Update")
        assert not g.is_churn("Oakline Wealth Management Takes Position in Marketon Inc.")


class TestTheTwoGenresThatNameTheCompanyWithoutBeingAboutIt:

    @pytest.mark.parametrize("headline", [
        "Marketon’s top-rated $110 boho comforter set is just $30 during a Flash deal",
        "Marketon is selling $160 noise-canceling headphones for 88% off",
        "Marketon’s $500 velvet sectional sofa is on sale for $199",
        "Marketon’s bestselling 2-seat patio swing glider is on sale for $70",
        "The Best Marketon Deals to Shop Before Labor Day Officially Begins",
    ])
    def test_retail_deal_listings_are_dropped(self, headline):
        assert g.is_promo(headline), headline

    @pytest.mark.parametrize("headline", [
        "Teen found stabbed after police break up group fight in Millbrook Marketon parking lot",
        "Ashford man jailed after reported robbery in Marketon parking lot",
        "Greenfield man accused of video voyeurism at Riverton Marketon",
        "Eastport zoning board denies Marketon’s proposal for Westvale delivery hub",
        "Fairview man killed in Lakeside Marketon parking lot crash",
    ])
    def test_local_incidents_are_dropped(self, headline):
        assert g.is_local(headline), headline

    @pytest.mark.parametrize("headline", [
        # The bare word "deal" must never be a promo trigger.
        "Llanos closes US$1.2 billion deal to acquire 51% controlling stake in Costa Energia",
        # A regulatory probe is news; a shoplifting arrest is not.
        "Marketon faces antitrust investigation over supplier pricing",
        "Marketon sued by FTC over subscription cancellations",
        # Real business stories that mention money or a product launch.
        "Jane Doe: The real reason why Marketon is slashing prices by $2.9 billion",
        "Marketon, Prairie Foods, autonomous trucking partner announces $200M in funding",
        "Marketon launches affordable new private fashion label described as ‘boho’",
        "Marketon Falls as 37-Times Valuation Meets Weak Traffic",
        "Marketon (MKT) Stock Looks Fully Priced After Its 127% Run",
    ])
    def test_real_news_survives_both_genres(self, headline):
        assert not g.is_promo(headline), f"promo false positive: {headline}"
        assert not g.is_local(headline), f"local false positive: {headline}"


class TestRetellingsCollapse:

    def test_one_acquisition_told_four_ways_becomes_one_item(self):
        items = [{"headline": h} for h in [
            "LLANOS: Acquisition of 51% of Costa for USD 1.2B expands growth and diversification in Brazil",
            "LLANOS: $1.2B acquisition of 51% in Costa Energia boosts scale, diversification, and growth in Brazil",
            "LLANOS: Acquisition of 51% of Costa Energia for US$1.2B boosts reserves and Brazil market presence",
            "LLANOS: $1.2B acquisition of 51% in Costa Energia boosts scale, reserves, and growth in Brazil",
        ]]
        out = g.dedupe_stories(items, ECO)
        assert len(out) == 1
        assert out[0]["retellings"] == 3

    def test_a_cluster_joins_through_its_members_not_just_its_first(self):
        """Headlines 1 and 3 above share only 0.43 directly, below the 0.6
        threshold; they land in one cluster because each clears it against the
        headlines in between. Comparing only against a cluster's first member
        would split this acquisition into three items."""
        a = "LLANOS: Acquisition of 51% of Costa for USD 1.2B expands growth and diversification in Brazil"
        c = "LLANOS: Acquisition of 51% of Costa Energia for US$1.2B boosts reserves and Brazil market presence"
        assert not g.same_story(a, c, ECO)
        assert len(g.dedupe_stories([{"headline": a}, {"headline": c}], ECO)) == 2

    def test_a_short_headline_cannot_bridge_two_stories(self):
        """The bug this guard exists for. "Kestrel Mining 3999 HK" has three
        content words; at a 0.5 threshold with no length floor it matched both
        the price-target story and the profit story and merged them into one."""
        items = [{"headline": h} for h in [
            "Ridgeway Adjusts Kestrel Mining Group's Price Target to 46.30 Yuan From 45.30 Yuan, Keeps at Buy",
            "Kestrel Mining 3999 HK",
            "Kestrel Mining's Profit Climbs 68% in H1",
        ]]
        out = g.dedupe_stories(items, ["kestrel", "kestrel mining"])
        assert len(out) == 3, "a price target and an earnings release are not one story"

    def test_different_stories_about_one_holding_stay_separate(self):
        items = [{"headline": h} for h in [
            "Kestrel’s 2026 Copper Target Under Pressure After Congo Mine Flood",
            "Kestrel Mining’s First-Half Profit Jumped On Gold, Copper, And Lithium",
            "Ridgeway Adjusts Kestrel Mining Group's Price Target to HK$50.20 From HK$49.10, Keeps at Buy",
        ]]
        assert len(g.dedupe_stories(items, ["kestrel", "kestrel mining"])) == 3

    def test_stemming_joins_jumps_and_jumped(self):
        assert g.same_story(
            "Kestrel Mining's First-Half Profit Jumps 68% as Higher Metal Prices Lift Margins",
            "Kestrel Mining First-Half Profit Jumped 68% as Higher Metal Price Lifts Margin",
            ["kestrel", "kestrel mining"])


class TestRankingReplacesRecency:

    def test_the_thesis_outranks_the_clock(self):
        """The failure this whole change exists for: on 2026-08-26 the eight
        newest Marketon items were six churn plus two product listings, while
        the valuation story that touches `mkt-growth-multiple-on-thin-margin`
        sat below the cut."""
        items = [
            {"headline": "Marketon gets Elm River OK to get bigger, expand groceries", "at": "2026-08-26T09:15"},
            {"headline": "Marketon opens 100th company-owned EV charging site", "at": "2026-08-26T09:10"},
            {"headline": "Marketon Falls as 37-Times Valuation Meets Weak Traffic", "at": "2026-08-26T01:00"},
        ]
        terms = g._content(
            "If trailing P/E exceeds 45 while net margin stays below 4%, I am paying "
            "growth pricing for a 3%-margin retailer")
        top = g.rank_news(items, terms, MKT, cap=1)
        assert top[0]["headline"].startswith("Marketon Falls as 37-Times Valuation")

    def test_the_cap_spreads_across_stories_not_angles(self):
        """Four tellings of one payments story plus two other stories: with a
        cap of 3 the other two must both get in."""
        items = [{"headline": h, "at": "2026-08-26T09:00"} for h in [
            "Marketon launches Tap to Pay at Marketon stores and Value Club locations",
            "Marketon launches Tap to Pay at Marketon stores and Value Club sites",
            "Marketon launches Tap to Pay across Marketon stores and Value Club locations",
            "Marketon Falls as 37-Times Valuation Meets Weak Traffic",
            "Kestrel flood halts copper output at Marketon supplier plant",
        ]]
        got = [x["headline"] for x in g.rank_news(items, set(), MKT, cap=3)]
        assert sum(1 for h in got if "Tap to Pay" in h) == 1, got
        assert any("37-Times Valuation" in h for h in got), got
        assert any("flood halts copper" in h for h in got), got

    def test_it_never_returns_more_than_the_cap_or_invents_items(self):
        items = [{"headline": f"Marketon story number {i} about margin", "at": "2026-08-26T09:00"}
                 for i in range(20)]
        out = g.rank_news(items, set(), MKT, cap=8)
        assert len(out) == 8
        assert len({id(x) for x in out}) == 8
        assert all(x in items for x in out)

    def test_a_short_pool_is_returned_whole(self):
        items = [{"headline": "Lowland Grocers N.V.: A good level to buy", "at": "2026-08-26T09:00"}]
        assert len(g.rank_news(items, set(), ["lowland"], cap=8)) == 1


class TestTheCorpusItselfDoesNotRegress:
    """Guards the measured numbers, so a later pattern edit that quietly starts
    dropping real news fails here rather than in a Wednesday brief."""

    REAL_NEWS = [
        "Jane Doe: The real reason why Marketon is slashing prices by $2.9 billion",
        "Marketon Stock Tops Defensive Consumer Picks as Tariff Pressure Builds",
        "Marketon (MKT) Stock Looks Fully Priced After Its 127% Run",
        "The Retail Model Is Changing. Here's What Investors Need to Know About What's Driving Growth at Marketon and Northstar.",
        "Marketon Stock Is More Expensive Than Chipcore Amid Earnings Miss",
        "Riverbend Clothing Share Doubles as Marketon Loses Ground",
        "Marketon Falls as 37-Times Valuation Meets Weak Traffic",
        "Ridgeway Securities Adjusts Price Target on Marketon to $113 From $132",
        "Marketon says its AI assistant, Buddy, is leading to customers spending 40% more",
        "Marketon communications chief to depart after 3 years",
        "What Could Marketon (MKT) Gain From Closing In On India’s Quick Commerce Leaders?",
        "Marketon, Prairie Foods, autonomous trucking partner announces $200M in funding",
    ]

    @pytest.mark.parametrize("headline", REAL_NEWS)
    def test_no_filter_touches_real_retailer_news(self, headline):
        assert not g.is_churn(headline, MKT)
        assert not g.is_promo(headline)
        assert not g.is_local(headline)
