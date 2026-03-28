#!/bin/bash
# WiFi watchdog - checks connectivity and reconnects if dropped
# Writes 1 to /dev/shm/internet_ok when online, 0 when offline.
#
# wlan0 is managed by wifi.service (wpa_supplicant) and wifi-dhcp.service (dhclient).
# On reconnect we restart wifi.service; wifi-dhcp.service restarts automatically
# via BindsTo=wifi.service.

IFACE="wlan0"
PING_HOST="8.8.8.8"
CHECK_INTERVAL=60
STATUS_FILE="/dev/shm/internet_ok"

write_status() {
    echo -n "$1" > "$STATUS_FILE"
}

reconnect_wifi() {
    echo "$(date): Internet down, attempting WiFi reconnect..."
    systemctl restart wifi.service
    # wifi-dhcp.service restarts automatically (BindsTo=wifi.service)
    # Wait for association + DHCP
    sleep 20
    # Trigger NTP sync after reconnect
    systemctl restart systemd-timesyncd 2>/dev/null
}

# Give the network stack time to finish booting before first check
sleep 30

while true; do
    if ping -c 1 -W 5 "$PING_HOST" > /dev/null 2>&1; then
        write_status 1
    else
        write_status 0
        reconnect_wifi
    fi
    sleep "$CHECK_INTERVAL"
done
