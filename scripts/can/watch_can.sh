#!/usr/bin/env bash
# Sample a CAN interface's counters while something else drives the bus.
#
#     bash scripts/can/watch_can.sh can_right 60      # watch for 60 s
#
# Run this in a second terminal, then start the rollout or park_arm in the first. It prints a
# line whenever anything changes, so the moment a write path breaks is visible next to what
# the other process was doing -- which is the only way to tell a transport fault from an arm
# fault after the fact. Cumulative counters alone cannot: a large bus-off total says it
# happened, not that it is happening.
set -u
IF="${1:-can_right}"; DUR="${2:-60}"
S=/sys/class/net/$IF/statistics
[ -d "$S" ] || { echo "No such interface: $IF"; exit 1; }

hdr() { printf '%8s %10s %10s %8s %8s  %-13s %s\n' \
        time tx_pkts rx_pkts tx_drop tx_err state "warn/pass/busoff"; }
hdr
prev=""
t0=$(date +%s)
while [ $(( $(date +%s) - t0 )) -lt "$DUR" ]; do
  d=$(ip -details -statistics link show "$IF" 2>/dev/null) || break
  st=$(printf '%s\n' "$d" | sed -n 's/.*can state \([A-Z-]*\).*/\1/p' | head -1)
  ctr=$(printf '%s\n' "$d" | sed -n '/re-started/{n;p}' | awk '{print $4"/"$5"/"$6}')
  cur="$(cat $S/tx_packets) $(cat $S/rx_packets) $(cat $S/tx_dropped) $(cat $S/tx_errors) $st $ctr"
  if [ "$cur" != "$prev" ]; then
    printf '%8s %10s %10s %8s %8s  %-13s %s\n' "$(date +%H:%M:%S)" $cur
    prev="$cur"
  fi
  sleep 0.2
done
