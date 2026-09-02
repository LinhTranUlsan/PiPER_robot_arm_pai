# Resolve each PiPER CAN adapter by its USB serial, whatever the kernel called it.
#
#     source piper/env.sh
#     source piper/scripts/can/can_env.sh
#     echo "$CAN_RIGHT"        # -> can_right, or can0, or whatever it is right now
#
# WHY THIS EXISTS
# A netdev name is global to the machine, so this rig cannot be can_left for one user
# and can0 for another at the same time. Instead of fighting over 80-piper-can.rules,
# resolve the adapter by serial at run time: correct under either naming scheme, and it
# touches nothing outside this shell. Works with or without the udev rule installed.
#
# TO ADD OR REPLACE AN ADAPTER
#   udevadm info -p /sys/class/net/<iface> | grep ID_SERIAL_SHORT
# then add its serial below.

PIPER_CAN_SERIAL_LEFT="${PIPER_CAN_SERIAL_LEFT:-REPLACE-WITH-YOUR-LEFT-ADAPTER-SERIAL}"
PIPER_CAN_SERIAL_RIGHT="${PIPER_CAN_SERIAL_RIGHT:-REPLACE-WITH-YOUR-RIGHT-ADAPTER-SERIAL}"

_piper_iface_for_serial() {
    local want="$1" iface serial
    for iface in $(ls /sys/class/net); do
        [ -e "/sys/class/net/$iface/device" ] || continue
        # ARPHRD_CAN = 280; skip ethernet, wifi, loopback
        [ "$(cat /sys/class/net/$iface/type 2>/dev/null)" = "280" ] || continue
        serial=$(udevadm info -p "/sys/class/net/$iface" 2>/dev/null \
                 | sed -n 's/^E: ID_SERIAL_SHORT=//p')
        [ "$serial" = "$want" ] && { echo "$iface"; return 0; }
    done
    return 1
}

export CAN_LEFT="$(_piper_iface_for_serial "$PIPER_CAN_SERIAL_LEFT")"
export CAN_RIGHT="$(_piper_iface_for_serial "$PIPER_CAN_SERIAL_RIGHT")"

for _side in LEFT RIGHT; do
    eval "_v=\$CAN_$_side"
    if [ -z "$_v" ]; then
        echo "  CAN_$_side : NOT FOUND -- adapter unplugged, or its serial is not listed" >&2
        echo "               in $(dirname "${BASH_SOURCE[0]}")/can_env.sh" >&2
    else
        printf '  CAN_%-5s : %s\n' "$_side" "$_v"
    fi
done
unset _side _v
