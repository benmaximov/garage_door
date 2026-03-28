# Garage Door Controller

Runs on an Orange Pi Zero (AM3352 SoM) with OPi.GPIO, a MAX7219 8-digit display, and four GPIO pins connected to the garage door opener via optocouplers.

---

## Hardware

| Pin | Direction | Description |
|-----|-----------|-------------|
| PA0 | Input  | Opening-relay sensor (optocoupler). **LOW** = relay open (idle). **HIGH pulse** = someone pressed the remote. |
| PA1 | Output | Pulse output to trigger door open (API use only). |
| PA3 | Output | Optical-sensor relay. **HIGH** (default) = door can close. **LOW** = optical circuit broken → door cannot close. |
| PA6 | Input  | Optical-sensor feedback (optocoupler). Works only when PA3 is LOW. **LOW** = optical circuit intact. **HIGH pulse** = car passing through the door. |

All inputs use optocouplers; no internal pull-up/pull-down resistors are needed.

---

## Behaviour

### Outside active hours / days
- PA0 pulses (LOW → HIGH) are **counted only**.
- PA3 is never touched.
- Display cycles between current time and the opening counter (or shows counter only when there is no internet).

### During active hours (Mon–Fri, 07:00–09:00 and 17:00–19:00)
1. **PA0 HIGH edge** → set PA3 LOW (prevent door from closing), increment counter, cancel any running timer.
2. **PA0 LOW edge** (pulse end) → start the 15-minute hold timer.
3. **PA6 HIGH edge** (car detected passing) → restart the 15-minute hold timer.
4. **Another PA0 HIGH** while timer is running → cancel timer (show static HOLD_TIME), restart on next LOW edge.
5. **Timer expires** → set PA3 HIGH (door can close again).

### Display during hold
- PA0 HIGH (pulse active): static `MM=SS` showing the full hold time.
- PA0 LOW (pulse ended): live `MM=SS` countdown to release.

---

## Configuration (`garage.py`)

| Constant | Default | Meaning |
|----------|---------|---------|
| `INTERVALS` | `[(7,9),(17,19)]` | Active time windows (hour, hour) |
| `ACTIVE_DAYS` | `[0,1,2,3,4]` | Mon–Fri (0=Mon … 6=Sun) |
| `HOLD_TIME` | `900` s (15 min) | How long to keep PA3 LOW after a pulse |

---

## HTTP API (`api.py`, port 8080)

| Endpoint | Description |
|----------|-------------|
| `GET /counter` | `{"counter": N}` — current opening count |
| `GET /pulse[?duration=S]` | Pulse PA1 HIGH for `S` seconds (default 1 s) to trigger door open |
| `GET /gpio` | `{"PA0":0/1, "PA1":0/1, "PA3":0/1, "PA6":0/1}` — current pin states |
| `GET /gpio/set?pin=PA1&state=HIGH` | Set PA1 or PA3 output state |
| `GET /hold` | Force PA3 LOW (hold door open) |
| `GET /release` | Force PA3 HIGH (allow door to close) |

---

## Files

| File | Purpose |
|------|---------|
| `garage.py` | Main controller — GPIO, logic, display loop |
| `api.py` | HTTP API server (runs in background thread) |
| `display.py` | MAX7219 SPI driver (`display_number`, `display_time`, `display_countdown`) |
| `counter.py` | Opening-count persistence (`/root/garage_count.txt`) |
| `garage.service` | systemd unit — auto-start on boot |

---

## Running

```bash
# Manual
python3 /root/garage.py

# As a service
systemctl enable garage
systemctl start garage
systemctl status garage
journalctl -u garage -f
```
