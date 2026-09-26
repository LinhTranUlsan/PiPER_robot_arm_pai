#!/usr/bin/env python3
"""Identify which camera AND which CAN bus belongs to which arm, by shaking one at a time.

    python identify_cameras.py

Two windows: a quiet baseline, then one per cluster.

Why both at once: the udev rule pins a CAN name to an adapter's serial, which survives a
port change -- but it cannot know whether that adapter is still cabled to the same ARM. If
the CAN leads were ever swapped between clusters, `can_right` silently drives the left arm
and nothing in the config reveals it. Moving the right master emits control frames
(0x155-0x157, 0x159) on exactly one bus, which settles it by measurement.

For each CAN bus (skipped if it is down):

  control  0x15x frames seen. The master arm you moved emits these on ITS OWN bus only.

For each camera:

  energy   mean |frame - prev| for the window, as a MULTIPLE of that camera's own quiet
           baseline. This is the reliable signal -- it says "this camera saw motion".
  spread   fraction of the frame that changed. Separates a wrist camera (rides on J6, so
           the whole image moves) from a fixed one (only part of its view changes).
           Threshold is the 90th percentile of that camera's own baseline noise; a
           stricter bar silently crushes real motion to zero.

It also writes first/last frames per camera per phase to  frames_<phase>_<cam>_{a,b}.png
so the wrist-vs-fixed call can be settled by eye when the numbers are close.
"""
import sys, threading, time
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import can as pycan
except ImportError:                     # cameras still work without python-can
    pycan = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect_cameras import find_color_cameras
from lerobot.cameras.opencv import OpenCVCamera, OpenCVCameraConfig

OUT = Path(__file__).resolve().parents[2] / "camera_id_frames"
OUT.mkdir(exist_ok=True)
BASELINE_S, PHASE_S = 4.0, 7.0
NOISE_PCT = 90.0
MIN_THRESH = 3.0


CONTROL = range(0x150, 0x160)


def can_up():
    """CAN interfaces that are UP -- a down bus cannot be listened to."""
    out = []
    for p in sorted(Path("/sys/class/net").glob("*/type")):
        try:
            if p.read_text().strip() != "280":      # ARPHRD_CAN
                continue
            if (p.parent / "operstate").read_text().strip() in ("up", "unknown"):
                out.append(p.parent.name)
        except OSError:
            pass
    return out


def listen(iface, stop, tally):
    """Count control vs total frames on one bus until `stop` is set."""
    try:
        bus = pycan.interface.Bus(channel=iface, interface="socketcan")
    except Exception as e:
        tally[iface] = {"error": type(e).__name__}
        return
    tally[iface] = {"control": 0, "total": 0}
    try:
        while not stop.is_set():
            m = bus.recv(timeout=0.2)
            if m is None:
                continue
            tally[iface]["total"] += 1
            if m.arbitration_id in CONTROL:
                tally[iface]["control"] += 1
    except Exception as e:
        tally[iface].setdefault("error", type(e).__name__)
    finally:
        try:
            bus.shutdown()
        except Exception:
            pass


def gray_small(frame):
    return frame.astype(np.float32).mean(axis=2)[::8, ::8]


def sample(cams, seconds, keep_frames=False):
    """Consecutive-frame diffs per camera; optionally the first and last raw frame."""
    prev = {k: None for k in cams}
    diffs = {k: [] for k in cams}
    first, last = {}, {}
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        for k, cam in cams.items():
            try:
                f = cam.read()
            except Exception:
                continue
            if keep_frames:
                if k not in first:
                    first[k] = f.copy()
                last[k] = f
            g = gray_small(f)
            if prev[k] is not None:
                diffs[k].append(np.abs(g - prev[k]))
            prev[k] = g
    return diffs, first, last


def countdown(msg, secs=4):
    print("\n" + "=" * 70 + "\n" + msg + "\n" + "=" * 70, flush=True)
    for i in range(secs, 0, -1):
        print("   starting in %d ..." % i, flush=True)
        time.sleep(1)


found = find_color_cameras()
print("Found %d colour camera(s):" % len(found))
cams, meta = {}, {}
for i, c in enumerate(found):
    key = "cam%d" % i
    try:
        cam = OpenCVCamera(OpenCVCameraConfig(index_or_path=Path(c["by_path"]),
                                              width=640, height=480, fps=30))
        cam.connect()
        cams[key], meta[key] = cam, c
        print("  %-6s usb=%-14s" % (key, c["usb_port"]))
    except Exception as e:
        print("  %-6s usb=%-14s FAILED (%s)" % (key, c["usb_port"], type(e).__name__))
if not cams:
    sys.exit("No camera opened.")

PHASES = [("right", "RIGHT arm"), ("left", "LEFT arm")]
BUSES = can_up() if pycan else []
if BUSES:
    print("\nWatching CAN: %s" % ", ".join(BUSES))

    # A powered arm reports its state continuously (~2420 frames/s) whether or not anyone
    # touches it, so silence on every bus means nothing is powered. This whole script works
    # by SHAKING an arm, so without that it spends 22 s measuring nothing and prints a table
    # of 1.00x with the reason buried at the bottom.
    _stop, _tally, _threads = threading.Event(), {}, []
    for _b in BUSES:
        _t = threading.Thread(target=listen, args=(_b, _stop, _tally), daemon=True)
        _t.start()
        _threads.append(_t)
    time.sleep(1.5)
    _stop.set()
    for _t in _threads:
        _t.join(timeout=2)
    _total = sum(v.get("total", 0) for v in _tally.values())
    if _total == 0:
        for cam in cams.values():
            cam.disconnect()
        sys.exit(
            "\nEvery CAN bus is silent -- no arm is powered.\n"
            "  This step identifies a camera by SHAKING the arm it rides on, so with the\n"
            "  arms off every camera can only read 1.00x.\n"
            "  Power both clusters on, wait 10 s, then run this again.\n"
            "  Check with: python scripts/can/bus_scan.py 5 --can " + BUSES[0]
        )
    print("   arms powered -- %.0f frames/s across %d bus(es)" % (_total / 1.5, len(BUSES)))
    del _stop, _tally, _threads, _total
elif pycan:
    print("\nNo CAN bus is up -- cameras only. To include the CAN check:\n"
          "    sudo bash scripts/can/fix_can.sh")
else:
    print("\npython-can not installed -- cameras only.")
try:
    countdown("BASELINE -- hold still, touch nothing (%.0fs)" % BASELINE_S, 3)
    qd, _, _ = sample(cams, BASELINE_S)
    thresh, base_e = {}, {}
    for k in cams:
        allv = np.concatenate([d.ravel() for d in qd[k]]) if qd[k] else np.zeros(1)
        thresh[k] = max(float(np.percentile(allv, NOISE_PCT)), MIN_THRESH)
        base_e[k] = max(float(allv.mean()), 1e-6)
    for k in cams:
        print("   %-6s baseline energy %6.2f   change threshold %5.2f" % (k, base_e[k], thresh[k]))

    E, S = {}, {}
    CAN = {}
    for phase, human in PHASES:
        countdown("SHAKE THE %s WRIST for %.0f s -- BIG movements, start NOW" % (human, PHASE_S))
        # One listener thread per bus: interleaving recv() with camera reads would drop frames.
        stop, tally, threads = threading.Event(), {}, []
        for b in BUSES:
            th = threading.Thread(target=listen, args=(b, stop, tally), daemon=True)
            th.start()
            threads.append(th)
        d, fa, fb = sample(cams, PHASE_S, keep_frames=True)
        stop.set()
        for th in threads:
            th.join(timeout=2)
        CAN[phase] = tally
        E[phase] = {k: (float(np.mean([x.mean() for x in d[k]])) if d[k] else 0.0) for k in cams}
        S[phase] = {k: (float(np.mean([(x > thresh[k]).mean() for x in d[k]])) if d[k] else 0.0)
                    for k in cams}
        for k in cams:
            if k in fa:
                Image.fromarray(fa[k].astype(np.uint8)).save(OUT / f"frames_{phase}_{k}_a.png")
                Image.fromarray(fb[k].astype(np.uint8)).save(OUT / f"frames_{phase}_{k}_b.png")
        print("   energy x baseline:",
              "  ".join("%s=%.2fx" % (k, E[phase][k] / base_e[k]) for k in cams))
        if BUSES:
            print("   control frames:  ",
                  "  ".join("%s=%s" % (b, tally.get(b, {}).get("control", "?")) for b in BUSES))
finally:
    for cam in cams.values():
        cam.disconnect()

print("\n" + "=" * 84)
print(" RESULTS      energy is a multiple of each camera's own quiet baseline")
print("=" * 84)
print("%-6s %-14s | %-17s | %-17s" % ("cam", "usb_port", "RIGHT phase", "LEFT phase"))
print("%-6s %-14s | %-8s %-8s | %-8s %-8s" % ("", "", "energy", "spread", "energy", "spread"))
print("-" * 84)
R = {}
for k in cams:
    er, el = E["right"][k] / base_e[k], E["left"][k] / base_e[k]
    R[k] = {"right": (er, S["right"][k]), "left": (el, S["left"][k])}
    print("%-6s %-14s | %7.2fx %8.2f | %7.2fx %8.2f"
          % (k, meta[k]["usb_port"], er, S["right"][k], el, S["left"][k]))

if BUSES:
    print("\n" + "=" * 84)
    print(" CAN VERDICT   the bus carrying 0x15x is the one whose master you just moved")
    print("=" * 84)
    for b in BUSES:
        cr = CAN["right"].get(b, {})
        cl = CAN["left"].get(b, {})
        if "error" in cr or "error" in cl:
            print("  %-10s could not listen (%s)" % (b, cr.get("error") or cl.get("error")))
            continue
        r, l = cr.get("control", 0), cl.get("control", 0)
        if r == 0 and l == 0:
            print("  %-10s NO control frames in either phase -- master not moved, or powered off"
                  % b)
        elif r > 0 and l > 0:
            print("  %-10s control in BOTH phases (%d right / %d left) -- cannot separate. Move "
                  "only ONE master per window." % (b, r, l))
        else:
            side = "RIGHT" if r > l else "LEFT"
            print("  %-10s -> %-5s cluster   (%d control frames in the %s window, 0 in the other)"
                  % (b, side, max(r, l), side.lower()))
    print()
    print("  If this contradicts the name -- can_right resolving to the LEFT cluster -- the CAN")
    print("  leads were swapped between arms. Swap the two serials in")
    print("  scripts/can/80-piper-can.rules and re-run scripts/can/install_udev.sh.")

print("\n" + "=" * 84)
print(" CAMERA VERDICT   a camera belongs to the arm whose phase it responded to")
print("=" * 84)
RESPOND = 1.30      # 30% above its own baseline counts as "saw that arm move"

# How hard each master was actually moved, from its own bus. Spread is NOT comparable
# across phases when these differ: a gently shaken arm produces a low spread on a genuine
# wrist camera, which a fixed threshold then mislabels as a fixed camera.
effort = {}
for phase, _ in PHASES:
    effort[phase] = sum(v.get("control", 0) for v in CAN.get(phase, {}).values()) if BUSES else 0
if BUSES and min(effort.values()) > 0:
    lo, hi = min(effort.values()), max(effort.values())
    print("  shake effort (0x15x frames):  " +
          "   ".join("%s=%d" % (ph, effort[ph]) for ph, _ in PHASES))
    if hi > lo * 1.4:
        weak = min(effort, key=effort.get)
        print("  ⚠ the %s window was moved %.1fx less -- its spread reads low for that reason"
              % (weak, hi / lo))
        print("    alone, NOT because the camera is fixed. Settle it with the frames below.")
    print()

claimed, wrist_of, no_response = {}, {}, []
for k in cams:
    er, el = R[k]["right"][0], R[k]["left"][0]
    if max(er, el) < RESPOND:
        print("  %-6s usb=%-14s NO RESPONSE (%.2fx / %.2fx) -- that arm is not in its view"
              % (k, meta[k]["usb_port"], er, el))
        no_response.append(k)
        continue
    phase = "right" if er > el else "left"
    ratio, spr = R[k][phase]
    other = R[k]["left" if phase == "right" else "right"][0]
    # Scale the bar by how hard that arm was moved, so a gentle shake is not penalised.
    bar = 0.30
    if BUSES and min(effort.values()) > 0:
        bar *= effort[phase] / max(effort.values())
    if spr > bar:
        kind = "likely WRIST (whole frame moved)"
        wrist_of[phase] = k
    else:
        kind = "likely fixed / scene view -- CHECK THE FRAMES"
    print("  %-6s usb=%-14s -> %-5s arm   %.2fx (other phase %.2fx), spread %.2f  = %s"
          % (k, meta[k]["usb_port"], phase.upper(), ratio, other, spr, kind))
    claimed.setdefault(phase, []).append(k)

print("\nFrames written to %s :" % OUT)
print("  frames_<phase>_<cam>_a.png  and  _b.png   -- first and last frame of that shake")
print("  If the whole scene shifted between a and b, that camera is on the arm.")
print()
print("Then record the assignment:")
# Print the real indices wherever they were measured. `--front F --wrist-right R` is a
# template, and pasting it verbatim -- which is what the docs invite -- is the single most
# common way this step fails. "camN" is the Nth entry of detect_cameras.py's own listing,
# so the index is just the digits in the key.
_flags = ["--wrist-%s %s" % (side, wrist_of[side][3:]) for side in ("right", "left")
          if side in wrist_of]
if len(no_response) == 1:
    _flags.insert(0, "--front %s" % no_response[0][3:])
if _flags and "--front" in " ".join(_flags):
    print("  python scripts/setup/detect_cameras.py %s --write" % " ".join(_flags))
else:
    print("  python scripts/setup/detect_cameras.py --front F --wrist-right R "
          "--wrist-left L --write")
    print("  # F / R / L are PLACEHOLDERS -- replace with the cam numbers above:"
          " cam0 -> 0, cam1 -> 1, ...")
if len(no_response) > 1:
    print("  # %d cameras never responded (%s). One is FRONT; any other is the fixed"
          % (len(no_response), ", ".join(no_response)))
    print("  # overview camera. Tell them apart in %s, then set --front yourself" % OUT)
    print("  # and put the overview camera's by-path into ALL= in env_all.sh.")
