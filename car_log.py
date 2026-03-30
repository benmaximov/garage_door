"""
car_log.py - In-memory ring buffer of PA6 car-pass events for the last 24 hours.

Each entry:
  {
    "ts":      "2026-03-30T07:00:00",   # ISO-8601 local time
    "peak":    true | false             # whether the event was in peak hours
  }

Usage:
  import car_log
  car_log.record(peak=True)            # call from PA6 settled handler
  entries = car_log.get()              # returns list, newest last
"""

import threading
from collections import deque
from datetime import datetime, timedelta

_WINDOW = timedelta(hours=24)
_lock   = threading.Lock()
_buf    = deque()   # entries are dicts; oldest at left, newest at right


def record(peak: bool) -> None:
    """Append a new car-pass event and evict entries older than 24 hours."""
    now   = datetime.now()
    entry = {"ts": now.strftime("%Y-%m-%dT%H:%M:%S"), "peak": peak}
    cutoff = now - _WINDOW
    with _lock:
        _buf.append(entry)
        # evict from the left while the oldest entry is outside the window
        while _buf and datetime.fromisoformat(_buf[0]["ts"]) < cutoff:
            _buf.popleft()


def get() -> list:
    """Return a copy of all entries within the last 24 hours, oldest first."""
    now    = datetime.now()
    cutoff = now - _WINDOW
    with _lock:
        # also evict stale entries on read (in case record() wasn't called recently)
        while _buf and datetime.fromisoformat(_buf[0]["ts"]) < cutoff:
            _buf.popleft()
        return list(_buf)
