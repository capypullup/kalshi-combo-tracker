"""The Quote page end to end on demo data: betting, side switches, sizing and the PIN."""

from pathlib import Path

import pytest
import requests
from streamlit.testing.v1 import AppTest

from combo_tracker import quote_view
from combo_tracker.demo import DemoClient
from combo_tracker.quote import DemoRfqDesk

APP = str(Path(__file__).with_name("quote_app.py"))


def combo(n=0):
    t = DemoClient().trades[n]
    side = t["taker_outcome_side"]
    return t["ticker"], side, float(t["yes_price_dollars"] if side == "yes" else t["no_price_dollars"])


def page(n=0, pin=None):
    at = AppTest.from_file(APP, default_timeout=60)
    ticker, side, price = combo(n)
    at.query_params.update(t=ticker, side=side, p=f"{price:.4f}")
    if pin is not None:
        at.session_state["_pin"] = pin
    return at.run(), ticker, side


def makers_answer(at):
    """Age the demo RFQs past the makers' lag, so their quotes are in."""
    desk = at.session_state["q_demo_desk"]
    for rid, (t, d, t0) in list(desk.open.items()):
        desk.open[rid] = (t, d, t0 - 10)
    return at.run()


def bet_button(at):
    return next(b for b in at.button if b.label.startswith("Bet $"))


def test_bet_with_a_dropped_connection_says_it_may_have_gone_through(monkeypatch):
    at, ticker, side = page()
    makers_answer(at)
    bet_button(at).click().run()
    assert at.button(key="q_yes").label == "Confirm bet"

    def lost(self, rfq_id, quote_id, side):
        raise requests.ConnectionError("Connection aborted")
    monkeypatch.setattr(DemoRfqDesk, "accept", lost)
    monkeypatch.setattr(quote_view, "ACCEPT_WAIT", 1)
    at.button(key="q_yes").click().run()
    assert not at.exception
    assert "q_confirm" not in at.session_state and not [b for b in at.button if b.key == "q_yes"]
    assert any("may have gone through" in e.value for e in at.error)
    assert all(r["closed"] for r in at.session_state["q_rfqs"].values())  # no second Bet on it


def test_bet_fills_and_its_result_stays_with_its_combo():
    at, ticker, side = page()
    makers_answer(at)
    bet_button(at).click().run()
    at.button(key="q_yes").click().run()
    assert not at.exception and any(s.value.startswith("Filled:") for s in at.success)
    other, oside, oprice = combo(next(i for i in range(1, 50) if combo(i)[0] != ticker))
    at.query_params.update(t=other, side=oside, p=f"{oprice:.4f}")
    at.run()
    assert not at.exception and not at.success  # the last combo's "Filled" isn't shown under this one


def test_a_failed_check_after_a_side_switch_shows_no_quotes():
    at, ticker, side = page()
    assert any(b.label.startswith("Get quotes") for b in at.button)
    at.session_state["_books_down"] = True
    at.segmented_control[0].set_value("no" if side == "yes" else "yes").run()
    assert any("Couldn't check" in e.value for e in at.error)
    assert not any(b.label.startswith("Get quotes") for b in at.button)  # not priced with the other side's fair


def test_sizing_survives_runs_that_dont_draw_it():
    at, ticker, side = page()
    at.number_input(key="q_unit_w").set_value(500.0).run()
    at.session_state["_books_down"] = True  # the page stops before Sizing
    at.button[[b.label for b in at.button].index("Check")].click().run()
    at.session_state["_books_down"] = False
    at.run()
    assert at.session_state["q_unit"] == 500.0 and at.number_input(key="q_unit_w").value == 500.0


@pytest.mark.parametrize("pin", ["123456", "café boba"])
def test_pin_unlocks_and_a_new_pin_locks_again(pin):
    at, ticker, side = page(pin=pin)
    assert not any(b.label.startswith("Get quotes") for b in at.button)
    at.text_input[0].input(pin)
    at.button[[b.label for b in at.button].index("Unlock")].click().run()
    assert not at.exception and any(b.label.startswith("Get quotes") for b in at.button)
    at.session_state["_pin"] = "999999"  # the owner changes TRADE_PIN
    at.run()
    assert not any(b.label.startswith("Get quotes") for b in at.button)


def test_a_link_with_a_stray_character_loads_once():
    at = AppTest.from_file(APP, default_timeout=60)
    ticker, side, price = combo()
    at.query_params.update(t=ticker + " ", side=side, p=f"{price:.4f}")
    at.run()
    rfqs = dict(at.session_state["q_rfqs"])
    at.button[[b.label for b in at.button].index("Check")].click().run()
    assert at.query_params["t"] == ticker and dict(at.session_state["q_rfqs"]) == rfqs  # no new RFQ per tap


def test_a_dropped_tap_during_the_bet_keeps_its_result(monkeypatch):
    """Streamlit can end a callback at a session_state call; the result was stored before the accept."""
    at, ticker, side = page()
    makers_answer(at)
    bet_button(at).click().run()
    real = DemoRfqDesk.accept
    stored = {}

    def accept(self, rfq_id, quote_id, side):
        stored["msg"] = at.session_state["q_bet"]["msg"]  # already there, before Kalshi hears about the bet
        real(self, rfq_id, quote_id, side)
    monkeypatch.setattr(DemoRfqDesk, "accept", accept)
    at.button(key="q_yes").click().run()
    assert "may have gone through" in stored["msg"] and at.session_state["q_bet"]["msg"].startswith("Filled")


def test_a_bad_link_doesnt_break_the_page():
    at = AppTest.from_file(APP, default_timeout=60)
    ticker, side, price = combo()
    at.query_params.update(t=ticker, side=side, p="25")  # cents, not dollars
    at.run()
    assert not at.exception
    at = AppTest.from_file(APP, default_timeout=60)
    at.query_params.update(t="[RE-ENTER YOUR PIN](https://evil.example)")
    at.run()
    assert not at.exception and not at.error
