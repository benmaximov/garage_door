# WiFi watchdog — OS conflict checklist

Run these on the OPi Zero to find and neutralise conflicting daemons
before the watchdog will work reliably.

---

## 1. Detect which network manager is active

```bash
systemctl is-active NetworkManager
systemctl is-active systemd-networkd
systemctl is-active connman
systemctl is-active dhcpcd
```

---

## 2a. If NetworkManager is active → disable NM managing wlan0

Option A — tell NM to leave wlan0 alone (preferred, keeps NM for eth0):

```bash
cat > /etc/NetworkManager/conf.d/99-unmanaged-wlan0.conf << 'EOF'
[keyfile]
unmanaged-devices=interface-name:wlan0
EOF
systemctl restart NetworkManager
```

Option B — disable NM entirely if you don't need it:

```bash
systemctl disable --now NetworkManager
```

---

## 2b. If systemd-networkd is active → remove the wlan0 network file

```bash
# List .network files that match wlan0
grep -rl 'wlan\|wireless\|WiFi' /etc/systemd/network/ /lib/systemd/network/ 2>/dev/null

# Either delete/rename the matching file, or add a [Match] override:
# (example: if the file is /etc/systemd/network/10-wlan0.network)
# Comment out or remove [DHCP] and [Network] sections for wlan0,
# then restart:
systemctl restart systemd-networkd
```

---

## 2c. If dhcpcd is active → exclude wlan0 from dhcpcd

```bash
echo "denyinterfaces wlan0" >> /etc/dhcpcd.conf
systemctl restart dhcpcd
```

---

## 2d. If connman is active → disable it

```bash
systemctl disable --now connman
```

---

## 3. Ensure wpa_supplicant is running and owns wlan0

wpa_supplicant must be managed by systemd, NOT by NM or connman.
Check the running instance:

```bash
ps aux | grep wpa_supplicant
```

It should look like:
  wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant/wpa_supplicant.conf

If it is missing, enable it:

```bash
systemctl enable --now wpa_supplicant@wlan0
# or the generic unit:
systemctl enable --now wpa_supplicant
```

---

## 4. Make sure credentials are in wpa_supplicant.conf

```bash
cat /etc/wpa_supplicant/wpa_supplicant.conf
# Must contain:
# network={
#     ssid="YourSSID"
#     psk="YourPassword"
# }
```

---

## 5. Deploy updated watchdog and restart

```bash
# copy wifi-watchdog.sh to /root/wifi-watchdog.sh on the device
chmod +x /root/wifi-watchdog.sh
systemctl daemon-reload
systemctl enable --now wifi-watchdog
systemctl restart wifi-watchdog
journalctl -u wifi-watchdog -f   # watch live output
```
