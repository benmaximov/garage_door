#!/usr/bin/env python3
"""
garage.py - Garage door controller

Hardware (all via optocouplers, no internal pulls needed):
  PA0: opening relay sensor. Normal LOW. Pulse = LOW->HIGH->LOW when remote pressed.
  PA1: output pulse to open door (API only).
  PA3: optical sensor relay output. HIGH = normal (door can close). LOW = door held open.
  PA6: optical sensor input. Works only when PA3 is LOW.
       Normal LOW. Goes HIGH when car passes (optical circuit broken).

Pulse logic (always active):
  HIGH edge (PA0) -> set PA3 LOW (prevent door closing), cancel any running timer,
                     increment counter (only on first activation, not re-entries)
  LOW edge  (PA0) -> start countdown (HOLD_TIME in peak hours, HOLD_TIME_SHORT outside);
                     PA3 stays LOW; display countdown
                     (skipped when api_hold_active — door stays held until /release)
  HIGH edge (PA6) -> cancel timer, hold relay static (car in beam)
  LOW edge  (PA6) -> start countdown (same hold-time selection as PA0 LOW)
                     (skipped when api_hold_active — only car is counted/logged)
  Timer=0         -> set PA3 HIGH (door can close again)
  Another HIGH (PA0) while PA3 LOW -> cancel timer, hold relay static

API hold / release:
  /hold    -> sets PA3 LOW and api_hold_active=True; no timer is started or restarted
              by PA0 or PA6 events until /release is called.
  /release -> cancel any running timer, set PA3 HIGH, clear api_hold_active and
              _relay_activated; normal operation resumes.
  PA6 events during API hold still log/count cars but do NOT start a countdown.

Display:
  PA3 LOW + PA0/PA6 HIGH -> hold-time static (HOLD_TIME in peak hours, HOLD_TIME_SHORT outside)
  PA3 LOW + PA0/PA6 LOW  -> countdown seconds (decrementing) — same for peak and non-peak
  PA3 HIGH               -> internet: cycle clock/counter  |  no internet: counter only
"""

import OPi.GPIO as GPIO
import time
import threading
from datetime import datetime

import counter
import display
import api
import db_log

# ── Configuration ────────────────────────────────────────────────────────────
INTERNET_STATUS_FILE = "/dev/shm/internet_ok"

INTERVALS = [
    (7, 9),    # morning:   07:00 - 09:00
    (17, 19),  # afternoon: 17:00 - 19:00
]
# Days of week on which the relay may activate (0=Monday ... 6=Sunday)
ACTIVE_DAYS = [0, 1, 2, 3, 4]   # Monday - Friday
HOLD_TIME       = 15 * 60        # seconds to hold relay during active hours (15 min)
HOLD_TIME_SHORT =  60        # seconds to hold relay outside active hours

# ── Pin names (SUNXI numbering) ───────────────────────────────────────────────
PA0 = "PA0"   # opening relay sensor (optocoupler; normal LOW, HIGH on pulse)
PA1 = "PA1"   # output pulse to open door (API only)
PA3 = "PA3"   # optical sensor relay (HIGH = normal/door can close; LOW = door held open)
PA6 = "PA6"   # optical sensor input (optocoupler; normal LOW, HIGH when car passes)

# ── GPIO setup ────────────────────────────────────────────────────────────────
GPIO.setmode(GPIO.SUNXI)
GPIO.setwarnings(False)
GPIO.setup(PA0, GPIO.IN)   # no pull: optocoupler drives LOW normally, HIGH on pulse
GPIO.setup(PA1, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(PA3, GPIO.OUT, initial=GPIO.HIGH)
GPIO.setup(PA6, GPIO.IN)   # no pull: optocoupler drives LOW normally, HIGH when car passes

# Read initial PA0/PA6 state so first edge is always detected correctly
pa0_was_high = GPIO.input(PA0) == GPIO.HIGH
pa6_is_high  = GPIO.input(PA6) == GPIO.HIGH

# ── Relay state ───────────────────────────────────────────────────────────────
relay_lock         = threading.Lock()
relay_timer        = None
relay_release_time = 0.0   # time.time() when relay will release
_relay_activated   = False  # True only when relay was closed by door-sensor logic
api_hold_active    = False  # True while /hold API is active; cleared by /release

# ── Debounce ──────────────────────────────────────────────────────────────────
# Delayed-read debounce: any GPIO interrupt restarts a DEBOUNCE_S timer.
# When the timer fires (after the signal has settled), the actual pin level
# is read and processed - handles any number of bounces transparently.
DEBOUNCE_S          = 0.2
_debounce_timer     = None
_debounce_pa6_timer = None

# ── Internet status ───────────────────────────────────────────────────────────
def read_internet_status():
    """Return True if internet connectivity file contains '1'."""
    try:
        with open(INTERNET_STATUS_FILE, "r") as f:
            return f.read().strip() == "1"
    except Exception:
        return False

# ── Time-interval check ───────────────────────────────────────────────────────
def in_peak_hours():
    """Return True only on ACTIVE_DAYS within a configured interval."""
    now = datetime.now()
    if now.weekday() not in ACTIVE_DAYS:
        return False
    hour = now.hour
    return any(start <= hour < end for start, end in INTERVALS)

# ── Relay control ─────────────────────────────────────────────────────────────
def release_relay():
    """Open the relay (called by timer expiry only)."""
    global relay_timer, _relay_activated
    with relay_lock:
        relay_timer = None
    _relay_activated = False
    db_log.relay_closed()
    GPIO.output(PA3, GPIO.HIGH)
    print("[relay] released")

def close_relay():
    """Set PA3 LOW (prevent door closing) and cancel any running timer.
    Called on PA0 HIGH edge (pulse start). Timer starts on the next LOW edge (pulse end).
    Counter increment is handled by the caller before this is called.
    """
    global relay_timer, _relay_activated
    if not _relay_activated:
        db_log.relay_open()
    _relay_activated = True
    GPIO.output(PA3, GPIO.LOW)
    with relay_lock:
        if relay_timer is not None:
            relay_timer.cancel()
            relay_timer = None
    print("[relay] closed / re-entry - timer cancelled, holding")

def start_countdown(hold_time=None):
    """Start (or restart) countdown. Called on PA0 LOW edge (pulse end) or PA6 LOW edge."""
    global relay_timer, relay_release_time
    if hold_time is None:
        hold_time = HOLD_TIME if in_peak_hours() else HOLD_TIME_SHORT
    relay_release_time = time.time() + hold_time
    with relay_lock:
        if relay_timer is not None:
            relay_timer.cancel()
        relay_timer = threading.Timer(hold_time, release_relay)
        relay_timer.daemon = True
        relay_timer.start()
    print("[relay] countdown started, hold=%ds" % hold_time)

# ── Interrupt callback (both edges) ──────────────────────────────────────────
def _process_pa0_settled():
    """Called after DEBOUNCE_S of quiet on PA0 - read actual pin state and act."""
    global pa0_was_high, pa6_is_high
    state = GPIO.input(PA0) == GPIO.HIGH
    if state == pa0_was_high:
        return   # no real change
    pa0_was_high = state
    if state:
        # PA0 went HIGH: pulse start (remote pressed)
        print("[sensor] pulse start (HIGH)")
        first_activation = GPIO.input(PA3) != GPIO.LOW
        if first_activation:
            # First activation: increment counter regardless of peak hours
            new_count = counter.increment()
            print("[sensor] count=%d" % new_count)
        # Record click BEFORE close_relay() so _open_ts is still None on first
        # activation (click_record uses that to set opening = now).
        # Since PA1 reflects to PA0, check sticky flag set by /pulse endpoint.
        var_type = 2 if api.is_api_pulse() else 1
        db_log.click_record(var=var_type)
        close_relay()   # counter already incremented above if first activation
        # If a car is already blocking the beam when the relay activates,
        # PA6 never fires a HIGH edge so car_pass() would be missed.
        # Check PA6 now and log the car if it's already there.
        if first_activation and GPIO.input(PA6) == GPIO.HIGH:
            pa6_is_high = True
            peak = in_peak_hours()
            db_log.car_pass(peak=peak)
            print("[sensor] car already on beam at activation (peak=%s)" % peak)
    else:
        # PA0 went LOW: pulse end (remote released)
        print("[sensor] pulse end (LOW)")
        api.clear_api_pulse()
        if GPIO.input(PA3) == GPIO.LOW:
            if api_hold_active:
                print("[sensor] pulse end - API hold active, no countdown")
            else:
                start_countdown()   # hold_time auto-selected based on in_peak_hours()

def pa0_changed(channel):
    """GPIO interrupt: cancel pending PA0 debounce timer and restart it."""
    global _debounce_timer
    if _debounce_timer is not None:
        _debounce_timer.cancel()
    _debounce_timer = threading.Timer(DEBOUNCE_S, _process_pa0_settled)
    _debounce_timer.daemon = True
    _debounce_timer.start()

GPIO.add_event_detect(PA0, GPIO.BOTH, callback=pa0_changed, bouncetime=50)

def _process_pa6_settled():
    """Called after DEBOUNCE_S of quiet on PA6 - read actual pin state and act."""
    global pa6_is_high, relay_timer
    state = GPIO.input(PA6) == GPIO.HIGH
    if state == pa6_is_high:
        return   # no real change
    pa6_is_high = state
    if state:
        # Car detected: only act if relay is active (PA3 LOW or just being activated).
        # Use _relay_activated rather than the physical PA3 pin because the pin may
        # not have been driven LOW yet if the PA0 debounce is still in flight.
        if not _relay_activated:
            return   # relay not active, ignore
        peak = in_peak_hours()
        db_log.car_pass(peak=peak)
        print("[PA6] car detected - holding timer (peak=%s)" % peak)
        with relay_lock:
            if relay_timer is not None:
                relay_timer.cancel()
                relay_timer = None
    else:
        # Car passed: only start countdown if relay is physically held LOW
        if GPIO.input(PA3) != GPIO.LOW:
            return   # relay already released, nothing to do
        if api_hold_active:
            print("[PA6] car passed - API hold active, no countdown")
        else:
            print("[PA6] car passed - starting countdown")
            start_countdown()

def pa6_changed(channel):
    """GPIO interrupt: cancel pending PA6 debounce timer and restart it."""
    global _debounce_pa6_timer
    if _debounce_pa6_timer is not None:
        _debounce_pa6_timer.cancel()
    _debounce_pa6_timer = threading.Timer(DEBOUNCE_S, _process_pa6_settled)
    _debounce_pa6_timer.daemon = True
    _debounce_pa6_timer.start()

GPIO.add_event_detect(PA6, GPIO.BOTH, callback=pa6_changed, bouncetime=50)

# ── Startup ───────────────────────────────────────────────────────────────────
counter.load_count()
display.init()
display.display_number(counter.open_count)
db_log.start_timer()
api.start()

print("garage.py running - press Ctrl+C to stop")
print("Intervals: %s  Active days: %s  hold=%ds" % (INTERVALS, ACTIVE_DAYS, HOLD_TIME))

# ── Main loop ─────────────────────────────────────────────────────────────────
DISPLAY_TIME  = 15   # seconds to show clock in cycle
DISPLAY_COUNT = 5    # seconds to show counter in cycle
REINIT_EVERY  = 300   # re-init MAX7219 every N seconds to recover from noise
display_tick  = 0

try:
    while True:
        if display_tick % REINIT_EVERY == 0:
            display.init()

        relay_on = _relay_activated

        if relay_on:
            if pa0_was_high or pa6_is_high:
                # PA0 HIGH (pulse active) or PA6 HIGH (car present): show hold-time static
                hold = HOLD_TIME if in_peak_hours() else HOLD_TIME_SHORT
                display.display_countdown(hold)
            else:
                # PA0 LOW and PA6 LOW: show live countdown (same for peak and non-peak)
                remaining = max(0, int(relay_release_time - time.time()))
                display.display_countdown(remaining)
        else:
            # Relay off: normal cycle or counter-only
            if read_internet_status():
                phase = display_tick % (DISPLAY_TIME + DISPLAY_COUNT)
                if phase < DISPLAY_TIME:
                    display.display_time()
                else:
                    display.display_number(counter.open_count)
            else:
                display.display_number(counter.open_count)

        display_tick += 1
        time.sleep(1)

except KeyboardInterrupt:
    print("\nStopping...")

finally:
    counter.save_count()
    with relay_lock:
        if relay_timer is not None:
            relay_timer.cancel()
    GPIO.output(PA3, GPIO.HIGH)
    GPIO.cleanup()
    display.close()
    print("Cleanup done.")
