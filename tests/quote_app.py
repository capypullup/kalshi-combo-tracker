"""The Quote page on demo data, for tests/test_quote_page.py (Streamlit's AppTest runs it)."""

import streamlit as st

from combo_tracker import quote_view
from combo_tracker.demo import DemoClient

ss = st.session_state
client = ss.setdefault("_client", DemoClient())
books = type(client).get_orderbooks


def get_orderbooks(tickers):
    if ss.get("_books_down"):
        raise ConnectionError("Kalshi isn't answering")
    return books(client, tickers)


client.get_orderbooks = get_orderbooks
quote_view.render(client, [], demo=True, pool=lambda: (client.leg_markets, client.events), pin=ss.get("_pin"))
