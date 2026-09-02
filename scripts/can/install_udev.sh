#!/usr/bin/env bash
# Install the udev rule that pins gs_usb adapters to can_left / can_right.
#
#     sudo bash scripts/can/install_udev.sh
#
# Run once per machine, and again after editing 80-piper-can.rules.
#
# A udev NAME= only applies when the device is ADDED, so an already-present interface is
# not renamed by reloading rules alone. Reloading the gs_usb driver destroys and recreates
# both net devices, which makes udev re-apply the rule -- that is why this script does it.
set -euo pipefail

RULES=80-piper-can.rules
SRC="$(cd "$(dirname "$0")" && pwd)/$RULES"
DST=/etc/udev/rules.d/$RULES

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
[ -f "$SRC" ] || { echo "Missing $SRC"; exit 1; }

echo "== 1. install $DST =="
install -m 644 "$SRC" "$DST"

echo "== 2. reload udev rules =="
udevadm control --reload-rules

echo "== 3. reload gs_usb so udev re-applies NAME= =="
# Named interfaces must be down before the driver goes, or the unbind can hang.
for IF in $(ip -br link show type can | awk '{print $1}'); do ip link set "$IF" down 2>/dev/null || true; done
modprobe -r gs_usb 2>/dev/null || true; sleep 2; modprobe gs_usb; sleep 2

echo
echo "== 4. result =="
ip -br link show type can
echo
FOUND=$(ip -br link show type can | awk '{print $1}' | sort | tr '\n' ' ')
case "$FOUND" in
  *can_left*can_right*) echo ">>> OK: both adapters pinned." ;;
  *can_left*|*can_right*) echo ">>> PARTIAL: only one pinned. The other adapter is unplugged, or its serial is not in $RULES." ;;
  *) echo ">>> FAILED: still kernel names. Check: udevadm test /sys/class/net/can0" ;;
esac
echo
echo "Serials currently on the bus:"
for IF in $(ip -br link show type can | awk '{print $1}'); do
  printf "  %-10s %s\n" "$IF" "$(udevadm info -p "/sys/class/net/$IF" | sed -n 's/^E: ID_SERIAL_SHORT=//p')"
done
echo
echo "Next: sudo bash scripts/can/fix_can.sh    # sets the 1 Mbps bitrate and brings them up"
