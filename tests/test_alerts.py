from datetime import datetime, timezone

from combo_tracker import alerts
from combo_tracker.engine import build_bets, score_bet

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
LEGS = [{"event_ticker": "KXNBAGAME-26OCT21ATLORL", "market_ticker": "A", "side": "yes"},
        {"event_ticker": "KXNFLGAME-26OCT21SEADEN", "market_ticker": "B", "side": "yes"}]


def book(bid, ask, size=500):
    return {"yes_dollars": [[f"{bid:.4f}", f"{size:.2f}"]], "no_dollars": [[f"{1 - ask:.4f}", f"{size:.2f}"]]}


def row(ticker="KXMVECOMBO-1", price=0.20, count=3000):
    trade = {"trade_id": ticker, "ticker": ticker, "taker_side": "yes", "taker_outcome_side": "yes",
             "yes_price_dollars": f"{price:.4f}", "no_price_dollars": f"{1 - price:.4f}",
             "count_fp": f"{count:.2f}", "created_time": "2026-10-21T11:55:00Z"}
    bet = build_bets([trade])[0]
    books = {"A": book(0.49, 0.51), "B": book(0.59, 0.61)}  # fair mid 0.5 * 0.6 = 0.30
    markets = {"A": {"yes_sub_title": "Hawks"}, "B": {"yes_sub_title": "Seahawks"}}
    return score_bet(bet, {"mve_selected_legs": LEGS}, markets, books, now=NOW)


TIERS = alerts.parse_tiers("1000:10,500:20")


def test_tiers():
    assert TIERS == [(500, 0.20), (1000, 0.10)]
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
    assert [r["ticker"] for r in alerts.pick([small, bad, mid_size, big, good], TIERS, set())] == ["KXMVECOMBO-1", "KXMVECOMBO-5"]
    assert alerts.pick([good], TIERS, {alerts.key(good)}) == []
    good["leg_detail"][0]["spread"] = 0.05
    assert alerts.pick([good], TIERS, set()) == []
    assert alerts.pick([good], TIERS, set(), skip_thin=False) == [good]


def test_message_has_card_link_and_louder_ping_for_bigger_edges():
    msg = alerts.message(row(), "my-topic", app_url="https://capyev.streamlit.app/")
    assert msg["topic"] == "my-topic"
    assert msg["title"] == "+50% EV · $600 YES 2-leg combo"
    assert msg["click"] == "https://kalshi.com/markets_by_ticker/KXMVECOMBO-1"
    assert "$600 @ 5.0x (+400) → $3,000" in msg["message"]
    assert "YES Hawks (KXNBAGAME-26OCT21ATLORL)" in msg["message"]
    assert [a["label"] for a in msg["actions"]] == ["Bet in Kalshi", "Quote it"]
    assert msg["actions"][1]["url"] == "https://capyev.streamlit.app/quote?t=KXMVECOMBO-1&side=yes&p=0.2000&n=3000"
    assert alerts.tier(0.12)[0] < alerts.tier(0.6)[0] < alerts.tier(8.0)[0]


def test_telegram_message_bolds_title_and_adds_buttons():
    msg = alerts.telegram_message(row(price=0.18), "42", app_url="https://capyev.streamlit.app")
    head = msg["text"].split("\n")[0]
    assert head == "🔥 +67% EV · $540 YES 2-leg combo"
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
