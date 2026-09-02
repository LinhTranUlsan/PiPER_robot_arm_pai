#!/usr/bin/env python3
"""Compare the two clusters side by side: what each master commands against where
each follower actually is.

    source piper/scripts/can/can_env.sh      # sets $CAN_LEFT / $CAN_RIGHT by USB serial
    python scripts/check/compare_arms.py
    python scripts/check/compare_arms.py --left can0 --right can_right

Put BOTH master arms in the same physical pose first, then run this. A teach-mode
master is silent until moved, so nudge each one gently if the master columns read --.

READ-ONLY: opens both buses with piper_init=False and never writes.
"""
import argparse
import os
import sys
import time

JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6")
MILLIDEG_TO_DEG = 1e-3
MATCH_TOL_DEG = 2.0


def read(port):
    from piper_sdk import C_PiperInterface_V2, LogLevel

    p = C_PiperInterface_V2(port, judge_flag=False, logger_level=LogLevel.CRITICAL)
    p.ConnectPort(can_init=False, piper_init=False, start_thread=True)
    time.sleep(1.5)
    fb = p.GetArmJointMsgs()
    follower = [getattr(fb.joint_state, n) * MILLIDEG_TO_DEG for n in JOINTS]
    jc = p.GetArmJointCtrl()
    master = ([getattr(jc.joint_ctrl, n) * MILLIDEG_TO_DEG for n in JOINTS]
              if jc.Hz > 0 else None)
    gripper = p.GetArmGripperMsgs().gripper_state.grippers_angle * 1e-3   # mm
    p.DisconnectPort()
    return follower, master, gripper, fb.Hz, jc.Hz


def main():
    # Interface names are global to the machine, so on a shared PC this rig may come up as
    # can0/can1 instead of can_left/can_right. $CAN_LEFT / $CAN_RIGHT (from can_env.sh)
    # resolve it by USB serial; the flags override.
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--left", default=os.environ.get("CAN_LEFT") or "can_left")
    ap.add_argument("--right", default=os.environ.get("CAN_RIGHT") or "can_right")
    args = ap.parse_args()

    print(f"left bus  : {args.left}\nright bus : {args.right}\n")
    lf, lm, lg, l_hz, lm_hz = read(args.left)
    rf, rm, rg, r_hz, rm_hz = read(args.right)

    print(f"feedback Hz : left={l_hz}  right={r_hz}")
    print(f"master   Hz : left={lm_hz}  right={rm_hz}"
          "   (0 = master not transmitting: powered off, or powered and at rest)\n")

    print("           FOLLOWER (measured, deg)      MASTER (commanded, deg)")
    print("  joint      left     right     diff       left     right     diff")
    print("  " + "-" * 62)
    for i, name in enumerate(JOINTS):
        diff = lf[i] - rf[i]
        row = f"  {name:9}{lf[i]:8.2f}{rf[i]:10.2f}{diff:9.2f}"
        if lm and rm:
            row += f"  {lm[i]:9.2f}{rm[i]:10.2f}{lm[i] - rm[i]:9.2f}"
        else:
            row += "        --        --       --"
        print(row + ("   <<<" if abs(diff) > MATCH_TOL_DEG else ""))
    print(f"\n  gripper  {lg:8.2f}{rg:10.2f}{lg - rg:9.2f}   (mm)")

    print("\n" + "=" * 66)
    if not (lm and rm):
        print("  One of the masters is not transmitting control frames.")
        print("  Power both masters on and nudge each one, then run again.")
        return 0

    follower_gap = max(abs(lf[i] - rf[i]) for i in range(6))
    master_gap = max(abs(lm[i] - rm[i]) for i in range(6))
    print(f"  largest gap: FOLLOWER {follower_gap:.2f} deg   MASTER {master_gap:.2f} deg")
    if master_gap > MATCH_TOL_DEG:
        print("  -> THE TWO MASTERS ARE NOT IN THE SAME POSE. Match them, then measure again.")
    elif follower_gap > MATCH_TOL_DEG:
        print("  -> Masters agree but followers do not: THE TWO ARMS HAVE DIFFERENT ZEROS.")
        print("     Joints marked '<<<' are the ones that differ. See DEBUG.md.")
    else:
        print(f"  -> Both arms agree within {MATCH_TOL_DEG:.0f} deg. A height difference is")
        print("     MECHANICAL (bases mounted at different heights), not software.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
