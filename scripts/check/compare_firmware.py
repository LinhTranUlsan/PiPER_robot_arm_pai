#!/usr/bin/env python3
"""Compare firmware version and gripper status between the two arms.

    python scripts/check/compare_firmware.py

Per-joint acceleration/speed limits are deliberately NOT compared here: the SDK returns
them one motor at a time, and they cover J1-J6 only -- the gripper is a separate motor and
none of those limits apply to it.

READ-ONLY: sends only the SDK's query requests, never a motion command.
"""
import sys
import time

# status_code bits, from arm_feedback_gripper.py
BITS = [
    (0, "voltage too low"),
    (1, "motor over-temperature"),
    (2, "driver over-current"),
    (3, "driver over-temperature"),
    (4, "sensor abnormal"),
    (5, "driver error"),
]


def read(port):
    from piper_sdk import C_PiperInterface_V2, LogLevel

    p = C_PiperInterface_V2(port, judge_flag=False, logger_level=LogLevel.CRITICAL)
    p.ConnectPort(can_init=False, piper_init=False, start_thread=True)
    time.sleep(0.8)
    p.SearchPiperFirmwareVersion()
    time.sleep(1.5)
    fw = p.GetPiperFirmwareVersion()
    g = p.GetArmGripperMsgs().gripper_state
    p.DisconnectPort()
    return fw, g.grippers_angle, g.grippers_effort, g.status_code


def describe(code):
    faults = [name for bit, name in BITS if code & (1 << bit)]
    extra = ("driver enabled" if code & 0x40 else "driver disabled")
    extra += ", zeroed" if code & 0x80 else ", NEVER ZEROED"
    return (", ".join(faults) if faults else "no fault") + f" ({extra})"


def main():
    rows = [(side, *read(port)) for side, port in (("left", "can_left"), ("right", "can_right"))]
    for side, fw, angle, effort, code in rows:
        print(f"{side:6} firmware {str(fw):12}  gripper {angle/1000:7.2f} mm"
              f"  effort {effort:5}  status 0x{code:02X}")
        print(f"       {describe(code)}")
    print()
    fws = {r[1] for r in rows}
    if len(fws) > 1 and all(isinstance(f, str) for f in fws):
        print("  FIRMWARE VERSIONS DIFFER -- that alone can change behaviour.")
    elif any(not isinstance(r[1], str) for r in rows):
        print("  A version query did not answer (numeric result = error code). Rerun;")
        print("  if it keeps failing on the same arm, that arm is not answering queries.")
    else:
        print("  Firmware versions match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
