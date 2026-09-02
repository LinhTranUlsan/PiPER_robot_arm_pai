#!/usr/bin/env bash
# Set the CPU governor to 'performance' and keep it across reboots.
#
#     sudo bash ~/linh/PiPER/PiPER_PAI/pi0_piper/scripts/set_cpu_performance.sh
#
# Why: the 'powersave' governor pins the clock near its floor -- 1200 of 4600 MHz on the
# original Xeon W-2235; the same trap applies to any CPU here. A 30 Hz
# control loop does a short burst of work then sleeps 25 ms, so average
# utilisation looks very low and the governor never ramps up. Ticks miss their
# deadline even at load average 0.4, and the robot shakes because commands
# reach the arm unevenly.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Must run with sudo:  sudo bash $0" >&2
  exit 1
fi

echo "== before =="
grep "cpu MHz" /proc/cpuinfo | awk '{s+=$4; n++} END {printf "   %.0f MHz average\n", s/n}'
echo "   governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"

echo
echo "== setting governor = performance =="
cpupower frequency-set -g performance >/dev/null
sleep 1

echo "== installing service so it survives reboot =="
cat > /etc/systemd/system/cpu-performance.service <<'EOF'
[Unit]
Description=Set CPU governor to performance
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/bin/cpupower frequency-set -g performance

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable cpu-performance.service >/dev/null 2>&1
echo "   /etc/systemd/system/cpu-performance.service  enabled"

echo
echo "== after =="
grep "cpu MHz" /proc/cpuinfo | awk '{s+=$4; n++} END {printf "   %.0f MHz average\n", s/n}'
echo "   governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"
echo
echo "Done. To revert:   sudo systemctl disable --now cpu-performance.service"
echo "                   sudo cpupower frequency-set -g powersave"
