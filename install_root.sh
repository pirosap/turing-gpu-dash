#!/bin/bash
# Run once as root from inside the installed directory:
#   cd ~/gpu_dash && sudo bash install_root.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$DIR/99-turing-screen.rules" /etc/udev/rules.d/99-turing-screen.rules
cp "$DIR/gpu-dash.service" /etc/systemd/system/gpu-dash.service
systemctl daemon-reload
udevadm control --reload-rules
udevadm trigger
systemctl enable --now gpu-dash.service
sleep 4
echo "---- unit ----"
systemctl is-enabled gpu-dash.service
systemctl is-active gpu-dash.service
echo "---- device ----"
ls -l /dev/ttyACM0
echo "---- last log lines ----"
journalctl -u gpu-dash.service -n 15 --no-pager
