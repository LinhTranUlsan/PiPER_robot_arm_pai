#!/usr/bin/env python3
"""Where each joint is, what the master is asking for, and the firmware limits.

    python scripts/check/joint_limits.py --can can_left
    python scripts/check/joint_limits.py --can can_left --watch 20

Use it when a follower will not travel as far as the master: it separates
  "the firmware limit stops it"  from  "the follower is not tracking".

Move the master to the position the follower refuses to reach, then read the table.

READ-ONLY: opens the bus with piper_init=False, sends only parameter queries, never a
motion command.
"""
import argparse
import math
import sys
import time

JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6")
MILLIDEG_TO_DEG = 1e-3
LIMIT_TO_DEG = 0.1          # firmware angle limits are in 0.1 deg
NEAR = 3.0                  # deg from a limit before we call it "AT LIMIT"


def read_limits(p):
    """Query motors 1..6 one at a time; the SDK keeps only the most recent reply."""
    out = {}
    for j in range(1, 7):
        p.SearchMotorMaxAngleSpdAccLimit(j, 0x01)
        time.sleep(0.25)
        m = p.GetCurrentMotorAngleLimitMaxVel().current_motor_angle_limit_max_vel
        if getattr(m, "motor_num", None) == j:
            out[j] = (m.max_angle_limit * LIMIT_TO_DEG, m.min_angle_limit * LIMIT_TO_DEG)
    return out


def snapshot(p):
    fb = p.GetArmJointMsgs()
    fol = [getattr(fb.joint_state, n) * MILLIDEG_TO_DEG for n in JOINTS]
    jc = p.GetArmJointCtrl()
    mas = ([getattr(jc.joint_ctrl, n) * MILLIDEG_TO_DEG for n in JOINTS]
           if jc.Hz > 0 else None)
    return fol, mas


def table(fol, mas, lim):
    print("  joint   follower    master     gap   firmware limit        headroom")
    print("  " + "-" * 70)
    for i, name in enumerate(JOINTS):
        j = i + 1
        f = fol[i]
        m = f"{mas[i]:8.2f}" if mas else "      --"
        gap = f"{mas[i] - f:7.2f}" if mas else "     --"
        if j in lim:
            hi, lo = lim[j]
            head = f"{f - lo:6.1f} down / {hi - f:6.1f} up"
            mark = ""
            if f - lo < NEAR:
                mark = "  <<< AT LOWER LIMIT"
            elif hi - f < NEAR:
                mark = "  <<< AT UPPER LIMIT"
            print(f"  {name:8}{f:8.2f}  {m}  {gap}   [{lo:7.1f} .. {hi:6.1f}]  {head}{mark}")
        else:
            print(f"  {name:8}{f:8.2f}  {m}  {gap}   [ limit query timed out ]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--can", required=True)
    ap.add_argument("--watch", type=float, default=0.0,
                    help="keep printing the min/max reached over N seconds")
    a = ap.parse_args()
    port = a.can if a.can.startswith("can") else "can_" + a.can

    from piper_sdk import C_PiperInterface_V2, LogLevel

    p = C_PiperInterface_V2(port, judge_flag=False, logger_level=LogLevel.CRITICAL)
    p.ConnectPort(can_init=False, piper_init=False, start_thread=True)
    time.sleep(1.0)

    print(f"{port}: reading firmware angle limits (about 2 s) ...\n")
    lim = read_limits(p)
    fol, mas = snapshot(p)
    if mas is None:
        print("  master not transmitting -- move it to see the 'master' and 'gap' columns\n")
    table(fol, mas, lim)

    if a.watch > 0:
        print(f"\n  watching {a.watch:.0f}s -- MOVE THE MASTER through the range the follower refuses\n")
        lo_f = list(fol); hi_f = list(fol)
        lo_m = list(fol); hi_m = list(fol); seen_master = False
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < a.watch:
            f, m = snapshot(p)
            for i in range(6):
                lo_f[i] = min(lo_f[i], f[i]); hi_f[i] = max(hi_f[i], f[i])
                if m:
                    seen_master = True
                    lo_m[i] = min(lo_m[i], m[i]); hi_m[i] = max(hi_m[i], m[i])
            time.sleep(1 / 30)
        print("  joint    follower reached      master asked for     shortfall")
        print("  " + "-" * 66)
        for i, name in enumerate(JOINTS):
            fr = f"[{lo_f[i]:7.2f} ..{hi_f[i]:7.2f}]"
            mr = f"[{lo_m[i]:7.2f} ..{hi_m[i]:7.2f}]" if seen_master else "        --"
            short = ""
            if seen_master:
                d = max(lo_f[i] - lo_m[i], hi_m[i] - hi_f[i])
                if d > 2.0:
                    short = f"  {d:5.1f} deg  <<<"
            print(f"  {name:8} {fr}  {mr}{short}")
        if not seen_master:
            print("\n  The master never transmitted. Power it on and move it during the watch.")

    p.DisconnectPort()
    return 0


if __name__ == "__main__":
    sys.exit(main())
