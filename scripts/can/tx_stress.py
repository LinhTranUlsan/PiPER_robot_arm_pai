#!/usr/bin/env python3
"""Drive the TX path at rollout load without touching the robot.

    python scripts/can/tx_stress.py --can can_right [--seconds 20] [--fps 150]

A rollout writes 5 frames per 30 Hz tick = 150 frames/s. preflight's 20-frame probe
passes on a bus that dies under that load, so this reproduces the load instead.

Sends zero-length frames on ID 0x7FF. PiPER's highest ID is 0x4AF, so the arm ignores
them: nothing is commanded, nothing moves.
"""
import argparse, subprocess, sys, time
from pathlib import Path


def counters(port):
    out = subprocess.run(["ip", "-details", "-statistics", "link", "show", port],
                         capture_output=True, text=True).stdout.split()
    if "re-started" not in out:
        return {}
    i = out.index("re-started")
    return {n: int(v) for n, v in zip(out[i:i + 6], out[i + 6:i + 12])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--can", required=True)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--fps", type=float, default=150.0)
    ap.add_argument("--id", default="0x7FF",
                    help="CAN ID to send on. Default 0x7FF (unused by PiPER, safe). "
                         "Use 0x155 to test whether another node owns the control IDs.")
    a = ap.parse_args()
    port = a.can if a.can.startswith("can") else "can_" + a.can

    import can
    txp = Path(f"/sys/class/net/{port}/statistics/tx_packets")
    before, tx0 = counters(port), int(txp.read_text())
    bus = can.interface.Bus(channel=port, interface="socketcan")
    msg = can.Message(arbitration_id=int(a.id, 0), data=b"", is_extended_id=False)

    n = int(a.seconds * a.fps)
    period, failed, t0 = 1.0 / a.fps, 0, time.perf_counter()
    print(f"{port}: sending {n} frames at {a.fps:.0f}/s for {a.seconds:.0f}s ...")
    for i in range(n):
        try:
            bus.send(msg, timeout=0.1)
        except can.CanError:
            failed += 1
        nxt = t0 + (i + 1) * period
        while time.perf_counter() < nxt:
            pass
    bus.shutdown()
    time.sleep(0.5)

    tx1, after = int(txp.read_text()), counters(port)
    grew = {k: after[k] - before[k] for k in after if after[k] > before.get(k, 0)}
    print(f"  send() errors : {failed}/{n}")
    print(f"  tx_packets    : {tx0} -> {tx1}  (+{tx1 - tx0})")
    print(f"  controller    : {grew or 'no new errors'}")
    # A stray error-warn is a transient, not a broken bus. Only error-pass/bus-off
    # (or frames that never went out) mean the transmit path is actually failing.
    fatal = {k: v for k, v in grew.items() if k in ("error-pass", "bus-off", "bus-errors")}
    bad = failed or fatal or (tx1 - tx0) < n * 0.9
    print("  VERDICT: " + ("TX PATH BROKEN under load" if bad else "TX PATH HEALTHY under load"))
    if grew and not fatal:
        print("           (transient error-warn only -- not a fault)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
