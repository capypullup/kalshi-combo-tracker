"""Kalshi combo EV tracker."""

import time

# app.py compares this with the files' modification times to spot a deploy that
# updated the code without re-importing it.
IMPORTED_AT = time.time()
