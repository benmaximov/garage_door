#!/usr/bin/env python3
"""
garage.py - Garage door controller

Hardware (all via optocouplers, no internal pulls needed):
  PA0: opening relay sensor. Normal LOW. Pulse = LOW->HIGH->LOW when remote pressed.
  PA1: output pulse to open door (API only).
  PA3: optical sensor relay output. HIGH = normal (door can close). LOW = door held open.
  PA6: optical sensor input. Works only when PA3 is LOW.
       Normal LOW. Goes HIGH when car passes (optical circuit broken).

Pulse logic (ACTIVE_DAYS only, within INTERVALS):
  HIGH edge (PA0) -> set PA3 LOW (prevent door closing), cancel any running timer,
                     increment counter (only on first activation, not re-entries)
  LOW edge  (PA0) -> start HOLD_TIME countdown; PA3 stays LOW; display countdown
  HIGH edge (PA6) -> restart HOLD_TIME countdown (car detected passing through)
  Timer=0         -> set PA3 HIGH (door can close again)
  Another HIGH (PA0) while PA3 LOW -> cancel timer, show HOLD_TIME static

Outside active hours/days: PA0 pulses are counted only (PA3 not touched).

Display:
  PA3 LOW + PA0 HIGH -> HOLD_TIME number (static, all 8 digits)
  PA3 LOW + PA0 LOW  -> countdown seconds (decrementing, all 8 digits)
  PA3 HIGH           -> internet: cycle clock/counter  |  no internet: counter only
"""

import OPi.GPIO as GPIO
import time
import threading
from datetime import datetime

import counter
import display
import api

# ── Configuration ────────────────────────────────────────────────────────────
INTERNET_STATUS_FILE = "/dev/shm/internet_ok"

INTERVALS = [
    (7, 9),    # morning:   07:00 - 09:00
    (17, 19),  # afternoon: 17:00 - 19:00
]
# Days of week on which the relay may activate (0=Monday ... 6=Sunday)
ACTIVE_DAYS = [0, 1, 2, 3, 4]   # Monday - Friday
HOLD_TIME   = 15 * 60            # seconds to hold relay (15 min)

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

# Read initial PA0 state so first edge is always detected correctly
pa0_was_high = GPIO.input(PA0) == GPIO.HIGH

# ── Relay state ───────────────────────────────────────────────────────────────
relay_lock         = threading.Lock()
relay_timer        = None
relay_release_time = 0.0   # time.time() when relay will release
_relay_activated   = False  # True only when relay was closed by door-sensor logic

# ── Debounce ──────────────────────────────────────────────────────────────────
# Delayed-read debounce: any GPIO interrupt restarts a DEBOUNCE_S timer.
# When the timer fires (after the signal has settled), the actual pin level
# is read and processed - handles any number of bounces transparently.
DEBOUNCE_S         = 0.2
_debounce_timer    = None
_flash_counter     = False   # set True to force display to counter phase immediately

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
    GPIO.output(PA3, GPIO.HIGH)
    print("[relay] released")

def close_relay(first_activation):
    """Set PA3 LOW (prevent door closing) and cancel any running timer.
    Called on PA0 HIGH edge (pulse start). Timer starts on the next LOW edge (pulse end).
    """
    global relay_timer, _relay_activated
    _relay_activated = True
    GPIO.output(PA3, GPIO.LOW)
    with relay_lock:
        if relay_timer is not None:
            relay_timer.cancel()
            relay_timer = None
    if first_activation:
        new_count = counter.increment()
        print("[relay] closed, count=%d" % new_count)
    else:
        print("[relay] re-entry while on - timer cancelled, HOLD_TIME static")

def start_countdown():
    """Start (or restart) HOLD_TIME countdown. Called on PA0 LOW edge (pulse end) or PA6 HIGH."""
    global relay_timer, relay_release_time
    relay_release_time = time.time() + HOLD_TIME
    with relay_lock:
        if relay_timer is not None:
            relay_timer.cancel()
        relay_timer = threading.Timer(HOLD_TIME, release_relay)
        relay_timer.daemon = True
        relay_timer.start()
    print("[relay] countdown started, hold=%ds" % HOLD_TIME)

# ── Interrupt callback (both edges) ──────────────────────────────────────────
def _process_settled():
    """Called after DEBOUNCE_S of quiet - read actual pin state and act."""
    global pa0_was_high
    state = GPIO.input(PA0) == GPIO.HIGH
    if state == pa0_was_high:
        return   # no real change
    pa0_was_high = state
    if state:
        # PA0 went HIGH: pulse start (remote pressed)
        print("[sensor] pulse start (HIGH)")
        if in_peak_hours():
            relay_on = GPIO.input(PA3) == GPIO.LOW
            close_relay(first_activation=not relay_on)
        else:
            global _flash_counter
            new_count = counter.increment()
            _flash_counter = True
            print("[relay] outside active hours/days - counted only, count=%d" % new_count)
    else:
        # PA0 went LOW: pulse end (remote released)
        print("[sensor] pulse end (LOW)")
        if GPIO.input(PA3) == GPIO.LOW:
            start_countdown()

def door_changed(channel):
    """GPIO interrupt: cancel pending debounce timer and restart it."""
    state = GPIO.input(PA0)
    
    global _debounce_timer
    if _debounce_timer is not None:
        _debounce_timer.cancel()
    _debounce_timer = threading.Timer(DEBOUNCE_S, _process_settled)
    _debounce_timer.daemon = True
    _debounce_timer.start()

GPIO.add_event_detect(PA0, GPIO.BOTH, callback=door_changed, bouncetime=50)

def pa6_changed(channel):
    """GPIO interrupt: print PA6 state; restart timer if PA3 is LOW and PA6 went HIGH."""
    state = GPIO.input(PA6)
    if state == GPIO.HIGH and GPIO.input(PA3) == GPIO.LOW:
        print("[PA6] car detected - restarting timer")
        start_countdown()

GPIO.add_event_detect(PA6, GPIO.BOTH, callback=pa6_changed, bouncetime=50)

# ── Startup ───────────────────────────────────────────────────────────────────
counter.load_count()
display.init()
display.display_number(counter.open_count)
counter.periodic_save()
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

        # If a count happened outside peak hours, jump to counter phase
        if _flash_counter:
            _flash_counter = False
            display_tick = DISPLAY_TIME   # start of counter phase

        relay_on = _relay_activated

        if relay_on:
            if pa0_was_high:
                # PA0 HIGH (pulse active): show HOLD_TIME static as MM = SS
                display.display_countdown(HOLD_TIME)
            else:
                # PA0 LOW (pulse ended): show countdown as MM = SS
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
