#!/usr/bin/env python3
"""Move the arm to a named pose over CAN, without running any policy.

    python scripts/deploy/park_arm.py --park           # (0, 0, 0, 0, 30, 0) deg
    python scripts/deploy/park_arm.py --home           # (0, 0, 0, 0,  0, 0) deg
    python scripts/deploy/park_arm.py --look           # camera over the table, 25 deg tilt
    python scripts/deploy/park_arm.py --look-near      # nearly straight down, 5 deg
    python scripts/deploy/park_arm.py --usd-home       # the .usd pose, no wrist tilt
    python scripts/deploy/park_arm.py --pose 0 0 0 0 30 0

Both poses are printed with the wrist position every run, so you can see at a
glance which one you asked for. Measured with CalFK:

    --home       (0, 0, 0, 0, 30, 0) deg     link6 [ 48, 0, 167] mm
    --usd-home   (0, 0, 0, 0,  0, 0) deg     link6 [ 56, 0, 213] mm
    --look       (0, 40, -50, 0, 60, 0) deg  link6 [109, 0, 356] mm

home and usd-home differ ONLY by the 30 deg wrist tilt, so they look similar --
read the printed angles, not the arm.

Which pose is "home", and which is "park"?
------------------------------------------
They are two different poses and only one of them is a free choice.

    --home    (0, 0, 0, 0,  0, 0) deg    where every episode STARTS
    --park    (0, 0, 0, 0, 30, 0) deg    where a deploy run ENDS

HOME IS NOT NEGOTIABLE. It is the USD's own pose -- every arm joint in the asset
carries drive:angular:physics:targetPosition = 0.0 -- and the policy's whole frame
is anchored to it: the observation is q - default_joint_pos. Move it and every
checkpoint reads a state it never trained on. The policy in checkpoints/ scored
98.68% anchored here. A run must BEGIN here.

PARK IS FREE. The ramp to it runs after the policy has stopped, so nothing is
anchored to it and changing it costs no retraining. The 30 deg tilt on joint5
points the wrist D435 at the table, so a run ENDS with the camera already looking
where the next detection has to come from.

Neither is hard-coded here. --home reads default_joint_pos and --park reads
park_joint_pos, both out of deploy_spec.json, which export_deploy_spec.py writes
from the sim -- so this script cannot disagree with what the policy trained
around. RE-EXPORT THE SPEC after changing either pose in robots/piper.py, or these
flags will send the arm to the previous one.

--home and --usd-home are the same pose right now; --usd-home is kept as the name
that says what it is. Both put joint2 and joint3 exactly on a mechanical stop:

    joint2 limits [   0, 180] deg   ->  0 is exactly its LOWER limit
    joint3 limits [-170,   0] deg   ->  0 is exactly its UPPER limit

That is fine to START from -- the object is forward and down, so both joints move
INTO their range to reach it. It would NOT be fine as the action offset, because a
zero action sitting on a stop wastes half of that joint's range; that is why
PIPER_ACTION_OFFSET is a separate constant at mid-range. See robots/piper.py for
the measured wasted-range table.

Why this script exists at all: the arm ends every deploy run at home, and the
object is usually not visible from there, so the next run cannot latch a
detection. This parks the arm wherever you need it. It is also the answer to "is
there an SDK command to go home" -- there is `ReqMasterArmMoveToHome` (CAN 0x191),
but it is a MASTER-SLAVE command, it goes to the mechanical zero rather than the
training home pose, and its mode 0 restores master-slave mode, which is the class
of command that retargets both arms on a shared bus. Interpolating JointCtrl is
the safe route.

Same safety as piper_deploy_onnx.py: clamped to the joint limits, rate limited,
and the command is never allowed to run far ahead of the measured pose.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

DEG_PER_RAD_MILLI = 180000.0 / math.pi
RAD_PER_MILLI_DEG = math.pi / 180000.0

# COMPUTED, not eyeballed, by scripts/tools/find_look_pose.py -- which projects the
# spawn fan through the real D435 intrinsics using the camera_link body pose out of
# the articulation, the same chain detect_object_d435.py uses.
#
# What the old pose (-2.1, 0, 0, 0, 22.4, -5.9) cost: it had the best raw coverage
# of anything tried, 31.1%, but 53.5 DEGREES OF TILT from straight down. At that
# angle a 3.6 cm box shows its side rather than its top face, so the depth blob is a
# sliver and its centroid sits on the near edge. Measured consequence -- an object
# tape-measured at (0.350, -0.140, 0.018) read as (0.273, -0.042, 0.028), off by
# 0.125 m, with the blob only 0.014 m across where the object is 0.028-0.036.
# The detector's own message blamed the optical convention. It was the pose.
#
#   pose                          coverage   tilt   height   sees on the table
#   (-2.1, 0, 0, 0, 22.4, -5.9)     31.1 %   53.5    0.241   x 0.25..0.90 y -0.45..0.32
#   (-20, 40, -40, 0, 60, 0)        51.5 %   14.9    0.314   x 0.10..0.45 y -0.32..0.07
#   (0, 40, -50, 0, 60, 0)          65.3 %   24.9    0.381   x 0.21..0.58 y -0.27..0.25
#   (0, 80, -70, 0, 60, 0)          34.3 %    4.9    0.322   x 0.27..0.52 y -0.18..0.16
#
# The second row was --look until it was measured on hardware. Its joint1 = -20 deg
# yaws the base, so the arm visibly leans to one side, and its footprint runs
# y -0.32..+0.07 -- centred 12 cm off the robot's centre line. It was chosen when
# the target was the old proven spawn box, which sat entirely at negative y.
#
# --look is now the THIRD row: joint1 = 0, so the arm stays on the centre line, the
# footprint is symmetric in y, and coverage of the trained fan goes 51.5 -> 65.3 %.
# The cost is 10 more degrees of tilt, and 25 deg is well clear of the ~45 where a
# box starts showing its side rather than its top face. joint5 = 60, not 70: the
# limit is +-70 and a joint parked on its stop is what evaluate.py flags as
# joint_at_limit.
#
# --look-near is the last row: 5 deg, nearly straight down, for calibration and for
# the most accurate single reading. Its footprint is smaller, so place the object
# deliberately.
#
# Three-way agreement on --look's old footprint, which is why these numbers are
# trusted: find_look_pose.py (Isaac articulation) x 0.10..0.44 y -0.31..0.07;
# eval_look_pose.py (piper_sdk CalFK + camera_extrinsic.json) x 0.10..0.45
# y -0.32..0.07; and the detector's own SEEN x/y on the real arm, x 0.15..0.448
# y -0.313..0.054.
#
# PUT THE OBJECT IN THE MIDDLE, not just inside. A blob that reaches a frame edge
# is CUT, and then the median position is pulled toward the visible side and the
# measured width is whatever fraction fit -- an error no offset can remove, because
# it changes with how much was cut off. Measured case: object at (0.308, -0.140)
# under --look-near, 2.5 cm from the near x edge and 3 cm from the near y edge, read
# width 0.019/0.038 on an object whose two sides are equal. Aim for the centre:
#
#   --look        (0.39, -0.01)      --look-near   (0.40, -0.01)
#
# Both are inside the trained fan (r 0.22..0.48, bearing +-60 deg). The detector now
# warns when the blob touches an edge, and refuses to --calibrate against one.
#
# NO POSE SEES THE WHOLE FAN. Covering the fan's 0.84 m of y extent from overhead
# would need the camera about 0.80 m up at the D435's 55.6 deg horizontal field of
# view, which is past this arm's reach.
LOOK_POSE_DEG = (0.0, 40.0, -50.0, 0.0, 60.0, 0.0)
LOOK_NEAR_POSE_DEG = (0.0, 80.0, -70.0, 0.0, 60.0, 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--park", action="store_true",
                   help="the bench park pose, park_joint_pos from deploy_spec.json: "
                        "(0, 0, 0, 0, 30, 0) deg, wrist tilted so the camera looks "
                        "at the table. Where a deploy run ends by default")
    g.add_argument("--home", action="store_true",
                   help="the training home pose, default_joint_pos from "
                        "deploy_spec.json. Where every episode STARTS -- a run must "
                        "begin here or the policy sees offset observations")
    g.add_argument("--look", action="store_true",
                   help="camera over the table at 16 deg from vertical; footprint "
                        "x 0.10..0.44, y -0.31..0.07")
    g.add_argument("--look-near", action="store_true",
                   help="camera nearly straight down, 6 deg; smaller footprint "
                        "x 0.28..0.51, y -0.17..0.15. Use this for --calibrate")
    g.add_argument("--usd-home", "--zero", action="store_true", dest="zero",
                   help="the pose the .usd file authors: all joints at 0. This is "
                        "'home' if you mean the arm's initial pose in the USD. It "
                        "is NOT the training home, and joint2 and joint3 sit on "
                        "their mechanical limits there -- verified from "
                        "piper_girpper_D435_physics.usd")
    g.add_argument("--pose", type=float, nargs=6, metavar=("J1", "J2", "J3", "J4", "J5", "J6"),
                   help="explicit joint angles in DEGREES")
    ap.add_argument("--spec", default="deploy_spec.json")
    # No default cluster on purpose: this script moves a real arm, and picking the wrong
    # bus parks the wrong one. $CAN_PORT (from env.sh) or an explicit --can, nothing else.
    ap.add_argument("--can", default=os.environ.get("CAN_PORT"),
                    help="CAN interface: can_right | can_left (or just right | left). "
                         "Defaults to $CAN_PORT.")
    ap.add_argument("--seconds", type=float, default=5.0, help="ramp duration")
    ap.add_argument("--hz", type=float, default=50.0)
    ap.add_argument("--max-lead", type=float, default=0.15,
                   help="how far the command may run ahead of the measured pose [rad]")
    ap.add_argument("--spd-rate", type=int, default=20, help="move_spd_rate_ctrl 0-100")
    ap.add_argument("--yes", action="store_true", help="skip the countdown")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the pose table and the target, send nothing. Use "
                         "this to check WHICH pose a flag means before moving")
    args = ap.parse_args()

    if not args.can:
        avail = sorted(p.parent.name for p in Path("/sys/class/net").glob("*/type")
                       if p.read_text().strip() == "280")   # ARPHRD_CAN
        sys.exit("--can is required: this moves a real arm and there is one bus per "
                 "master-follower cluster.\n"
                 "  --can %s\n"
                 "  (or set $CAN_PORT once per session)"
                 % (" | --can ".join(avail) or "<no CAN interface up>"))
    if not args.can.startswith("can"):      # `right` -> `can_right`
        args.can = "can_" + args.can

    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)
    names = spec["joint_names"]
    arm = [i for i, n in enumerate(names) if not n.startswith("gripper")]
    limits = np.array(spec["joint_pos_limits"], np.float64)[arm]
    home = np.array(spec["default_joint_pos"], np.float64)[arm]
    _pk = spec.get("park_joint_pos")
    park = np.array(_pk, np.float64) if _pk else None

    # TWO different things get called "home" and they are 20 cm apart. Print both,
    # with where the wrist actually ends up, so there is nothing to guess.
    #
    #   training home   the pose deploy_spec.json exports as default_joint_pos. The
    #                   policy's whole frame is anchored here: the observation is
    #                   q - q_default and the action decodes as q = q_default +
    #                   scale * a. A run MUST start here or every number the policy
    #                   sees is offset.
    #   USD zero        every joint at 0, which is what the .usd file authors as its
    #                   drive target -- read out of piper_girpper_D435_physics.usd,
    #                   all eight targetPosition = 0.0. It is the arm folded back
    #                   over its own base, and the same file gives joint2 a lower
    #                   limit of 0 and joint3 an upper limit of 0, so BOTH sit
    #                   exactly on a mechanical stop there.
    try:
        from piper_sdk.kinematics.piper_fk import C_PiperForwardKinematics

        _fk = C_PiperForwardKinematics()

        def _tcp(q):
            l6 = _fk.CalFK(list(q))[-1]
            return "  wrist at [%4.0f %4.0f %4.0f] mm" % (l6[0], l6[1], l6[2])
    except Exception:
        def _tcp(q):
            return ""

    print("[pose] training home  = %s deg%s"
          % (np.round(np.degrees(home), 2).tolist(), _tcp(home)))
    print("         ^ --home. What the policy is built around; a run must start here.")
    print("[pose] USD zero       = %s deg%s"
          % ([0.0] * len(arm), _tcp(np.zeros(len(arm)))))
    print("         ^ --usd-home (= --zero). The .usd file's own pose."
          " joint2 and joint3 sit ON their limits.")
    if park is not None:
        print("[pose] park           = %s deg%s"
              % (np.round(np.degrees(park), 2).tolist(), _tcp(park)))
        print("         ^ --park. Where a deploy run ENDS. Free to change; nothing"
              " is anchored to it.")

    if args.park:
        if park is None:
            print("deploy_spec.json has no park_joint_pos -- re-run "
                  "scripts/deploy/export_deploy_spec.py")
            return 5
        target, what = park, "park"
    elif args.home:
        target, what = home, "home"
    elif args.zero:
        target, what = np.zeros(len(arm)), "USD zero"
    elif args.look:
        target, what = np.radians(LOOK_POSE_DEG), "look"
    elif args.look_near:
        target, what = np.radians(LOOK_NEAR_POSE_DEG), "look-near"
    else:
        target, what = np.radians(args.pose), "custom"

    clipped = np.clip(target, limits[:, 0], limits[:, 1])
    if not np.allclose(clipped, target, atol=1e-6):
        print("clipped to the joint limits: %s -> %s deg"
              % (np.round(np.degrees(target), 1).tolist(),
                 np.round(np.degrees(clipped), 1).tolist()))
    target = clipped

    from piper_sdk import C_PiperInterface_V2, LogLevel

    p = C_PiperInterface_V2(can_name=args.can, judge_flag=False, can_auto_init=True,
                            logger_level=LogLevel.WARNING)
    p.ConnectPort(can_init=False, piper_init=False, start_thread=True)
    p.EnableFkCal()
    time.sleep(1.5)

    def q_now():
        js = p.GetArmJointMsgs().joint_state
        return np.array([getattr(js, "joint_%d" % i) for i in range(1, 7)],
                        np.float64) * RAD_PER_MILLI_DEG

    st = p.GetArmStatus().arm_status
    fb = float(p.GetArmJointMsgs().Hz)
    print("feedback %.0f Hz | ctrl_mode=%s arm_status=%s"
          % (fb, hex(st.ctrl_mode), hex(st.arm_status)))
    if fb <= 0.0:
        print("No joint feedback. Check power and the CAN cable.")
        return 3
    if st.arm_status != 0x00:
        print("arm_status=%s is not 0x00. Clear the fault first." % hex(st.arm_status))
        return 4

    q0 = q_now()
    print("\n%s pose: %s -> %s deg over %.1f s"
          % (what, np.round(np.degrees(q0), 1).tolist(),
             np.round(np.degrees(target), 1).tolist(), args.seconds))
    if args.dry_run:
        print("*** DRY RUN: nothing sent. That is the '%s' pose. ***" % what)
        return 0
    if not args.yes:
        print("*** THE ROBOT WILL MOVE. Clear the workspace. ***")
        for i in range(3, 0, -1):
            print("   %d..." % i, flush=True)
            time.sleep(1.0)

    p.MotionCtrl_2(ctrl_mode=0x01, move_mode=0x01, move_spd_rate_ctrl=args.spd_rate)
    p.EnableArm(7)
    # Wait for the drivers to actually report enabled instead of assuming. Straight after
    # a power cycle they take seconds, not milliseconds -- and a disabled driver ignores
    # every command silently, so the ramp below runs to completion and reports a large
    # "joint error", which reads as a mechanical fault rather than "you started too soon".
    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline:
        if all(p.GetArmEnableStatus()):
            break
        time.sleep(0.1)
    else:
        st = p.GetArmEnableStatus()
        sys.exit("drivers not enabled after 5s: %s\n"
                 "  J<n>=False -> that driver refused. Check for a latched fault:\n"
                 "    python scripts/check/motor_faults.py --can %s" % (st, args.can))
    time.sleep(0.2)

    n = max(2, int(args.seconds * args.hz))
    try:
        for k in range(1, n + 1):
            a = k / n
            q_cmd = (1.0 - a) * q0 + a * target
            if args.max_lead > 0.0:
                m = q_now()
                q_cmd = np.clip(q_cmd, m - args.max_lead, m + args.max_lead)
            p.JointCtrl(*[int(round(v * DEG_PER_RAD_MILLI)) for v in q_cmd])
            time.sleep(1.0 / args.hz)
    except KeyboardInterrupt:
        print("\n[Ctrl-C] stopping where it is.")
    finally:
        err = float(np.abs(q_now() - target).max())
        print("\nworst joint error vs target: %.4f rad (%.2f deg)" % (err, math.degrees(err)))
        if err > 0.05:
            print("Larger than expected -- a joint may be faulted or blocked.")
        print("Holding this pose. The arm is NOT disabled -- disabling makes it drop.")
        p.DisconnectPort()
    return 0


if __name__ == "__main__":
    sys.exit(main())
