"""
api.py - Simple HTTP API server for garage.py

Endpoints:
  GET /counter                         -> {"counter": <int>}
  GET /pulse                           -> pulses PA1 HIGH for PULSE_DURATION_S seconds
  GET /pulse?duration=<secs>           -> pulses PA1 for the given duration
  GET /gpio                            -> {"PA0": 0|1, "PA1": 0|1, "PA3": 0|1}
  GET /gpio/set?pin=PA1|PA3&state=HIGH|LOW -> set PA1 or PA3 output state
  GET /hold                            -> hold relay: sets PA3 LOW, cancels timer, sets api_hold_active
  GET /release                         -> release relay: sets PA3 HIGH, clears timers and flags
  GET /cars                            -> list of PA6 car-pass events in buffer
                                          [{"date": "...", "opening": "...", "peak": 0|1, "counter": <int>}, ...]
  GET /clicks                          -> list of button-click events in buffer
                                          [{"date": "...", "opening": "...", "counter": <int>, "var": 1|2}, ...]
  GET /flush                           -> save counter + flush log buffers to MySQL now (resets 30-min timer)
                                          {"ok": true, "counter": <int>}
  GET /optocheck                       -> {"status": "normal"|"blocked", "pa6": 0|1, "held": true|false}
                                          Briefly pulls PA3 LOW to power the optical sensor, reads PA6.
                                          If relay is already active (PA3 already LOW), just reads PA6
                                          without touching PA3 or any counters/timers.
"""

import re
import threading
import time
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

import OPi.GPIO as GPIO
import counter as cnt
import db_log
import state

# ── Configuration ─────────────────────────────────────────────────────────────
API_PORT           = 8080
PULSE_DURATION_S   = 1.0   # default pulse duration in seconds
PA0                = "PA0" # input pin (door sensor)
PA1                = "PA1" # output pin for API pulse / control
#PA2                = "PA2" # output pin for API control
PA3                = "PA3" # output pin for API control
PA6                = "PA6" # input pin (state logged to console)

# Pins that may be written via the API (regex pattern: PA followed by digits)
_PIN_RE     = re.compile(r'^PA\d+$')
_OUT_PINS   = {PA1, PA3}   # writable pins
_READ_PINS  = [PA0, PA1, PA3, PA6]  # pins reported by GET /gpio

# ── GPIO for PA1 ──────────────────────────────────────────────────────────────
# Note: garage.py sets up GPIO mode and PA1 already; this module only uses it.
_pa1_lock = threading.Lock()
_pa1_timer = None
_api_pulse_pending = False   # sticky flag: set before PA1 HIGH, cleared on PA0 LOW edge

# ── Optical-path check ────────────────────────────────────────────────────────
_optocheck_lock    = threading.Lock()   # serialise concurrent /optocheck calls
OPTOCHECK_HOLD_S   = 0.15               # how long to hold PA3 LOW while sampling PA6
OPTOCHECK_SETTLE_S = 0.05               # wait after PA3 LOW before reading PA6


def optocheck():
    """Check the optical-sensor path without disturbing the main state machine.

    Behaviour:
      * If the relay is currently active (state.relay_activated == True — set
        by the pulse logic or by /hold), just read PA6 and return. PA3 is left
        LOW, no timer is touched, no edges are suppressed (the normal PA6 ISR
        is needed for car-pass logging during a real hold).
      * Otherwise (relay idle, PA3 HIGH), temporarily:
          1. Suppress the PA6 ISR so no edge we cause can reach the state
             machine (no car_pass logs, no spurious countdown starts).
          2. Drive PA3 LOW to power the optocoupler, wait to settle, read PA6.
          3. Restore PA3 HIGH — but ONLY if state.relay_activated is still
             False. If a real PA0 edge fired during our sampling window and
             legitimately activated the relay, leave PA3 LOW so we don't fight
             the pulse logic.
          4. Resume the PA6 ISR, resyncing its cached level to truth.

    Concurrent /optocheck calls are serialised by _optocheck_lock.

    No counters, car-pass logs, db_log entries or timers are touched.
    Returns (status_str, pa6_level, already_held_bool).
    """
    with _optocheck_lock:
        already_held = state.relay_activated
        if already_held:
            # Relay is in legitimate use — just sample PA6, touch nothing.
            # The normal PA6 ISR must stay live to log any real car passages.
            pa6 = GPIO.input(PA6)
        else:
            # Relay is idle: safe to drive PA3 briefly. Suppress PA6 ISR first
            # so any edge we induce cannot enter the state machine.
            state.suppress_pa6_isr = True
            try:
                GPIO.output(PA3, GPIO.LOW)
                try:
                    time.sleep(OPTOCHECK_SETTLE_S)
                    pa6 = GPIO.input(PA6)
                    time.sleep(OPTOCHECK_HOLD_S - OPTOCHECK_SETTLE_S)
                finally:
                    # If a real pulse legitimately activated the relay during
                    # our window, DO NOT pull PA3 back HIGH — that would break
                    # the hold. Only restore HIGH if still idle.
                    if not state.relay_activated:
                        GPIO.output(PA3, GPIO.HIGH)
                    # Brief settle so the opto has reached its post-test level
                    # before we resync pa6_is_high and re-enable the ISR.
                    time.sleep(OPTOCHECK_SETTLE_S)
            finally:
                # Resync garage.py's cached pa6_is_high and cancel any
                # pending debounce timer BEFORE re-enabling the ISR, so the
                # next real edge compares against truth.
                if state.resync_pa6 is not None:
                    state.resync_pa6()
                state.suppress_pa6_isr = False
        status = "blocked" if pa6 == GPIO.HIGH else "normal"
        return status, int(pa6 == GPIO.HIGH), already_held


def _end_pulse():
    with _pa1_lock:
        global _pa1_timer
        _pa1_timer = None
    GPIO.output(PA1, GPIO.LOW)


def is_api_pulse():
    """Return True if the current PA0 cycle was triggered by an API /pulse call.
    The flag is sticky: set when /pulse fires PA1, survives the PA1 timer ending,
    and is only cleared by clear_api_pulse() when PA0 settles back to LOW.
    """
    with _pa1_lock:
        return _api_pulse_pending


def clear_api_pulse():
    """Clear the API pulse flag. Called from garage.py when PA0 LOW edge is processed."""
    global _api_pulse_pending
    with _pa1_lock:
        _api_pulse_pending = False


def pulse_pa1(duration=PULSE_DURATION_S):
    """Raise PA1 HIGH for 'duration' seconds, then drop LOW."""
    global _pa1_timer, _api_pulse_pending
    with _pa1_lock:
        _api_pulse_pending = True
        if _pa1_timer is not None:
            _pa1_timer.cancel()
        _pa1_timer = threading.Timer(duration, _end_pulse)
        _pa1_timer.daemon = True
        _pa1_timer.start()
    GPIO.output(PA1, GPIO.HIGH)


# ── HTTP handler ──────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass   # suppress default access log

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self._do_GET_inner()
        except Exception as exc:
            try:
                self._send_json(500, {"error": str(exc)})
            except Exception:
                pass

    def _do_GET_inner(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        if path == "/counter":
            self._send_json(200, {"counter": cnt.open_count})
        elif path == "/flush":
            db_log.flush()
            self._send_json(200, {"ok": True, "counter": cnt.open_count})
        elif path == "/pulse":
            duration = PULSE_DURATION_S
            if "duration" in params:
                try:
                    duration = float(params["duration"][0])
                except ValueError:
                    pass
            pulse_pa1(duration)
            self._send_json(200, {"ok": True, "duration": duration})
        elif path == "/gpio":
            self._send_json(200, {pin: GPIO.input(pin) for pin in _READ_PINS})
        elif path == "/cars":
            self._send_json(200, db_log.get())
        elif path == "/clicks":
            self._send_json(200, db_log.get_clicks())
        elif path == "/gpio/set":
            pin = params.get("pin", [None])[0] or ""
            # normalise to canonical "PA<n>" form
            _m_digit = re.match(r'^(\d+)$', pin)
            _m_pa    = re.match(r'^[Pp][Aa](\d+)$', pin)
            if _m_digit:
                pin = "PA" + _m_digit.group(1)
            elif _m_pa:
                pin = "PA" + _m_pa.group(1)
            pin_state = (params.get("state", [None])[0] or "").upper()
            # normalise "1"/"0" -> "HIGH"/"LOW"
            pin_state = {"1": "HIGH", "0": "LOW"}.get(pin_state, pin_state)
            if not _PIN_RE.match(pin) or pin not in _OUT_PINS:
                self._send_json(400, {"error": "pin must be a writable PA pin (e.g. PA1, pa1, 1)"})
            elif pin_state not in ("HIGH", "LOW"):
                self._send_json(400, {"error": "state must be HIGH, LOW, 1, or 0"})
            else:
                level = GPIO.HIGH if pin_state == "HIGH" else GPIO.LOW
                GPIO.output(pin, level)
                self._send_json(200, {"ok": True, "pin": pin, "state": pin_state})
        elif path == "/optocheck":
            status, pa6, held = optocheck()
            self._send_json(200, {"status": status, "pa6": pa6, "held": held})
        elif path == "/hold":
            state.api_hold_active = True
            if not state.relay_activated:
                db_log.relay_open()
            state.relay_activated = True
            with state.relay_lock:
                if state.relay_timer is not None:
                    state.relay_timer.cancel()
                    state.relay_timer = None
            GPIO.output(PA3, GPIO.LOW)
            self._send_json(200, {"ok": True, "PA3": "LOW"})
        elif path == "/release":
            state.api_hold_active = False
            with state.relay_lock:
                if state.relay_timer is not None:
                    state.relay_timer.cancel()
                    state.relay_timer = None
            state.relay_activated = False
            db_log.relay_closed()
            GPIO.output(PA3, GPIO.HIGH)
            self._send_json(200, {"ok": True, "PA3": "HIGH"})
        else:
            self._send_json(404, {"error": "not found"})


# ── Public API ────────────────────────────────────────────────────────────────
def start(port=API_PORT):
    """Start the HTTP server in a background daemon thread."""
    server = HTTPServer(("", port), _Handler)
    t = threading.Thread(target=server.serve_forever, name="api-server")
    t.daemon = True
    t.start()
    print("[api] HTTP server listening on port %d" % port)
