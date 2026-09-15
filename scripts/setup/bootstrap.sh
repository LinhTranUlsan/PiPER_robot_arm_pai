#!/usr/bin/env bash
# Turn a fresh clone into a runnable checkout on THIS machine.
#
#     bash scripts/setup/bootstrap.sh
#     bash scripts/setup/bootstrap.sh --from ~/old/checkout    # reuse a working setup
#
# Three files are deliberately gitignored because each describes one machine: env.sh and
# env_all.sh (camera by-paths, which embed the PCI id and every USB port) and the CAN
# adapter serials. A fresh clone has none of them, so every session block fails on its
# first line. This builds them from what is actually plugged in.
#
# Camera ROLES still need you: only shaking an arm tells a wrist camera from a fixed one.
# --from skips that by importing env.sh / env_all.sh from a checkout you already set up.
#
# Safe to re-run, and re-running is the fix after you copy env files in from elsewhere:
# that overwrites the CAN serials this script wrote, and this puts them back.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
FROM=""
while [ $# -gt 0 ]; do
    case "$1" in
        --from) FROM="${2:?--from needs a directory}"; shift 2 ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done
say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

say "1. CAN adapters"
declare -A SERIAL
for i in $(ls /sys/class/net 2>/dev/null); do
    # ARPHRD_CAN = 280
    [ "$(cat "/sys/class/net/$i/type" 2>/dev/null)" = "280" ] || continue
    s=$(udevadm info -p "/sys/class/net/$i" 2>/dev/null | sed -n 's/^E: ID_SERIAL_SHORT=//p')
    SERIAL["$i"]="$s"
    printf '   %-12s serial %s\n' "$i" "${s:-<none>}"
done
LEFT="${SERIAL[can_left]:-}"; RIGHT="${SERIAL[can_right]:-}"
if [ "${#SERIAL[@]}" -eq 0 ]; then
    echo "   none found. Plug the adapters in, then: sudo bash scripts/can/fix_can.sh"
elif [ -z "$LEFT" ] || [ -z "$RIGHT" ]; then
    echo
    echo "   Not named can_left / can_right, so which adapter drives which cluster cannot"
    echo "   be derived. Put the serials above into scripts/can/80-piper-can.rules, then:"
    echo "     sudo bash scripts/can/install_udev.sh && sudo bash scripts/can/fix_can.sh"
    echo "   and re-run this script."
fi

say "2. env.sh and env_all.sh"
for f in env.sh env_all.sh; do
    if [ -n "$FROM" ] && [ -f "$FROM/$f" ]; then
        cp "$FROM/$f" "$f"
        echo "   $f imported from $FROM"
    elif [ -f "$f" ]; then
        echo "   $f already present -- left alone"
    else
        cp "$f.example" "$f"
        echo "   $f created from $f.example"
    fi
done
sed -i "s|^export PIPER_REPO=.*|export PIPER_REPO=\"$REPO\"|" env.sh 2>/dev/null || true

# The serials go in env_all.sh, which is gitignored, so they never reach the repo.
# can_env.sh honours PIPER_CAN_SERIAL_* over its own placeholders.
for side in LEFT RIGHT; do
    eval "val=\${$side:-}"; [ -n "$val" ] || continue
    var="PIPER_CAN_SERIAL_$side"
    if grep -q "^export $var=" env_all.sh; then
        sed -i "s|^export $var=.*|export $var=\"$val\"|" env_all.sh
    else
        sed -i "1i export $var=\"$val\"   # written by bootstrap.sh" env_all.sh
    fi
    echo "   $var recorded in env_all.sh"
done

say "3. Verify"
STATUS=0
# A subshell: sourcing here must not leak into the caller's environment.
eval "$(
  bash -c '
    source env_all.sh >/dev/null 2>&1 || true
    source scripts/can/can_env.sh >/dev/null 2>&1 || true
    for v in FRONT WRIST_RIGHT WRIST_LEFT ALL CAN_LEFT CAN_RIGHT; do
      printf "%s=%q\n" "V_$v" "${!v}"
    done'
)"
for v in FRONT WRIST_RIGHT WRIST_LEFT; do
    eval "p=\$V_$v"
    if [ -z "$p" ] || [ "${p#*REPLACE-ME}" != "$p" ]; then
        printf '   %-12s NOT SET -- camera roles still need assigning\n' "$v"; STATUS=1
    elif [ ! -e "$p" ]; then
        printf '   %-12s set, but that path does not exist on this machine\n' "$v"; STATUS=1
    else
        printf '   %-12s ok\n' "$v"
    fi
done
if [ -z "$V_ALL" ] || [ "${V_ALL#*REPLACE-ME}" != "$V_ALL" ]; then
    printf '   %-12s not configured (optional overview camera -- fine to leave)\n' "ALL"
elif [ ! -e "$V_ALL" ]; then
    printf '   %-12s set, but that path does not exist on this machine\n' "ALL"; STATUS=1
else
    printf '   %-12s ok\n' "ALL"
fi
for v in CAN_LEFT CAN_RIGHT; do
    eval "p=\$V_$v"
    if [ -z "$p" ]; then printf '   %-12s NOT RESOLVED\n' "$v"; STATUS=1
    else printf '   %-12s -> %s\n' "$v" "$p"; fi
done

if [ "$STATUS" -eq 0 ]; then
    say "READY"
    cat <<'NOTE'
   source env_all.sh && source scripts/can/can_env.sh
   python scripts/check/preflight.py --teleop --can $CAN_RIGHT
NOTE
else
    say "NOT READY YET"
    python scripts/setup/detect_cameras.py 2>/dev/null || true
    cat <<'NOTE'

   Assign the camera roles -- shaking an arm is the only way to tell a wrist camera
   from a fixed one:

     python scripts/setup/identify_cameras.py
     python scripts/setup/detect_cameras.py --front F --wrist-right R --wrist-left L --write

   With a 4th overview camera, TWO of them report NO RESPONSE (the fixed one and the
   overview one, since neither rides on an arm). Open camera_id_frames/, tell them apart
   by eye, then put the overview camera's by-path into ALL= in env_all.sh.

   Already have a working checkout? Import it instead:
     bash scripts/setup/bootstrap.sh --from /path/to/that/checkout

   Then run this script again.
NOTE
fi
exit "$STATUS"
