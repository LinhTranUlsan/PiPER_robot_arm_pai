#!/usr/bin/env bash
# Turn a fresh clone into a runnable checkout on THIS machine.
#
#     bash scripts/setup/bootstrap.sh
#
# Three files are deliberately gitignored because they describe one machine: env.sh and
# env_all.sh (camera by-paths, which embed the PCI id and each USB port) and the CAN
# adapter serials. This creates them from what is actually plugged in.
#
# Camera ROLES still need you: only shaking an arm can tell a wrist camera from a fixed
# one, so the script stops and hands you that command.
#
# Safe to re-run: it never overwrites a file you already have.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

say "1. CAN adapters"
mapfile -t IFACES < <(ls /sys/class/net 2>/dev/null | while read -r i; do
    [ "$(cat "/sys/class/net/$i/type" 2>/dev/null)" = "280" ] && echo "$i"
done)
if [ "${#IFACES[@]}" -eq 0 ]; then
    echo "   no CAN interface found. Plug the adapters in, then:"
    echo "     sudo bash scripts/can/fix_can.sh"
    echo "     bash scripts/setup/bootstrap.sh"
else
    declare -A SERIAL
    for i in "${IFACES[@]}"; do
        s=$(udevadm info -p "/sys/class/net/$i" 2>/dev/null | sed -n 's/^E: ID_SERIAL_SHORT=//p')
        SERIAL["$i"]="$s"
        printf '   %-12s serial %s\n' "$i" "${s:-<none>}"
    done
    # If udev already named them, the side is known and needs no guessing.
    LEFT="${SERIAL[can_left]:-}"; RIGHT="${SERIAL[can_right]:-}"
    if [ -z "$LEFT" ] || [ -z "$RIGHT" ]; then
        echo
        echo "   Interfaces are not named can_left / can_right, so which adapter drives which"
        echo "   cluster cannot be derived. Either install the udev rule:"
        echo "     # put the serials above into scripts/can/80-piper-can.rules"
        echo "     sudo bash scripts/can/install_udev.sh && sudo bash scripts/can/fix_can.sh"
        echo "   or export the two serials by hand before sourcing can_env.sh."
    fi
fi

say "2. env.sh  (camera paths)"
if [ -f env.sh ]; then
    echo "   already present -- left alone"
else
    cp env.sh.example env.sh
    echo "   created from env.sh.example -- the paths are still REPLACE-ME"
fi

say "3. env_all.sh  (optional overview camera + CAN serials)"
if [ -f env_all.sh ]; then
    echo "   already present -- left alone"
else
    cp env_all.sh.example env_all.sh
    echo "   created from env_all.sh.example"
fi
# Record the serials here: env_all.sh is gitignored, so they never reach the repo, and
# can_env.sh honours PIPER_CAN_SERIAL_* over its own placeholders.
for side in LEFT RIGHT; do
    var="PIPER_CAN_SERIAL_$side"
    eval "val=\${${side}:-}"
    [ -n "$val" ] || continue
    if grep -q "^export $var=" env_all.sh; then
        sed -i "s|^export $var=.*|export $var=\"$val\"|" env_all.sh
        echo "   updated $var"
    else
        sed -i "1i export $var=\"$val\"   # written by bootstrap.sh" env_all.sh
        echo "   wrote   $var"
    fi
done

say "4. Cameras"
python scripts/setup/detect_cameras.py || true
cat <<'NOTE'

   Roles cannot be auto-detected -- a wrist camera is identified by shaking its arm:

     python scripts/setup/identify_cameras.py
     python scripts/setup/detect_cameras.py --front F --wrist-right R --wrist-left L --write

   With a 4th overview camera, TWO cameras report NO RESPONSE (the fixed one and the
   overview one). Open camera_id_frames/ and tell them apart by eye, then put the
   overview camera's by-path into ALL= in env_all.sh.
NOTE

say "Done"
cat <<'NOTE'
   source env_all.sh                    # pulls in env.sh, adds $ALL and the CAN serials
   source scripts/can/can_env.sh        # -> $CAN_LEFT / $CAN_RIGHT
   echo "$CAN_RIGHT"; echo "$CAMS_RIGHT_ALL"

   Both must print. Then: python scripts/check/preflight.py --teleop --can $CAN_RIGHT
NOTE
