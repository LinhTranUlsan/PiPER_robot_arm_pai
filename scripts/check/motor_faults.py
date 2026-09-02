#!/usr/bin/env python3
"""Read every joint driver's fault flags. Run this when a driver refuses to enable.

    python scripts/check/motor_faults.py --can can_right

READ-ONLY: opens the bus with piper_init=False and never writes.
"""
import argparse, sys, time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--can", required=True)
    a = ap.parse_args()
    port = a.can if a.can.startswith("can") else "can_" + a.can

    from piper_sdk import C_PiperInterface_V2, LogLevel
    p = C_PiperInterface_V2(port, judge_flag=False, logger_level=LogLevel.CRITICAL)
    p.ConnectPort(can_init=False, piper_init=False, start_thread=True)
    time.sleep(1.5)

    print(f"enable status : {p.GetArmEnableStatus()}   (False = driver NOT enabled)")
    st = p.GetArmStatus().arm_status
    for f in ("ctrl_mode", "arm_status", "mode_feed", "teach_status",
              "motion_status", "err_code"):
        if hasattr(st, f):
            print(f"{f:14}: {getattr(st, f)}")
    print("\n-- per-driver low-speed info (voltage / current / temperature / fault) --")
    low = p.GetArmLowSpdInfoMsgs()
    for i in range(1, 7):
        m = getattr(low, f"motor_{i}", None)
        if m is None:
            continue
        bits = {k: v for k, v in vars(m).items() if not k.startswith("_")}
        print(f"  J{i}: " + "  ".join(f"{k}={v}" for k, v in bits.items()))
    p.DisconnectPort()
    return 0


if __name__ == "__main__":
    sys.exit(main())
