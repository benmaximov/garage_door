"""
counter.py - Open-count persistence for garage.py

Provides:
  open_count         - module-level integer, current door-open count
  load_count()       - read persisted value from disk
  save_count()       - write current open_count to disk (called by db_log.flush)
  increment()        - increment open_count and return new value
"""

# ── Configuration ─────────────────────────────────────────────────────────────
COUNTER_FILE  = "/root/garage_count.txt"

# ── State ─────────────────────────────────────────────────────────────────────
open_count = 0   # loaded / updated at runtime


def load_count():
    """Read persisted count from disk. Returns 0 on any error."""
    global open_count
    try:
        with open(COUNTER_FILE, "r") as f:
            open_count = int(f.read().strip())
    except Exception:
        open_count = 0
    return open_count


def save_count():
    """Write current open_count to disk."""
    try:
        with open(COUNTER_FILE, "w") as f:
            f.write(str(open_count))
    except Exception as e:
        print("[counter] save failed: %s" % e)


def increment():
    """Increment open_count by 1 and return the new value."""
    global open_count
    open_count += 1
    return open_count
