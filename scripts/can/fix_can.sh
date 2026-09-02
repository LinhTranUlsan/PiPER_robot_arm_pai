#!/usr/bin/env bash
# Reset the gs_usb adapters, set the 1 Mbps bitrate, and measure each bus.
#
#     sudo bash scripts/can/fix_can.sh                    # every bus
#     sudo bash scripts/can/fix_can.sh --can can_right    # just the right cluster
#     sudo bash scripts/can/fix_can.sh --can right        # same thing, shorthand
#
# Run this when bus_scan.py sees nothing, or after a bus-off. Reloading the driver is the
# only reliable way to clear a wedged gs_usb adapter -- the error counters do NOT reset on
# a plain link down/up.
#
# This rig has TWO adapters, one per master-follower cluster, and the driver reload
# re-enumerates both. Run scripts/can/install_udev.sh once first, or the kernel's
# can0/can1 can swap between runs and you will drive the wrong cluster.
set -u
BR=1000000

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

CAN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --can) [ $# -ge 2 ] || { echo "--can needs a value"; exit 1; }; CAN="$2"; shift 2 ;;
    --can=*) CAN="${1#--can=}"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown argument: $1"; echo; usage 1 ;;
  esac
done

# `right` and `left` are shorthand for can_right / can_left.
# Pass through any name the kernel actually uses (can_right, but also can0/can1 when
# the udev rule did not apply). Only the `--can right` shorthand gets the prefix.
case "$CAN" in "") ;; can*) ;; *) CAN="can_$CAN" ;; esac

if [ -n "$CAN" ]; then
  [ -e "/sys/class/net/$CAN" ] || {
    echo "No such interface: $CAN"
    echo "Present: $(ip -br link show type can | awk '{print $1}' | tr '\n' ' ')"
    exit 1
  }
  IFACES="$CAN"
else
  IFACES=$(ip -br link show type can | awk '{print $1}')
fi
[ -n "${IFACES// /}" ] || { echo "No CAN interface found. Is an adapter plugged in? lsusb | grep 1d50:606f"; exit 1; }

echo "== interfaces: $(echo $IFACES | tr '\n' ' ') =="
case "$IFACES" in
  *can0*|*can1*) echo "   NOTE: kernel names in use -- run 'sudo bash scripts/can/install_udev.sh' to pin can_left/can_right." ;;
esac

echo
echo "== 1. down =="
# The driver reload re-enumerates BOTH adapters no matter which one you asked for, so
# every interface has to come down first or the unbind can hang on a busy one.
for IF in $(ip -br link show type can | awk '{print $1}'); do ip link set "$IF" down 2>/dev/null; done

echo "== 2. reload the gs_usb driver =="
modprobe -r gs_usb 2>/dev/null; sleep 2; modprobe gs_usb; sleep 2

echo "== 3. set bitrate and bring up =="
# `ip link set <if> up` alone cannot work: a CAN interface has no bitrate until it is set,
# and the up fails. Both commands are required, in this order.
for IF in $IFACES; do
  # Bitrate on its own, and it must succeed -- the interface cannot come up without it.
  ip link set "$IF" type can bitrate $BR || { echo "   $IF: bitrate FAILED"; continue; }

  # Then ask for automatic bus-off recovery, SEPARATELY and best-effort. Without it a single
  # transient TX fault leaves the controller bus-off forever: the arm keeps reporting (RX
  # looks perfectly healthy) while every write fails and the SDK retries, which is thousands
  # of `SendCanMessage(SEND_MESSAGE_FAILED (100017))` lines and a rollout that never moves.
  #
  # The gs_usb adapters on this rig REJECT it ("Device doesn't support restart from Bus Off"),
  # so this must not be chained onto the bitrate command -- doing so fails the whole command
  # and leaves the bus with no bitrate at all. Where it is unsupported, recovery is manual:
  # re-run this script.
  # A CAN interface defaults to txqueuelen 10, which is far too shallow for a USB adapter.
  # gs_usb pays a USB round-trip per frame, and the PiPER SDK sends in bursts (0x151 plus
  # four JointCtrl/GripperCtrl frames per tick, from more than one thread). A 10-deep queue
  # overflows on those bursts, every send then fails with SEND_MESSAGE_FAILED (100017), and
  # the SDK retries instead of backing off -- so the queue never drains and the jam sustains
  # itself. Measured on this rig: paced 150 fps sends 450/450 with zero failures, while a
  # 60-frame burst loses 23. Raising the queue absorbs the bursts.
  ip link set "$IF" txqueuelen 1000 || echo "   $IF: txqueuelen FAILED"

  if ip link set "$IF" type can restart-ms 100 2>/dev/null; then
    RESTART_NOTE=""
  else
    RESTART_NOTE="  (not supported by this adapter -- clear a bus-off by re-running this script)"
  fi
  ip link set "$IF" up || { echo "   $IF: up FAILED"; continue; }
  # Print the bitrate explicitly. `ip -details` puts it on a TAB-indented line, so a
  # space-only pattern silently matches nothing -- and a missing bitrate is exactly the
  # failure this step exists to catch.
  D=$(ip -details link show "$IF")
  LINKSTATE=$(printf '%s\n' "$D" | sed -n '1s/.*state \([A-Z-]*\).*/\1/p')
  CANSTATE=$(printf '%s\n' "$D" | sed -n 's/.*can state \([A-Z-]*\).*/\1/p')
  BITRATE=$(printf '%s\n' "$D" | sed -n 's/.*bitrate \([0-9][0-9]*\).*/\1/p' | head -1)
  RESTART=$(printf '%s\n' "$D" | sed -n 's/.*restart-ms \([0-9][0-9]*\).*/\1/p' | head -1)
  QLEN=$(cat "/sys/class/net/$IF/tx_queue_len" 2>/dev/null)
  printf "   %-10s link=%s  can=%s  bitrate=%s  txqueuelen=%s  restart-ms=%s%s\n" \
    "$IF" "${LINKSTATE:-?}" "${CANSTATE:-?}" "${BITRATE:-*** MISSING ***}" \
    "${QLEN:-?}" "${RESTART:-0}" "$RESTART_NOTE"
done

echo
echo "== 4. measure each bus over 5 s =="
declare -A BEFORE
for IF in $IFACES; do BEFORE[$IF]=$(cat "/sys/class/net/$IF/statistics/rx_packets" 2>/dev/null || echo 0); done
sleep 5
for IF in $IFACES; do
  AFTER=$(cat "/sys/class/net/$IF/statistics/rx_packets" 2>/dev/null || echo 0)
  FPS=$(( (AFTER - ${BEFORE[$IF]}) / 5 ))
  if [ "$FPS" -eq 0 ]; then
    VERDICT="SILENT -- cluster powered off, e-stop engaged, or cable on the other adapter"
  elif [ "$FPS" -lt 1500 ]; then
    VERDICT="LOW -- expected ~2420 idle; run: python scripts/can/bus_scan.py 5 --can $IF"
  else
    VERDICT="OK"
  fi
  printf ">>> %-10s %6d fps   %s\n" "$IF" "$FPS" "$VERDICT"
done
cat <<'REF'

Reference, measured on this rig at 1 Mbps (one master-follower cluster per bus):

     0 fps  bus silent -- nothing powered on that adapter
  ~2420 fps one cluster, master arm at rest. Breaks down as
              0x2A1-0x2A8  8 x 200/s = 1600/s  follower reporting state
              0x251-0x256  6 x 100/s =  600/s  driver info, fast
              0x261-0x266  6 x  20/s =  120/s  driver info, slow
              0x1C0-0x1C3            =  100/s
            Higher while the master arm is being moved: it adds control frames
            (0x155-0x157, 0x159) on top. That is the recording state.

The number that proves the pairing, not the total: 0x2A1 must be ~200/s (one arm
reporting). ~400/s means both arms took the reporting role and the master-slave
pairing is broken -- see TROUBLESHOOTING.md.
REF
