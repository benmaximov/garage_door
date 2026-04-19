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
