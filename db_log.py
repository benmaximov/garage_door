"""
db_log.py - MySQL-backed car-pass and click event logger.

Records each PA6 car-pass event with:
  date    -> datetime the car passed
  opening -> datetime of the last relay open before this car pass
  peak    -> 1 if in peak hours, 0 otherwise

Records each button-click event in the `clicks` table with:
  date    -> datetime of the click
  opening -> datetime of the last relay open (same as date if this click
             caused the opening; the earlier open time if timer was already
             running when the click arrived)
  counter -> current open_count at time of click
  var     -> 1 = physical remote (PA0), 2 = API call

All events are buffered in memory.  A single background timer flushes both
buffers to MySQL every FLUSH_INTERVAL_S seconds (default 30 minutes).  If
the flush succeeds, the written rows are removed from the buffer.  If it
fails, they stay and are retried at the next interval.  When a buffer
exceeds MAX_BUFFER entries the oldest entry is discarded to make room.

Public API
----------
  relay_open()              -- call when relay is first activated (door held open)
  relay_closed()            -- call when relay cycle ends (clears opening timestamp)
  car_pass(peak: bool)      -- call when PA6 goes HIGH (car detected in beam)
  click_record(var: int)    -- call on PA0 HIGH edge (var=1) or API open (var=2)
  get() -> list             -- return a copy of the car-pass buffer (for /cars API)
  get_clicks() -> list      -- return a copy of the clicks buffer (for /clicks API)
  flush()                   -- save counter to disk, flush both buffers to MySQL,
                               and reschedule the 30-minute timer
"""

import threading
from collections import deque
from datetime import datetime

import counter

# ── Configuration ─────────────────────────────────────────────────────────────
_DB_HOST     = "192.168.3.5"
_DB_PORT     = 3306
_DB_USER     = "cars"
_DB_PASS     = "cars"
_DB_NAME     = "garage"
_DB_TABLE        = "cars"
_DB_TABLE_CLICKS = "clicks"

MAX_BUFFER        = 200          # max rows kept in memory; oldest discarded on overflow
FLUSH_INTERVAL_S  = 30 * 60     # flush attempt every 30 minutes

# ── Internal state ─────────────────────────────────────────────────────────────
_lock       = threading.Lock()
_buf        = deque()            # pending rows: {"date": datetime, "opening": datetime, "peak": int}
_clicks_buf = deque()            # pending rows: {"date": datetime, "opening": datetime, "counter": int, "var": int}
_open_ts    = None               # datetime of the most recent relay_open() call
_timer      = None               # periodic flush timer


# ── Public functions ───────────────────────────────────────────────────────────

def relay_open():
    """Record the timestamp of a relay activation (first open, not re-entry)."""
    global _open_ts
    with _lock:
        _open_ts = datetime.now()
        ts = _open_ts
    print("[db_log] relay opened at %s" % ts.strftime("%Y-%m-%d %H:%M:%S"))


def relay_closed():
    """Clear the opening timestamp when the relay cycle ends (door can close again)."""
    global _open_ts
    with _lock:
        _open_ts = None
    print("[db_log] relay closed - opening timestamp cleared")


def car_pass(peak):
    """Record a car-pass event.  Call when PA6 goes HIGH while relay is active.

    Parameters
    ----------
    peak : bool
        True if the event occurred during peak hours.
    """
    now = datetime.now()
    with _lock:
        opening = _open_ts if _open_ts is not None else now
        row = {"date": now, "opening": opening, "peak": int(bool(peak)), "counter": counter.open_count}
        if len(_buf) >= MAX_BUFFER:
            _buf.popleft()
            print("[db_log] buffer full - oldest entry discarded")
        _buf.append(row)
    print("[db_log] car_pass recorded: date=%s opening=%s peak=%d"
          % (now.strftime("%Y-%m-%d %H:%M:%S"),
             opening.strftime("%Y-%m-%d %H:%M:%S"),
             int(bool(peak))))


def click_record(var):
    """Record a button-click event into the clicks buffer.

    Parameters
    ----------
    var : int
        1 = physical remote (PA0 HIGH edge)
        2 = API call (/pulse endpoint)

    The `opening` field is set to the current time if this click caused a new
    relay activation (i.e. _open_ts was None when the click happened, meaning
    relay_open() has not been called yet for this cycle).  If the relay was
    already active (_open_ts is set), `opening` reflects that earlier time.

    Call this *before* relay_open() so that the None-check correctly identifies
    a fresh opening vs. a re-entry.
    """
    now = datetime.now()
    with _lock:
        # _open_ts is None  -> this click is the trigger for a new opening
        # _open_ts is set   -> relay was already running; record the prior open time
        opening = now if _open_ts is None else _open_ts
        cnt = counter.open_count
        row = {
            "date":    now,
            "opening": opening,
            "counter": cnt,
            "var":     int(var),
        }
        if len(_clicks_buf) >= MAX_BUFFER:
            _clicks_buf.popleft()
            print("[db_log] clicks buffer full - oldest entry discarded")
        _clicks_buf.append(row)
    print("[db_log] click_record: date=%s opening=%s counter=%d var=%d"
          % (now.strftime("%Y-%m-%d %H:%M:%S"),
             opening.strftime("%Y-%m-%d %H:%M:%S"),
             cnt, int(var)))


def get():
    """Return a copy of the in-memory buffer as a list of JSON-serialisable dicts."""
    with _lock:
        return [
            {
                "date":    row["date"].strftime("%Y-%m-%d %H:%M:%S"),
                "opening": row["opening"].strftime("%Y-%m-%d %H:%M:%S"),
                "peak":    row["peak"],
                "counter": row["counter"],
            }
            for row in _buf
        ]


def get_clicks():
    """Return a copy of the clicks buffer as a list of JSON-serialisable dicts."""
    with _lock:
        return [
            {
                "date":    row["date"].strftime("%Y-%m-%d %H:%M:%S"),
                "opening": row["opening"].strftime("%Y-%m-%d %H:%M:%S"),
                "counter": row["counter"],
                "var":     row["var"],
            }
            for row in _clicks_buf
        ]


# ── Flush logic ────────────────────────────────────────────────────────────────

def flush():
    """Save the counter to disk, flush both log buffers to MySQL, and
    (re)schedule the next periodic flush.  Called every FLUSH_INTERVAL_S
    by the background timer and on-demand from the /flush API endpoint.
    """
    global _timer

    # ── 1. Save counter to disk ───────────────────────────────────────────
    counter.save_count()
    print("[db_log] counter saved (%d)" % counter.open_count)

    # ── 2. Cancel pending timer and snapshot buffers ────────────────────────
    with _lock:
        if _timer is not None:
            _timer.cancel()
            _timer = None
        rows        = list(_buf)
        clicks_rows = list(_clicks_buf)

    if rows or clicks_rows:
        try:
            try:
                import pymysql
            except SyntaxError:
                raise RuntimeError(
                    "PyMySQL version incompatible with Python 3.5 - "
                    "run: pip3 install \"PyMySQL==0.9.3\""
                )
            conn = pymysql.connect(
                host=_DB_HOST,
                port=_DB_PORT,
                user=_DB_USER,
                password=_DB_PASS,
                database=_DB_NAME,
                connect_timeout=10,
            )
            try:
                with conn.cursor() as cur:
                    # ── cars table ────────────────────────────────────────
                    if rows:
                        sql = (
                            "INSERT IGNORE INTO `%s` (`date`, `opening`, `peak`, `counter`) "
                            "VALUES (%%s, %%s, %%s, %%s)" % _DB_TABLE
                        )
                        params = [
                            (
                                row["date"].strftime("%Y-%m-%d %H:%M:%S"),
                                row["opening"].strftime("%Y-%m-%d %H:%M:%S"),
                                row["peak"],
                                row["counter"],
                            )
                            for row in rows
                        ]
                        cur.executemany(sql, params)

                    # ── clicks table ─────────────────────────────────────
                    if clicks_rows:
                        sql_c = (
                            "INSERT IGNORE INTO `%s` (`date`, `opening`, `counter`, `var`) "
                            "VALUES (%%s, %%s, %%s, %%s)" % _DB_TABLE_CLICKS
                        )
                        params_c = [
                            (
                                row["date"].strftime("%Y-%m-%d %H:%M:%S"),
                                row["opening"].strftime("%Y-%m-%d %H:%M:%S"),
                                row["counter"],
                                row["var"],
                            )
                            for row in clicks_rows
                        ]
                        cur.executemany(sql_c, params_c)

                conn.commit()
            finally:
                conn.close()

            # Success: remove the rows we just wrote from each buffer.
            with _lock:
                for _ in range(len(rows)):
                    if _buf:
                        _buf.popleft()
                for _ in range(len(clicks_rows)):
                    if _clicks_buf:
                        _clicks_buf.popleft()
            print("[db_log] flushed %d car row(s) and %d click row(s) to MySQL"
                  % (len(rows), len(clicks_rows)))

        except Exception as exc:
            print("[db_log] flush failed (%s) - %d car row(s), %d click row(s) kept in buffer"
                  % (exc, len(rows), len(clicks_rows)))

    # ── 4. Schedule next periodic flush ───────────────────────────────────
    with _lock:
        _timer = threading.Timer(FLUSH_INTERVAL_S, flush)
        _timer.daemon = True
        _timer.start()


def start_timer():
    """Start the periodic flush timer. Called once at startup after 
    counter.load_count() has completed.
    """
    global _timer
    with _lock:
        if _timer is None:
            _timer = threading.Timer(FLUSH_INTERVAL_S, flush)
            _timer.daemon = True
            _timer.start()
