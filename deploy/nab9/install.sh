#!/bin/bash
# Installs Ratatoskr's daily feed from the agent's staging dir, runs the first
# upload, then enables the timer. Run as root: sudo bash install.sh
set -euo pipefail
S=/var/tmp/ratatoskr-stage
D=/opt/stacks/ratatoskr
test -f "$D/.env" || { echo "missing $D/.env - store the key first"; exit 1; }
for f in ratatoskr.py dump_meshchat.py run.py; do
  install -o willryker -g willryker -m 644 "$S/$f" "$D/$f"
done
install -o willryker -g willryker -m 600 "$S/own-nodes.txt" "$D/own-nodes.txt"
install -o willryker -g willryker -m 644 "$S/gitignore" "$D/.gitignore"
install -o root -g root -m 644 "$S/ratatoskr.service" "$S/ratatoskr.timer" /etc/systemd/system/
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/ratatoskr.service /etc/systemd/system/ratatoskr.timer
echo "== first upload =="
systemctl start ratatoskr.service || true
journalctl -u ratatoskr.service --since -2min --no-pager -o cat | grep -v 'HELD BACK' || true
systemctl enable --now ratatoskr.timer
systemctl list-timers ratatoskr.timer --no-pager
rm -rf "$S"
