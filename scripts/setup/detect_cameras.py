#!/usr/bin/env python3
"""Find RGB cameras on ANY machine and generate env.sh.

    python scripts/setup/detect_cameras.py            # list what is present
    python scripts/setup/detect_cameras.py --write    # write env.sh

Why this exists: /dev/v4l/by-path/... embeds the machine's PCI identifier
(e.g. pci-0000:00:14.0-usb-0:1.3:1.3), so it CANNOT be committed to a repo.
And /dev/videoN changes on every replug. This probes the current machine and
emits stable paths.

Selection rule: only V4L2 nodes exposing a colour format (YUYV/MJPG) carry RGB.
A RealSense exposes 4-6 nodes per camera; most are depth (Z16) and IR (GREY).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BY_PATH = Path("/dev/v4l/by-path")
COLOR_FORMATS = ("YUYV", "MJPG", "YUV", "RGB3", "BGR3", "NV12")


def v4l2_formats(dev: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", str(dev), "--list-formats"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return [line.split("'")[1] for line in out.splitlines() if "'" in line]


def device_name(dev: Path) -> str:
    p = Path(f"/sys/class/video4linux/{dev.name}/name")
    return p.read_text().strip() if p.exists() else "?"


def find_color_cameras() -> list[dict]:
    """Each entry: {'by_path', 'dev', 'name', 'formats', 'usb_port'}."""
    if not BY_PATH.is_dir():
        return []
    seen: set[str] = set()
    found = []
    for link in sorted(BY_PATH.iterdir()):
        # usbv2-* duplicates usb-*; skip to avoid listing each device twice
        if "usbv2" in link.name:
            continue
        dev = link.resolve()
        if str(dev) in seen:
            continue
        fmts = v4l2_formats(dev)
        if not any(f.strip() in COLOR_FORMATS for f in fmts):
            continue
        seen.add(str(dev))
        # "pci-0000:00:14.0-usb-0:1.3:1.3-video-index0" -> "1.3:1.3"
        port = link.name.split("usb-0:")[-1].split("-video")[0] if "usb-0:" in link.name else "?"
        found.append({
            "by_path": str(link),
            "dev": str(dev),
            "name": device_name(dev),
            "formats": [f.strip() for f in fmts],
            "usb_port": port,
        })
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="write env.sh at the repo root")
    ap.add_argument("--front", type=int, help="index of the fixed camera watching the scene")
    ap.add_argument("--wrist-right", type=int, dest="wrist_right",
                    help="index of the RIGHT cluster's wrist camera")
    ap.add_argument("--wrist-left", type=int, dest="wrist_left",
                    help="index of the LEFT cluster's wrist camera (omit if not fitted)")
    args = ap.parse_args()

    cams = find_color_cameras()
    if not cams:
        print("No colour camera found.")
        print("  - Is the camera plugged in?   lsusb | grep -i -E 'realsense|camera'")
        print("  - Is v4l2-ctl installed?      sudo apt install v4l-utils")
        return 1

    print(f"Found {len(cams)} colour camera(s):\n")
    for i, c in enumerate(cams):
        print(f"  [{i}] {c['name']}")
        print(f"      cong USB  : {c['usb_port']}")
        print(f"      node      : {c['dev']}")
        print(f"      dinh dang : {' '.join(c['formats'])}")
        print(f"      by-path   : {c['by_path']}")
        print()

    if args.front is None or args.wrist_right is None:
        print("=" * 74)
        print("IDENTIFY THE CAMERAS FIRST -- do not guess from the order above.")
        print()
        print("    python scripts/setup/identify_cameras.py")
        print()
        print("It shakes one arm at a time and reports which camera responded to which.")
        print("Then re-run this with the indices it gives, e.g.:")
        print()
        print("    python scripts/setup/detect_cameras.py \\")
        print("        --front 0 --wrist-right 1 --wrist-left 2 --write")
        print("=" * 74)
        return 0

    picked = {"--front": args.front, "--wrist-right": args.wrist_right}
    if args.wrist_left is not None:
        picked["--wrist-left"] = args.wrist_left
    for flag, i in picked.items():
        if not 0 <= i < len(cams):
            print(f"{flag} {i} is out of range: must be 0..{len(cams)-1}.")
            return 1
    if len(set(picked.values())) != len(picked):
        print(f"Each camera can only have one role, got {picked}.")
        return 1

    def cams_json(wrist_var: str) -> str:
        return ("{ front: {type: opencv, index_or_path: $FRONT, width: 640, height: 480, fps: 30}, "
                f"wrist: {{type: opencv, index_or_path: ${wrist_var}, width: 640, height: 480, fps: 30}}}}")

    lines = [
        "# Generated by scripts/setup/detect_cameras.py -- specific to THIS machine.",
        "# by-path embeds this machine's PCI id and each camera's USB port, so re-run that",
        "# script if a camera moves to a different port.",
        "#",
        "#     source env.sh",
        "#",
        f'export PIPER_REPO="{Path(__file__).resolve().parents[2]}"',
        "",
        "# One fixed camera on the scene, one wrist camera per master-follower cluster.",
        f'export FRONT="{cams[args.front]["by_path"]}"',
        f'export WRIST_RIGHT="{cams[args.wrist_right]["by_path"]}"',
    ]
    if args.wrist_left is not None:
        lines.append(f'export WRIST_LEFT="{cams[args.wrist_left]["by_path"]}"')

    lines += [
        "",
        "# Ready to paste into --robot.cameras. Pick the one matching your --can.",
        f'export CAMS_RIGHT="{cams_json("WRIST_RIGHT")}"',
    ]
    if args.wrist_left is not None:
        lines.append(f'export CAMS_LEFT="{cams_json("WRIST_LEFT")}"')
        # For robot.type=piper_bimanual: both wrists plus the shared scene camera, in ONE
        # dataset. Keys are wrist_right / wrist_left, not wrist -- a bimanual episode has two
        # wrist views and they must stay distinguishable.
        both = ("{ front: {type: opencv, index_or_path: $FRONT, width: 640, height: 480, fps: 30}, "
                "wrist_right: {type: opencv, index_or_path: $WRIST_RIGHT, width: 640, height: 480, fps: 30}, "
                "wrist_left: {type: opencv, index_or_path: $WRIST_LEFT, width: 640, height: 480, fps: 30}}")
        lines += ["", "# For --robot.type=piper_bimanual (both clusters, one dataset).",
                  f'export CAMS_BOTH="{both}"']

    lines += [
        "",
        "# The CAN bus is NOT set here on purpose. This rig has two master-follower clusters",
        "# and every command names its own: --can can_right | --can can_left.",
        "",
    ]
    body = "\n".join(lines)

    if args.write:
        out = Path(__file__).resolve().parents[2] / "env.sh"
        out.write_text(body)
        print(f"Wrote {out}\n")
        print(body)
        print("Use:  source env.sh  &&  echo \"$CAMS_RIGHT\"")
    else:
        print("--- env.sh contents (add --write to save) ---")
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
