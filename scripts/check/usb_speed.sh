#!/usr/bin/env bash
# Link speed per camera. 5000+ = USB 3.0 · 480 = USB 2.0 cable or port.
#
#     bash scripts/check/usb_speed.sh
# A D435 exposes 5-6 video nodes, so dedupe by USB port or every camera prints six times.
for v in /sys/class/video4linux/video*; do
  p=$(readlink -f "$v/device") || continue
  while [ -n "$p" ] && [ "$p" != "/" ] && [ ! -f "$p/speed" ]; do p=$(dirname "$p"); done
  [ -f "$p/speed" ] || continue
  port=$(basename "$p")
  case " $seen " in *" $port "*) continue ;; esac
  seen="$seen $port"
  s=$(cat "$p/speed")
  case "$s" in
    480)              verdict="USB 2.0   <-- doi cap" ;;
    5000|10000|20000) verdict="USB 3.0   OK" ;;
    *)                verdict="?" ;;
  esac
  printf "  %-10s %6s Mbps   %s\n" "$port" "$s" "$verdict"
done
