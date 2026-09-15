#!/usr/bin/env python3
"""Check the whole rig before recording data or running a policy.

    source env.sh
    python scripts/check/preflight.py            # basic checks
    python scripts/check/preflight.py --teleop   # also: is the master arm transmitting
    python scripts/check/preflight.py --no-tx    # skip the CAN transmit probe

Never commands the robot. The only thing it puts on the wire is the TX probe in
check_can_tx() -- zero-length frames on ID 0x7FF, which the PiPER protocol never
uses (it tops out at 0x4AF). Pass --no-tx to send literally nothing.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

OK, WARN, FAIL = "[ OK ]", "[warn]", "[FAIL]"
results: list[tuple[str, str]] = []


def report(level: str, msg: str, hint: str = "") -> None:
    results.append((level, msg))
    print(f"{level} {msg}")
    if hint and level != OK:
        for line in hint.strip().splitlines():
            print(f"       {line}")


def check_cpu() -> None:
    gov = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    if not gov.exists():
        report(WARN, "cannot read the CPU governor")
        return
    g = gov.read_text().strip()
    mhz = [float(l.split(":")[1]) for l in Path("/proc/cpuinfo").read_text().splitlines()
           if "cpu MHz" in l]
    avg = sum(mhz) / len(mhz) if mhz else 0
    if g == "performance" and avg > 2500:
        report(OK, f"CPU governor={g}, {avg:.0f} MHz")
    else:
        report(FAIL, f"CPU governor={g}, only {avg:.0f} MHz",
               "The 'powersave' governor pins the CPU low -> the 30 Hz loop misses its\n"
               "deadline -> the robot shakes. Fix:  sudo bash scripts/setup/set_cpu_performance.sh")


def check_load() -> None:
    load1 = os.getloadavg()[0]
    ncore = os.cpu_count() or 1
    heavy = subprocess.run(
        ["ps", "-eo", "pid,pcpu,etime,args", "--sort=-pcpu", "--no-headers"],
        capture_output=True, text=True).stdout.splitlines()[:3]
    busy = [l for l in heavy if float(l.split()[1]) > 50]
    if load1 > ncore * 0.5 or busy:
        report(WARN, f"load {load1:.2f} / {ncore} threads; heavy processes running",
               "\n".join("  " + l[:90] for l in busy) +
               "\nForgotten jobs (Isaac Lab, ...) drag the cadence down. kill <pid> if not needed.")
    else:
        report(OK, f"CPU idle (load {load1:.2f} / {ncore} threads)")


def check_can(port: str) -> None:
    st = Path(f"/sys/class/net/{port}/operstate")
    if not st.exists():
        report(FAIL, f"{port} does not exist",
               "Adapter not plugged in, or the driver is not loaded.\n"
               "  lsusb | grep 1d50:606f\n"
               "  sudo bash scripts/can/fix_can.sh")
        return
    detail = subprocess.run(["ip", "-details", "link", "show", port],
                            capture_output=True, text=True).stdout
    if "state UP" not in detail:
        report(FAIL, f"{port} is DOWN",
               "sudo bash scripts/can/fix_can.sh")
        return
    if "bitrate" not in detail:
        report(FAIL, f"{port} is UP but has NO bitrate",
               "'ip link set up' cannot bring up CAN without a bitrate.\n"
               "  sudo bash scripts/can/fix_can.sh")
        return
    rx = Path(f"/sys/class/net/{port}/statistics/rx_packets")
    a = int(rx.read_text()); time.sleep(3); b = int(rx.read_text())
    fps = (b - a) / 3
    if fps == 0:
        report(FAIL, f"{port} is UP but the bus is SILENT (0 fps)",
               "Arm not powered, or the CAN cable is not connected.")
    elif fps < 1500:
        report(WARN, f"{port}: {fps:.0f} fps -- below one arm (~2422)")
    elif fps < 3600:
        report(OK, f"{port}: {fps:.0f} fps  (~2422 = one arm reporting state)")
    else:
        report(WARN, f"{port}: {fps:.0f} fps -- looks like TWO arms reporting",
               "If you are running a policy, the master arm must be POWERED OFF.")


def _can_errors(port: str) -> dict[str, int]:
    """The controller's cumulative error counters, from `ip -details -statistics`.

    Six names on one line, six numbers on the next:
        re-started bus-errors arbit-lost error-warn error-pass bus-off
    """
    out = subprocess.run(["ip", "-details", "-statistics", "link", "show", port],
                         capture_output=True, text=True).stdout.split()
    if "re-started" not in out:
        return {}
    i = out.index("re-started")
    try:
        return {n: int(v) for n, v in zip(out[i:i + 6], out[i + 6:i + 12], strict=True)}
    except ValueError:
        return {}


def _bus_holder(port: str) -> str | None:
    """A process that is probably already driving a CAN bus, or None.

    /proc/net/can/raw would name the exact socket owners, but that proc entry is
    not present here, so match on the handful of commands that open these buses.
    """
    own = {str(os.getpid()), str(os.getppid())}
    keys = ("lerobot-rollout", "lerobot-record", "lerobot-teleoperate",
            "park_arm.py", "bus_scan.py")
    out = subprocess.run(["ps", "-eo", "pid,args", "--no-headers"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid in own:
            continue
        hit = next((k for k in keys if k in args), None)
        if hit:
            return f"{hit} (pid {pid})"
    return None


def check_can_tx(port: str, frames: int = 20) -> None:
    """Prove the TRANSMIT path works. A healthy RX says nothing about TX.

    This is the check whose absence cost a session. A bus carrying one terminator
    too many -- or an unpowered second cluster hanging off the same wire -- reads
    a perfect 2422 fps while every single write fails. You hear the arm; the arm
    never hears you. Recording never writes (`passive=True`), so the fault sits
    invisible until a rollout floods the log with
    `SendCanMessage(SEND_MESSAGE_FAILED (100017))`.

    Sends zero-length frames on ID 0x7FF. PiPER's highest ID is 0x4AF, so the arm
    ignores them: nothing is commanded, nothing moves.
    """
    try:
        import can
    except ImportError:
        report(WARN, "python-can not installed -- TX path NOT verified")
        return

    # Refuse to probe a bus somebody is already driving. Two writers on one bus is
    # exactly the condition this check exists to catch, so running it under a live
    # rollout both corrupts the verdict and disturbs the run.
    holder = _bus_holder(port)
    if holder:
        report(WARN, f"{port}: TX probe SKIPPED -- {holder} is on the bus",
               "Stop that process first, then re-run. Probing a bus another process\n"
               "is driving reports errors that are the probe's own fault.")
        return

    before = _can_errors(port)
    txp = Path(f"/sys/class/net/{port}/statistics/tx_packets")
    tx0 = int(txp.read_text())

    try:
        bus = can.interface.Bus(channel=port, interface="socketcan")
    except Exception as exc:
        report(FAIL, f"{port}: cannot open for writing -- {exc}")
        return

    failed = 0
    try:
        msg = can.Message(arbitration_id=0x7FF, data=b"", is_extended_id=False)
        for _ in range(frames):
            try:
                bus.send(msg, timeout=0.1)
            except can.CanError:
                failed += 1
            time.sleep(0.005)
    finally:
        bus.shutdown()

    time.sleep(0.3)          # let the controller update its counters
    tx1 = int(txp.read_text())
    after = _can_errors(port)
    grew = {k: after[k] - before[k]
            for k in after if k in before and after[k] > before[k]}

    hint = (
        "The arm can talk to you, but you cannot talk to the arm.\n"
        f"  1. sudo bash scripts/can/fix_can.sh --can {port}\n"
        "     Clears a wedged bus-off. These gs_usb adapters cannot self-recover\n"
        "     (restart-ms stays 0), so reloading the driver is the only way out.\n"
        "  2. Unplug every CAN cable that is not this cluster's. A second cluster on\n"
        "     the same wire adds a THIRD 120 ohm terminator (60 -> 40 ohm), and if it\n"
        "     is unpowered its transceivers load the line through their body diodes.\n"
        "     An unpowered cluster is NOT electrically neutral.\n"
        "  3. Power everything off, unplug both adapters, measure CAN_H to CAN_L:\n"
        "     60 ohm = correct - 40 ohm = one terminator too many - 120 ohm = missing one."
    )

    if failed or tx1 == tx0:
        report(FAIL,
               f"{port}: TX DEAD ({failed}/{frames} sends errored, "
               f"tx_packets {tx0} -> {tx1})", hint)
    elif grew:
        report(FAIL,
               f"{port}: TX went out but the controller logged errors -- "
               + ", ".join(f"{k} +{v}" for k, v in grew.items()), hint)
    else:
        report(OK, f"{port}: TX path healthy ({tx1 - tx0} frames out, no controller errors)")


def check_control_frames(port: str, seconds: float = 5.0) -> None:
    """0x150-0x15F = master arm commands. Expected while recording, absent while running a policy."""
    try:
        import can
    except ImportError:
        report(WARN, "python-can not installed, skipping control-frame check")
        return
    try:
        bus = can.interface.Bus(channel=port, interface="socketcan")
    except Exception as e:
        report(WARN, f"cannot open {port}: {e}")
        return
    n, t0 = 0, time.time()
    while time.time() - t0 < seconds:
        m = bus.recv(timeout=0.5)
        if m is not None and 0x150 <= m.arbitration_id <= 0x15F:
            n += 1
    bus.shutdown()
    rate = n / seconds
    if rate > 10:
        report(OK, f"control frames 0x15x: {rate:.0f}/s -- master arm IS TRANSMITTING",
               "Correct for RECORDING. Power the master OFF before running a policy.")
    else:
        report(OK, f"control frames 0x15x: {rate:.0f}/s -- master arm SILENT",
               "Correct for RUNNING A POLICY. To record, power the master on and move it.")


def check_arm(port: str) -> None:
    try:
        from piper_sdk import C_PiperInterface_V2
    except ImportError:
        report(FAIL, "piper_sdk not installed", "pip install piper_sdk")
        return
    try:
        p = C_PiperInterface_V2(port, judge_flag=False)
        p.ConnectPort(can_init=False, piper_init=False, start_thread=True)
        time.sleep(3)
        j = p.GetArmJointMsgs()
        g = p.GetArmGripperMsgs()
        import math
        deg = [getattr(j.joint_state, f"joint_{i}") * 0.001 for i in range(1, 7)]
        if j.Hz > 100:
            report(OK, f"arm feedback {j.Hz:.0f} Hz | joints (deg): "
                       f"{[round(d,1) for d in deg]}")
            report(OK, f"gripper {g.Hz:.0f} Hz | opening {g.gripper_state.grippers_angle*0.001:.1f} mm")
        else:
            report(FAIL, f"arm feedback only {j.Hz:.0f} Hz", "Is the arm powered?")
        p.DisconnectPort()
    except Exception as e:
        report(FAIL, f"cannot read the arm: {type(e).__name__}: {e}")


def check_cameras(port: str) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "setup"))
    from detect_cameras import find_color_cameras
    cams = find_color_cameras()
    if len(cams) < 2:
        report(FAIL, f"only {len(cams)} colour camera(s) found, need 2",
               "python scripts/setup/detect_cameras.py")
        return
    report(OK, f"found {len(cams)} colour cameras")

    # One wrist camera per cluster, so which one to check follows --can. $ALL is the
    # optional overview camera from env_all.sh -- absent on a rig without one.
    #
    # Open every camera a run will actually use, not a subset: this check exists to catch a
    # starved USB link, and two cameras cannot starve a link that four will. A probe lighter
    # than the real load reports ALL PASS on a rig whose rollout still drops frames.
    wrist_var = "WRIST_LEFT" if port.endswith("left") else "WRIST_RIGHT"
    wanted = [(wrist_var, os.environ.get(wrist_var)), ("FRONT", os.environ.get("FRONT"))]
    if os.environ.get("ALL"):
        wanted.append(("ALL", os.environ.get("ALL")))
    if not all(path for _, path in wanted[:2]):
        report(WARN, f"{wrist_var}/FRONT variables not set",
               "python scripts/setup/identify_cameras.py     # which camera is which\n"
               "python scripts/setup/detect_cameras.py --front F --wrist-right R "
               "--wrist-left L --write\n"
               "source env_all.sh")
        return
    for label, path in wanted:
        if Path(path).exists():
            report(OK, f"{label} -> {Path(path).resolve()}")
        else:
            report(FAIL, f"{label} does not exist: {path}",
                   "Camera moved to a different port. Re-run detect_cameras.py --write")

    try:
        from lerobot.cameras.opencv import OpenCVCamera, OpenCVCameraConfig
        opened = []
        for _, path in wanted:
            c = OpenCVCamera(OpenCVCameraConfig(index_or_path=Path(path),
                                                width=640, height=480, fps=30))
            c.connect(); opened.append(c)
        # Measure rate and frame integrity in the SAME pass, while the cameras are still
        # open. A starved USB link delivers a PARTIAL frame: the unwritten tail decodes as
        # solid green, while fps stays at 30 and the buffer is still allocated full size --
        # so neither rate nor buffer size catches it, and a policy would train on the green.
        worst, rows_total = 0, 0
        t0 = time.perf_counter()
        for _ in range(30):
            for c in opened:
                a = c.read().astype("int16")
                r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
                rows = ((g > 100) & (r < 60) & (b < 60)).mean(axis=1)
                worst = max(worst, int((rows > 0.9).sum()))
                rows_total = a.shape[0]
        elapsed = time.perf_counter() - t0
        for c in opened:
            c.disconnect()
        # This loop reads the cameras one after another, so what it can honestly measure is
        # TOTAL reads per second, not each camera's own rate: adding a camera splits the
        # same total further. Judge the total, and let the truncation check below be the
        # real bandwidth verdict -- a starved link still reports 30 fps while delivering
        # half a frame, which is exactly why that check exists.
        reads = 30 * len(opened)
        total = reads / elapsed
        if total >= 40:
            report(OK, f"{len(opened)} cameras open simultaneously, "
                       f"{total:.0f} reads/s total ({total/len(opened):.1f} per camera)")
        else:
            report(WARN, f"{len(opened)} cameras: only {total:.0f} reads/s total",
                   "Low throughput. If the truncated-frame check below passes, this is this\n"
                   "loop's own overhead, not the bus. If it fails, the USB link is starved.")
        if worst:
            report(FAIL, f"TRUNCATED frames: {worst}/{rows_total} rows arrive as solid green",
                   "The USB link cannot carry these streams. Check the link speed:\n"
                   "  for v in $(ls /sys/class/video4linux); do "
                   "p=$(readlink -f /sys/class/video4linux/$v/device); "
                   "while [ ! -f \"$p/speed\" ]; do p=$(dirname $p); done; "
                   "echo \"$v $(cat $p/speed) Mbps\"; done\n"
                   "480 Mbps means USB 2.0: a D435 needs its USB 3.0 cable (a USB 2 cable in\n"
                   "a USB 3 port still negotiates 480). 640x480@30 YUYV is ~147 Mbps each.")
        else:
            report(OK, f"no truncated frames (full {rows_total} rows on every frame)")
    except Exception as e:
        report(FAIL, f"cannot open both cameras at once: {type(e).__name__}: {e}",
               "Is another process holding them? fuser /dev/video*")


def check_gpu() -> None:
    try:
        import torch
    except ImportError:
        report(WARN, "torch not installed")
        return
    if not torch.cuda.is_available():
        report(FAIL, "CUDA unavailable", "Training will be very slow or impossible.")
        return
    p = torch.cuda.get_device_properties(0)
    report(OK, f"GPU {p.name}, {p.total_memory/1e9:.1f} GB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--teleop", action="store_true",
                    help="check whether the master arm is transmitting")
    ap.add_argument("--no-tx", action="store_true",
                    help="skip the CAN transmit probe (then nothing at all is sent)")
    ap.add_argument("--can", default=None,
                    help="CAN interface: can_right | can_left (or just right | left). "
                         "Defaults to $CAN_PORT, then can_right.")
    args = ap.parse_args()
    # One bus per master-follower cluster, so which one is never implicit. check_can()
    # FAILs loudly on a name that does not exist, which is the safe way to be wrong here.
    port = args.can or os.environ.get("CAN_PORT") or "can_right"
    if not port.startswith("can"):          # `right` -> `can_right`
        port = "can_" + port

    print("=" * 62)
    print(" PREFLIGHT -- PiPER + LeRobot")
    print("=" * 62)
    for title, fn in [
        ("MACHINE", lambda: (check_cpu(), check_load(), check_gpu())),
        ("CAN BUS", lambda: (check_can(port),
                             None if args.no_tx else check_can_tx(port),
                             check_arm(port))),
        ("CAMERAS", lambda: check_cameras(port)),
    ]:
        print(f"\n-- {title} --")
        fn()
    if args.teleop:
        print("\n-- MASTER ARM --")
        check_control_frames(port)

    n_fail = sum(1 for lv, _ in results if lv == FAIL)
    n_warn = sum(1 for lv, _ in results if lv == WARN)
    print("\n" + "=" * 62)
    if n_fail:
        print(f" {n_fail} FAILED, {n_warn} warnings -- FIX BEFORE RUNNING")
    elif n_warn:
        print(f" {n_warn} warnings -- usable, but worth a look")
    else:
        print(" ALL PASS -- ready")
    print("=" * 62)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
