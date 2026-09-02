#!/usr/bin/env python3
"""Replay exactly what send_action() puts on the bus, one component at a time.

    python scripts/can/send_probe.py --can can_right --mode joint     # JointCtrl only
    python scripts/can/send_probe.py --can can_right --mode gripper   # GripperCtrl only
    python scripts/can/send_probe.py --can can_right --mode both      # what a rollout sends

Commands the arm's CURRENT pose, so it holds still. Isolates which SDK call drives
the CAN controller to bus-off.
"""
import argparse, math, subprocess, sys, time
from pathlib import Path

RAD_PER_MILLIDEG = math.pi / 180.0 / 1000.0
JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6")


def counters(port):
    out = subprocess.run(["ip", "-details", "-statistics", "link", "show", port],
                         capture_output=True, text=True).stdout.split()
    i = out.index("re-started")
    return {n: int(v) for n, v in zip(out[i:i + 6], out[i + 6:i + 12])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--can", required=True)
    ap.add_argument("--mode", choices=["joint", "gripper", "both"], default="both")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--cameras", action="store_true",
                    help="stream $FRONT and $WRIST_RIGHT while probing, to reproduce the "
                         "USB load a real rollout puts on the host controller")
    a = ap.parse_args()
    port = a.can if a.can.startswith("can") else "can_" + a.can

    from piper_sdk import C_PiperInterface_V2, LogLevel
    p = C_PiperInterface_V2(port, judge_flag=False, logger_level=LogLevel.CRITICAL)
    p.ConnectPort(can_init=False, piper_init=False, start_thread=True)
    time.sleep(1.0)

    p.EnableArm(7)
    for _ in range(50):
        if all(p.GetArmEnableStatus()):
            break
        time.sleep(0.1)
    else:
        sys.exit("drivers not enabled")
    p.MotionCtrl_2(0x01, 0x01, 50, 0x00)
    time.sleep(0.1)

    js = p.GetArmJointMsgs().joint_state
    hold = [int(getattr(js, n)) for n in JOINTS]              # milli-degrees, as read
    grip = int(p.GetArmGripperMsgs().gripper_state.grippers_angle)

    cams, stop = [], []
    if a.cameras:
        import os, threading
        import cv2
        for env in ("FRONT", "WRIST_RIGHT"):
            dev = os.environ.get(env)
            if not dev:
                sys.exit(f"${env} not set -- run: source piper/env.sh")
            c = cv2.VideoCapture(dev, cv2.CAP_V4L2)
            c.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            c.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            c.set(cv2.CAP_PROP_FPS, 30)
            if not c.isOpened():
                sys.exit(f"cannot open {env} at {dev}")
            cams.append(c)
        stop.append(False)

        def pump(cap):
            while not stop[0]:
                cap.read()

        for c in cams:
            threading.Thread(target=pump, args=(c,), daemon=True).start()
        time.sleep(1.0)
        print(f"  streaming {len(cams)} cameras at 640x480x30 during the probe")

    before, txp = counters(port), Path(f"/sys/class/net/{port}/statistics/tx_packets")
    tx0 = int(txp.read_text())
    n = int(a.seconds * a.fps)
    print(f"{port}: mode={a.mode}  {n} ticks @ {a.fps:.0f} Hz  (holding current pose)")
    t0 = time.perf_counter()
    for i in range(n):
        if a.mode in ("joint", "both"):
            p.JointCtrl(*hold)
        if a.mode in ("gripper", "both"):
            p.GripperCtrl(abs(grip), 1000, 0x01, 0)
        nxt = t0 + (i + 1) / a.fps
        while time.perf_counter() < nxt:
            pass
    if cams:
        stop[0] = True
        time.sleep(0.3)
        for c in cams:
            c.release()
    time.sleep(0.5)

    after, tx1 = counters(port), int(txp.read_text())
    grew = {k: after[k] - before[k] for k in after if after[k] > before[k]}
    print(f"  tx_packets : {tx0} -> {tx1}  (+{tx1 - tx0})")
    print(f"  controller : {grew or 'no new errors'}")
    fatal = {k: v for k, v in grew.items() if k in ("error-pass", "bus-off", "bus-errors")}
    print("  VERDICT: " + ("BUS-OFF -- this call is the problem" if fatal else "CLEAN"))
    p.DisconnectPort()
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
