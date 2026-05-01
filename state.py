"""
state.py - Shared relay state for garage.py and api.py.

Both modules import this directly, eliminating any circular dependency.
"""

import threading

relay_lock         = threading.Lock()
relay_timer        = None          # threading.Timer; set/cleared by garage.py
relay_release_time = 0.0           # time.time() when relay will release
relay_activated    = False         # True while relay is held closed
api_hold_active    = False         # True while /hold API is active; cleared by /release

# ── PA6 ISR suppression for /optocheck ───────────────────────────────────────
# garage.py's PA6 edge callback returns early when suppress_pa6_isr == True.
# api.py's /optocheck sets this True while it drives PA3 for the test, then
# resets it after re-sampling the real PA6 level.
suppress_pa6_isr   = False         # True = PA6 ISR ignores all edges
# Callback registered by garage.py at startup. Called by api.optocheck()
# after the check window ends to: cancel any pending PA6 debounce timer and
# resync pa6_is_high to the true physical level. Signature: resync_pa6() -> None
resync_pa6         = None
