#!/usr/bin/env python3
"""Record what happens on the bus during the first seconds after power-on.

    python scripts/check/powerup_trace.py --can can_left --seconds 30
    # start it FIRST, then switch the arms on

Answers what a still reading cannot: where the follower settles, where the master says it
is, and in which order the two started talking.

If the master starts BEFORE the follower, the follower missed the beginning of the
master-slave handshake and will sit wherever it powered up until the master moves. That is
the difference between landing at home and landing somewhere else.

READ-ONLY: opens the bus with piper_init=False and never writes.
"""
import argparse
import sys
import time

JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6")
MILLIDEG_TO_DEG = 1e-3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--can", required=True)
    ap.add_argument("--seconds", type=float, default=30.0)
    a = ap.parse_args()
    port = a.can if a.can.startswith("can") else "can_" + a.can

    from piper_sdk import C_PiperInterface_V2, LogLevel

    p = C_PiperInterface_V2(port, judge_flag=False, logger_level=LogLevel.CRITICAL)
    p.ConnectPort(can_init=False, piper_init=False, start_thread=True)

    print(f"{port}: listening {a.seconds:.0f}s")
    print("  >>> SWITCH THE ARMS ON NOW (follower first, then master) <<<\n")
    print("   t      follower J1..J6                              master J1..J6")

    t0 = time.perf_counter()
    t_fb = t_ctrl = None
    last = -1.0
    while time.perf_counter() - t0 < a.seconds:
        t = time.perf_counter() - t0
        fb, jc = p.GetArmJointMsgs(), p.GetArmJointCtrl()
        if t_fb is None and fb.Hz > 0:
            t_fb = t
        if t_ctrl is None and jc.Hz > 0:
            t_ctrl = t
        if t - last >= 1.0:
            last = t
            f = " ".join(f"{getattr(fb.joint_state, n)*MILLIDEG_TO_DEG:6.1f}" for n in JOINTS) \
                if fb.Hz > 0 else "        -- no feedback --        "
            m = " ".join(f"{getattr(jc.joint_ctrl, n)*MILLIDEG_TO_DEG:6.1f}" for n in JOINTS) \
                if jc.Hz > 0 else "     -- master silent --"
            print(f"  {t:5.1f}s  {f}   {m}")
        time.sleep(1 / 30)

    fb, jc = p.GetArmJointMsgs(), p.GetArmJointCtrl()
    print("\n  --- timeline ---")
    print(f"    follower feedback started : "
          + (f"t={t_fb:.2f}s" if t_fb is not None else "NEVER (not powered / e-stop)"))
    print(f"    master control started    : "
          + (f"t={t_ctrl:.2f}s" if t_ctrl is not None else "NEVER (powered off, or never moved)"))

    if t_fb is not None and t_ctrl is not None:
        d = t_ctrl - t_fb
        print(f"    master started {abs(d):.2f}s {'AFTER' if d > 0 else 'BEFORE'} the follower")
        if d < 0:
            print("    -> MASTER CAME UP FIRST. Switch the FOLLOWER on first, wait, then the master.")
        gap = max(abs(getattr(fb.joint_state, n) - getattr(jc.joint_ctrl, n)) * MILLIDEG_TO_DEG
                  for n in JOINTS)
        print(f"\n    final follower-vs-master gap: {gap:.2f} deg"
              + ("   <<< NOT SYNCED -- nudge the master and it should close" if gap > 3 else "   (synced)"))
    p.DisconnectPort()
    return 0


if __name__ == "__main__":
    sys.exit(main())
