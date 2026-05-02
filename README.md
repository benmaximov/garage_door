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

The pulse logic is **always active** — every remote press activates the relay. What changes with time of day is only the hold duration:

- **During active hours** (Mon–Fri, 07:00–09:00 and 17:00–19:00): hold PA3 LOW for `HOLD_TIME` (15 min) after each cycle.
- **Outside active hours / days**: hold PA3 LOW for `HOLD_TIME_SHORT` (60 s) after each cycle.

The counter is incremented on every first activation (PA0 HIGH while PA3 is HIGH) regardless of time of day.

### Event sequence (same for peak and non-peak, only hold duration differs)
1. **PA0 HIGH edge** (remote pressed) → set PA3 LOW, increment counter (if first activation), cancel any running timer.
2. **PA0 LOW edge** (remote released) → start countdown (`HOLD_TIME` peak / `HOLD_TIME_SHORT` non-peak).
3. **PA6 HIGH edge** (car enters the beam) → **cancel** the running timer; hold PA3 LOW indefinitely while the car is in the beam.
4. **PA6 LOW edge** (car leaves the beam) → start a fresh countdown (same duration selection as rule 2).
5. **Another PA0 HIGH** while PA3 is already LOW → cancel the timer; new countdown starts on the next PA0 LOW edge.
6. **Timer expires** → set PA3 HIGH (door can close again).

### Display behaviour
- **Relay idle** (PA3 HIGH):
  - With internet: cycle clock (15 s) and opening counter (5 s).
  - Without internet: counter only.
- **Relay active** (PA3 LOW):
  - PA0 HIGH (button pressed) OR PA6 HIGH (car in beam) → static `MM=SS` showing the full hold time.
  - PA0 LOW and PA6 LOW (idle, waiting for timeout) → live `MM=SS` countdown to release.

---

## Configuration (`garage.py`)

| Constant | Default | Meaning |
|----------|---------|---------|
| `INTERVALS` | `[(7,9),(17,19)]` | Active time windows (hour, hour) |
| `ACTIVE_DAYS` | `[0,1,2,3,4]` | Mon–Fri (0=Mon … 6=Sun) |
| `HOLD_TIME` | `900` s (15 min) | Hold duration during active hours |
| `HOLD_TIME_SHORT` | `60` s | Hold duration outside active hours / days |

---

## HTTP API (`api.py`, port 8080)

| Endpoint | Description |
|----------|-------------|
| `GET /counter` | `{"counter": N}` — current opening count |
| `GET /pulse[?duration=S]` | Pulse PA1 HIGH for `S` seconds (default 1 s) to trigger door open |
| `GET /gpio` | `{"PA0":0/1, "PA1":0/1, "PA3":0/1, "PA6":0/1}` — current pin states |
| `GET /gpio/set?pin=PA1&state=HIGH` | Set PA1 or PA3 output state |
| `GET /hold` | Force PA3 LOW (hold door open indefinitely, disables timer starts) |
| `GET /release` | Force PA3 HIGH (allow door to close; clears hold and timers) |
| `GET /cars` | List of buffered PA6 car-pass events: `[{"date":..., "opening":..., "peak":0\|1, "counter":N}, ...]` |
| `GET /clicks` | List of buffered button-click events: `[{"date":..., "opening":..., "counter":N, "var":1\|2}, ...]` (`var=1` remote, `var=2` API) |
| `GET /flush` | Save counter and flush log buffers to MySQL immediately (resets the 30-min auto-flush timer). Returns `{"ok": true, "counter": N}` |
| `GET /optocheck` | Diagnostic: briefly power the optical sensor and read PA6. Returns `{"status": "normal"\|"blocked", "pa6": 0\|1, "held": true\|false}`. `normal` = optical loop intact (nothing in the beam); `blocked` = something is in the way. Does **not** affect counters, car-pass logs or hold timers. If PA3 is already LOW (relay active or `/hold` in effect), PA6 is sampled without releasing PA3 (`held: true`). |

---

## Files

| File | Purpose |
|------|---------|
| `garage.py` | Main controller — GPIO, pulse logic, display loop |
| `api.py` | HTTP API server (runs in background thread) |
| `state.py` | Shared mutable state + locks between `garage.py` and `api.py` |
| `display.py` | MAX7219 SPI driver (`display_number`, `display_time`, `display_countdown`) |
| `counter.py` | Opening-count persistence (`/root/garage_count.txt`) |
| `db_log.py` | MySQL logger for clicks and car-pass events (buffered, auto-flush every 30 min) |
| `garage.service` | systemd unit — auto-start on boot |
| `wifi-watchdog.sh` | Reconnects WiFi on sustained internet loss; writes `/dev/shm/internet_ok` |
| `wifi-watchdog.service` | systemd unit for the watchdog |

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

---

## Initial Setup — Orange Pi Zero (H2+, 256 MB)

This section documents the full provisioning procedure from a bare Armbian Stretch image to a running garage controller.

### 1. Flash the SD card

Use Armbian Debian Stretch (legacy kernel) for the Orange Pi Zero H2+.  
Flash with Balena Etcher or `dd`. Boot with serial console attached (115200 baud) or with an Ethernet cable connected.

Default root credentials are `root / 1234` — Armbian forces a password change on first login.

---

### 2. Basic system configuration

```bash
# Set hostname
hostnamectl set-hostname garage
echo "garage" > /etc/hostname

# Set timezone
timedatectl set-timezone Europe/Sofia   # adjust to your zone
```

Remove the default non-root user created by Armbian if not needed:

```bash
deluser --remove-home orangepi
```

---

### 3. Expand the root filesystem

Armbian may not auto-expand to the full SD card. Do it manually:

```bash
# Check current layout
lsblk

# Use armbian-config or resize manually:
armbian-config   # → System → Resize

# Or manually with fdisk + resize2fs after reboot
```

---

### 4. Fix apt sources (Debian Stretch only)

Stretch reached end-of-life; its repos moved to the archive:

```bash
cat > /etc/apt/sources.list << 'EOF'
deb http://archive.debian.org/debian stretch main contrib non-free
deb http://archive.debian.org/debian-security stretch/updates main contrib non-free
EOF

apt-get update
apt-get dist-upgrade -y
```

---

### 5. Disable unused hardware and services

Reduces memory usage and heat:

```bash
# Blacklist unused kernel modules
cat > /etc/modprobe.d/blacklist-unused.conf << 'EOF'
blacklist snd_soc_core
blacklist snd_pcm
blacklist snd_timer
blacklist snd
blacklist lima
blacklist drm
blacklist videobuf2_core
blacklist videobuf2_v4l2
blacklist sunxi_cedrus
EOF

# Disable unused services
systemctl disable --now ModemManager NetworkManager dnsmasq hostapd bluetooth 2>/dev/null
```

---

### 6. Set CPU governor to powersave and cap maximum frequency

The H2+ runs hot at full speed; 480 MHz is sufficient for this workload.  
Setting both the governor and `scaling_max_freq` prevents the kernel from ever clocking above 480 MHz:

```bash
cat > /etc/rc.local << 'EOF'
#!/bin/sh -e
echo powersave > /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
echo 480000 > /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq
exit 0
EOF
chmod +x /etc/rc.local
```

Apply immediately without rebooting:

```bash
echo powersave > /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
echo 480000 > /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq
```

Verify:

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq   # should print 480000
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq   # should print 480000
```

---

### 7. Enable SPI1 in the device tree

The MAX7219 display is wired to SPI1 (PA13/PA14/PA15). The stock DTB does not enable it.

Back up and patch the DTB:

```bash
cp /boot/dtb/sun8i-h2-plus-orangepi-zero.dtb \
   /boot/dtb/sun8i-h2-plus-orangepi-zero.dtb.bak

apt-get install -y device-tree-compiler
dtc -I dtb -O dts /boot/dtb/sun8i-h2-plus-orangepi-zero.dtb -o /tmp/opi.dts
```

In `/tmp/opi.dts`:

- Find the `spi@1c69000` node (SPI1) — set `status = "okay"` and add a `spidev@0` child node:

```dts
spi@1c69000 {
    status = "okay";
    spidev@0 {
        compatible = "rohm,dh2228fv";
        reg = <0>;
        spi-max-frequency = <10000000>;
    };
};
```

- Find the `spi@1c68000` node (SPI0) — set `status = "disabled"` and remove the `flash@0` child node if present.

Recompile and reboot:

```bash
dtc -I dts -O dtb /tmp/opi.dts -o /boot/dtb/sun8i-h2-plus-orangepi-zero.dtb
reboot
```

After reboot, `/dev/spidev0.0` should appear.

---

### 8. Install Python dependencies

```bash
apt-get install -y python3 python3-pip
pip3 install OPi.GPIO spidev pymysql
```

`pymysql` is only needed if MySQL logging is used (`db_log.py`). The controller runs without it.

---

### 9. Deploy application files

Copy all `.py` files and service units from this repository to `/root/` on the device:

```bash
scp *.py *.service wifi-watchdog.sh root@<device-ip>:/root/
```

Install and start the garage service:

```bash
chmod +x /root/wifi-watchdog.sh

systemctl daemon-reload

systemctl enable garage
systemctl start garage

systemctl enable wifi-watchdog
systemctl start wifi-watchdog
```

---

### 10. Configure WiFi

Create the wpa_supplicant credentials file:

```bash
cat > /etc/wpa_supplicant/wpa_supplicant.conf << 'EOF'
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=BG

network={
    ssid="YOUR_SSID"
    psk="YOUR_PASSWORD"
    key_mgmt=WPA-PSK
}
EOF
chmod 600 /etc/wpa_supplicant/wpa_supplicant.conf
```

Create the interface configuration:

```bash
cat > /etc/network/interfaces.d/wlan0 << 'EOF'
auto wlan0
iface wlan0 inet dhcp
    wpa-conf /etc/wpa_supplicant/wpa_supplicant.conf
EOF
```

Assign wlan0 a **lower metric** (50) than eth0 (100) so it becomes the preferred default route — lower numbers always win in Linux routing:

```bash
cat > /etc/dhcp/dhclient-exit-hooks.d/wlan0-route-fix << 'EOF'
if [ "$interface" = "wlan0" ] && [ "$reason" = "BOUND" -o "$reason" = "RENEW" -o "$reason" = "REBIND" -o "$reason" = "REBOOT" ]; then
    ip route del default via "$new_routers" dev wlan0 2>/dev/null
    ip route add default via "$new_routers" dev wlan0 metric 50
fi
EOF
chmod +x /etc/dhcp/dhclient-exit-hooks.d/wlan0-route-fix
```

Bring the interface up:

```bash
ifup wlan0
```

Verify connectivity:

```bash
ip route          # should show wlan0 default at metric 50
ping -c 3 8.8.8.8
```

---

### 11. Configure static IP on eth0 (optional)

Useful for direct Windows host ↔ device connection for SSH/serial fallback:

```bash
cat > /etc/network/interfaces.d/eth0 << 'EOF'
auto eth0
iface eth0 inet static
    address 192.168.137.5
    netmask 255.255.255.0
    gateway 192.168.137.1
    metric 100
    dns-nameservers 8.8.8.8
EOF
```

**Important**: the `metric 100` line is essential. Without it, ifupdown installs the eth0 default route at metric 0, which beats wlan0's metric 50 — and when the eth0 cable is unplugged, the kernel still routes packets out the downed interface instead of falling through to wlan0. Symptom: `ping 8.8.8.8` fails with "Destination Host Unreachable" via eth0 even though wlan0 is fully associated.

---

### 12. Verify everything is running

```bash
systemctl status garage
systemctl status wifi-watchdog
journalctl -u garage -f

# Check internet flag written by watchdog
cat /dev/shm/internet_ok   # should print 1

# Check SPI display device
ls /dev/spidev*            # should show /dev/spidev0.0

# Test the HTTP API
curl http://localhost:8080/counter
curl http://localhost:8080/gpio
```
