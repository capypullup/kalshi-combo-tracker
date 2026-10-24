"""The Alerts tool on made-up bets, for tests/test_alert_settings.py (Streamlit's AppTest runs it)."""

import streamlit as st

from combo_tracker import alerts_view
from alert_bets import bets
from combo_tracker.ghfile import MemoryFile

ss = st.session_state


store = ss.setdefault("_store", MemoryFile())
alerts_view.render(bets(), store, can_save=ss.get("_can_save", True), save_note=ss.get("_note"))
