#!/bin/bash
# Install as a systemd user service that starts at login.
set -e
cd "$(dirname "$(readlink -f "$0")")"
install -Dm755 eos_wifi_keepalive.py ~/.local/bin/eos_wifi_keepalive.py
install -Dm644 eos-wifi-keepalive.service ~/.config/systemd/user/eos-wifi-keepalive.service
systemctl --user daemon-reload
systemctl --user enable --now eos-wifi-keepalive.service
systemctl --user --no-pager status eos-wifi-keepalive.service | head -5
echo "logs: journalctl --user -u eos-wifi-keepalive -f"
