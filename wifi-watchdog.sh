#!/bin/bash
# WiFi watchdog - checks internet connectivity and reconnects WiFi if dropped.
# Writes 1 to /dev/shm/internet_ok when online, 0 when offline.
#
# Internet is determined solely by ping - works regardless of whether
# connectivity comes from wlan0 or eth0.
# WiFi reconnect is attempted only when ping has failed FAIL_THRESHOLD times
# in a row, to avoid reacting to transient packet loss.
#
# wlan0 is managed by ifupdown (wpa-conf in /etc/network/interfaces.d/wlan0).

IFACE="wlan0"
PING_HOST="8.8.8.8"
CHECK_INTERVAL=60       # seconds between connectivity checks
FAIL_THRESHOLD=3        # consecutive failures required before attempting reconnect
RECONNECT_WAIT=30       # seconds to wait for association + DHCP after reconnect
STATUS_FILE="/dev/shm/internet_ok"

write_status() {
    echo -n "$1" > "$STATUS_FILE"
}

check_connectivity() {
    # Two-packet ping to reduce false positives from single dropped packets
    ping -c 2 -W 5 "$PING_HOST" > /dev/null 2>&1
}

reconnect_wifi() {
    echo "$(date): Internet down after $FAIL_THRESHOLD consecutive failures - reconnecting WiFi..."

    # Full interface cycle - brings wpa_supplicant + DHCP back up
    ifdown wlan0 2>/dev/null
    sleep 3
    ifup wlan0 &

    # Wait for association + DHCP
    sleep "$RECONNECT_WAIT"

    # Trigger NTP sync after reconnect
    systemctl restart systemd-timesyncd 2>/dev/null

    # Verify reconnect succeeded
    if check_connectivity; then
        echo "$(date): Reconnect successful."
        write_status 1
        return 0
    else
        echo "$(date): Reconnect failed - will retry next cycle."
        return 1
    fi
}

# Give the network stack time to finish booting before first check
sleep 30

fail_count=0

while true; do
    if check_connectivity; then
        write_status 1
        if [ "$fail_count" -gt 0 ]; then
            echo "$(date): Connectivity restored (was down for $fail_count check(s))."
        fi
        fail_count=0
    else
        fail_count=$((fail_count + 1))
        echo "$(date): Connectivity check failed ($fail_count/$FAIL_THRESHOLD)"

        if [ "$fail_count" -ge "$FAIL_THRESHOLD" ]; then
            write_status 0
            reconnect_wifi
            fail_count=0
        fi
    fi
    sleep "$CHECK_INTERVAL"
done
