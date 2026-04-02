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

Start by calling start() which launches the server in a daemon thread.
garage.py calls api.start() during startup.
"""

import re
import threading
import time
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

import OPi.GPIO as GPIO
import counter as cnt
import db_log

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
            state = (params.get("state", [None])[0] or "").upper()
            # normalise "1"/"0" -> "HIGH"/"LOW"
            state = {"1": "HIGH", "0": "LOW"}.get(state, state)
            if not _PIN_RE.match(pin) or pin not in _OUT_PINS:
                self._send_json(400, {"error": "pin must be a writable PA pin (e.g. PA1, pa1, 1)"})
            elif state not in ("HIGH", "LOW"):
                self._send_json(400, {"error": "state must be HIGH, LOW, 1, or 0"})
            else:
                level = GPIO.HIGH if state == "HIGH" else GPIO.LOW
                GPIO.output(pin, level)
                self._send_json(200, {"ok": True, "pin": pin, "state": state})
        elif path == "/hold":
            import garage
            garage.api_hold_active = True
            if not garage._relay_activated:
                db_log.relay_open()
            garage._relay_activated = True
            with garage.relay_lock:
                if garage.relay_timer is not None:
                    garage.relay_timer.cancel()
                    garage.relay_timer = None
            GPIO.output(PA3, GPIO.LOW)
            self._send_json(200, {"ok": True, "PA3": "LOW"})
        elif path == "/release":
            import garage
            garage.api_hold_active = False
            with garage.relay_lock:
                if garage.relay_timer is not None:
                    garage.relay_timer.cancel()
                    garage.relay_timer = None
            garage._relay_activated = False
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
