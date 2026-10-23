"""Is anyone looking at the app? Background workers poll Kalshi only while someone is.

Every open page pings seen() (app.py runs a small heartbeat each minute while a browser
tab is connected). After IDLE_SECONDS without a ping the collector, bettor tracker and
RFQ feed stop calling Kalshi, and the next ping wakes them. Only the app turns this on:
the alert and tail bots never go idle.
"""

from __future__ import annotations

import os
import threading
import time

IDLE_SECONDS = float(os.environ.get("IDLE_MINUTES", "5")) * 60

_last_seen = time.monotonic()
_seen_event = threading.Event()
_tracking = False


def seen() -> None:
    """A page is open. The first call also turns idle pausing on."""
    global _last_seen, _tracking
    _last_seen = time.monotonic()
    _tracking = True
    _seen_event.set()


def active() -> bool:
    return not _tracking or time.monotonic() - _last_seen < IDLE_SECONDS


def wait_active(stop: threading.Event, step: float = 5.0) -> None:
    """Block until someone opens the app (or stop is set)."""
    while not active() and not stop.is_set():
        _seen_event.clear()
        _seen_event.wait(step)
