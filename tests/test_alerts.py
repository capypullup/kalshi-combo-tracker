from datetime import datetime, timezone

from combo_tracker import alerts
from combo_tracker.engine import build_bets, score_bet

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
LEGS = [{"event_ticker": "KXNBAGAME-26OCT21ATLORL", "market_ticker": "A", "side": "yes"},
        {"event_ticker": "KXNFLGAME-26OCT21SEADEN", "market_ticker": "B", "side": "yes"}]


def book(bid, ask, size=500):
    return {"yes_dollars": [[f"{bid:.4f}", f"{size:.2f}"]], "no_dollars": [[f"{1 - ask:.4f}", f"{size:.2f}"]]}


def row(ticker="KXMVECOMBO-1", price=0.20, count=3000, legs=None):
    trade = {"trade_id": ticker, "ticker": ticker, "taker_side": "yes", "taker_outcome_side": "yes",
             "yes_price_dollars": f"{price:.4f}", "no_price_dollars": f"{1 - price:.4f}",
             "count_fp": f"{count:.2f}", "created_time": "2026-10-21T11:55:00Z"}
    bet = build_bets([trade])[0]
    books = {"A": book(0.49, 0.51), "B": book(0.59, 0.61)}  # fair mid 0.5 * 0.6 = 0.30
    markets = {"A": {"yes_sub_title": "Hawks"}, "B": {"yes_sub_title": "Seahawks"}}
    if legs:  # the first two legs priced like A and B, the rest at 0.99, so the fair price stays about 0.30
        for i, l in enumerate(legs):
            books[l["market_ticker"]] = book(0.49, 0.51) if i == 0 else book(0.59, 0.61) if i == 1 else book(0.985, 0.995)
            markets[l["market_ticker"]] = {"yes_sub_title": l["market_ticker"]}
    return score_bet(bet, {"mve_selected_legs": legs or LEGS}, markets, books, now=NOW)


def leg(event, market):
    return {"event_ticker": event, "market_ticker": market, "side": "yes"}


TIERS = alerts.parse_tiers("1000:10,500:20")
OFF = alerts.parse_tiers("off")


def test_tiers():
    assert TIERS == [(500, 0.20), (1000, 0.10)]
    assert OFF == [] and alerts.min_ev_for(5000, OFF) is None
    assert alerts.parse_tiers(alerts.PREGAME_TIERS)[0] == (50, 1.0)
    assert alerts.min_ev_for(499, TIERS) is None
    assert alerts.min_ev_for(700, TIERS) == 0.20
    assert alerts.min_ev_for(1000, TIERS) == 0.10


def test_pick_filters_on_size_tier_ev_thin_and_sent():
    good = row()  # $600 at 0.20 vs fair 0.30: +50%
    small = row("KXMVECOMBO-2", count=1000)  # $200
    bad = row("KXMVECOMBO-3", price=0.29)  # +3%
    mid_size = row("KXMVECOMBO-4", price=0.26)  # $780 at +15%: under the $500 tier's 20%
    big = row("KXMVECOMBO-5", price=0.26, count=4000)  # $1,040 at +15%: clears the $1k tier's 10%
    assert good["ev_pct_mid"] > 0.49
    assert [r["ticker"] for r in alerts.pick([small, bad, mid_size, big, good], TIERS, OFF, set())] == ["KXMVECOMBO-1", "KXMVECOMBO-5"]
    assert alerts.pick([good], TIERS, OFF, {alerts.key(good)}) == []
    good["leg_detail"][0]["spread"] = 0.05
    assert alerts.pick([good], TIERS, OFF, set()) == []
    assert alerts.pick([good], TIERS, OFF, set(), skip_thin=False) == [good]


def test_pick_leaves_out_crypto_legs():
    good = row()
    assert alerts.pick([good], TIERS, OFF, set()) == [good]
    good["leg_detail"][1]["ticker"] = "KXBTCD-26OCT2117-T108000"
    assert alerts.pick([good], TIERS, OFF, set()) == []
    assert alerts.pick([good], TIERS, OFF, set(), skip_series=()) == [good]


def test_pick_leaves_out_same_game_legs():
    same = row(legs=[leg("KXNBAGAME-26OCT21ATLORL", "KXNBAGAME-26OCT21ATLORL-ATL"),
                     leg("KXNBAPTS-26OCT21ATLORL", "KXNBAPTS-26OCT21ATLORL-ATLTYOUNG11-25")])
    assert same["same_game_legs"] == 1 and same["ev_pct_mid"] > 0.4
    assert alerts.pick([same], TIERS, OFF, set()) == []
    assert alerts.pick([same], TIERS, OFF, set(), skip_same_game=False) == [same]


def test_pick_leaves_out_combos_over_five_legs_or_two_leagues():
    nba = [leg(f"KXNBAGAME-26OCT21G{i}", f"KXNBAGAME-26OCT21G{i}-H{i}") for i in range(6)]
    five, six = row("KXMVECOMBO-5", legs=nba[:5]), row("KXMVECOMBO-6", legs=nba)  # NBA only
    two = row("KXMVECOMBO-2", legs=nba[:1] + [leg("KXNFLGAME-26OCT21SEADEN", "KXNFLGAME-26OCT21SEADEN-SEA")])
    three = row("KXMVECOMBO-3", legs=nba[:1] + [
        leg("KXNFLGAME-26OCT21SEADEN", "KXNFLGAME-26OCT21SEADEN-SEA"), leg("KXNHLGAME-26OCT21BOSNYR", "KXNHLGAME-26OCT21BOSNYR-BOS")])
    bets = [five, six, two, three]
    assert [r["legs"] for r in bets] == [5, 6, 2, 3]
    assert [alerts.leagues(r) for r in bets] == [1, 1, 2, 3]
    assert all(r["ev_pct_mid"] > 0.4 and not r["same_game_legs"] for r in bets)
    picked = lambda **kw: {r["ticker"] for r in alerts.pick(bets, TIERS, OFF, set(), **kw)}  # noqa: E731
    assert picked() == {"KXMVECOMBO-5", "KXMVECOMBO-2"}
    assert picked(max_legs=None, max_leagues=None) == {r["ticker"] for r in bets}
    assert picked(max_legs=6, max_leagues=3) == {r["ticker"] for r in bets}
    assert picked(max_legs=None) == {"KXMVECOMBO-5", "KXMVECOMBO-6", "KXMVECOMBO-2"}


def test_live_bets_use_the_live_tiers():
    good = row()  # $600 at +50%, legs' games not on the schedule and settling far off: pregame
    live = alerts.parse_tiers("500:40")
    assert alerts.pick([good], OFF, live, set()) == []
    good["leg_detail"][0]["hours_to_start"] = -0.5  # game started
    assert alerts.pick([good], OFF, live, set()) == [good]
    assert alerts.pick([good], TIERS, OFF, set()) == []
    assert alerts.title(good).startswith("LIVE +50% EV")


def test_summary_has_mid_and_crx_ev_multiple_and_units():
    s = alerts.summary(row())
    assert s.startswith("MID +50% · 3.33x · ") and " | CRX +" in s and s.endswith("u")


def test_message_has_card_link_and_louder_ping_for_bigger_edges():
    msg = alerts.message(row(), "my-topic", app_url="https://capyev.streamlit.app/")
    assert msg["topic"] == "my-topic"
    assert msg["title"] == "PREGAME +50% EV · $600 YES 2-leg combo"
    assert msg["click"] == "https://kalshi.com/combos?marketTicker=KXMVECOMBO-1"
    assert "$600 @ 5.0x (+400) → $3,000" in msg["message"]
    assert "YES Hawks (KXNBAGAME-26OCT21ATLORL)" in msg["message"]
    assert [a["label"] for a in msg["actions"]] == ["Bet in Kalshi", "Quote it"]
    assert msg["actions"][1]["url"] == "https://capyev.streamlit.app/quote?t=KXMVECOMBO-1&side=yes&p=0.2000&n=3000"
    assert alerts.tier(0.12)[0] < alerts.tier(0.6)[0] < alerts.tier(8.0)[0]


def test_telegram_message_bolds_title_and_adds_buttons():
    msg = alerts.telegram_message(row(price=0.18), "42", app_url="https://capyev.streamlit.app")
    head = "\n".join(msg["text"].split("\n")[:2])
    assert head.startswith("🔥 PREGAME +67% EV · $540 YES 2-leg combo\nMID +67% · ")
    assert msg["entities"][0]["length"] == len(head) + 1  # the emoji is two UTF-16 units
    assert [b["text"] for b in msg["reply_markup"]["inline_keyboard"][0]] == ["Bet in Kalshi", "Quote it"]


def test_telegram_finds_the_chat_that_messaged_the_bot():
    posted = []

    class Resp:
        status_code = 200

        def __init__(self, data=None):
            self.data = data

        def json(self):
            return self.data

        def raise_for_status(self):
            pass

    class Session:
        def get(self, url, timeout):
            assert url == "https://api.telegram.org/botTOKEN/getUpdates"
            return Resp({"result": [{"message": {"chat": {"id": 7}}}, {"message": {"chat": {"id": 99}}}]})

        def post(self, url, json, timeout):
            posted.append(json)
            return Resp()

    bot = alerts.Telegram("TOKEN", session=Session())
    assert bot.chat_id == "99" and "99" in posted[0]["text"]
    bot.send(row())
    assert posted[1]["chat_id"] == "99" and "YES Hawks" in posted[1]["text"]
