#!/usr/bin/env bash
# Re-enumerate a wedged USB camera without unplugging it.
#
#     sudo bash scripts/check/usb_reset.sh 4-8.3      # a USB port from usb_speed.sh
#     sudo bash scripts/check/usb_reset.sh            # every camera port
#
# When a camera drops off the bus mid-stream (errno=19) it often comes back enumerated but
# unable to stream: even `v4l2-ctl --stream-mmap` hangs. The device is wedged below the V4L2
# layer, so nothing in userspace can clear it -- only a bus-level reset can. This does that
# by de-authorizing and re-authorizing the port, which is what a replug does electrically.
#
# It does NOT fix the cause. A wrist camera that wedges when the arm moves has a cable
# problem; see TROUBLESHOOTING.md.
set -u
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

ports="${*:-}"
if [ -z "$ports" ]; then
  # Every USB device that owns a video4linux node.
  # Walk up to the USB DEVICE, not the interface. Interface dirs are named with a colon
  # (4-8.3:1.3) and carry their own `authorized`, so stopping at the first one that has the
  # file de-authorizes a single interface and leaves the device wedged.
  ports=$(for v in /sys/class/video4linux/video*; do
            p=$(readlink -f "$v/device")
            while [ -n "$p" ] && [ "$p" != "/" ]; do
              b=$(basename "$p")
              case "$b" in *:*) : ;; *) [ -f "$p/authorized" ] && { echo "$b"; break; } ;; esac
              p=$(dirname "$p")
            done
          done | sort -u | tr '\n' ' ')
fi
[ -n "${ports// /}" ] || { echo "No camera USB port found."; exit 1; }

for port in $ports; do
  d=/sys/bus/usb/devices/$port
  [ -f "$d/authorized" ] || { echo "$port: no such USB device"; continue; }
  printf '== %s ==\n' "$port"
  echo 0 > "$d/authorized"; sleep 1
  echo 1 > "$d/authorized"; sleep 2
  echo "   re-authorized"
done

# udev needs a moment to recreate the by-path symlinks; without this the next command sees
# a stale or missing path and looks like a different failure.
echo
echo "waiting for udev to settle"
udevadm settle --timeout=10 || true
sleep 2

echo
for port in $ports; do
  s=$(cat "/sys/bus/usb/devices/$port/speed" 2>/dev/null)
  printf '  %-12s %s Mbps\n' "$port" "${s:-gone}"
done
echo
echo "Now verify it actually streams (this is what was broken, not enumeration):"
echo "  bash scripts/check/usb_speed.sh"
echo "  source env.sh && python scripts/check/preflight.py --can can_right"
