#!/usr/bin/env python3
"""Live view of both cameras in rerun -- for aiming and identifying which is which.

    source env.sh
    python scripts/check/view_cameras.py [seconds]      # default 60

Without env.sh the script auto-detects the cameras.
READ-ONLY on cameras. Never touches the robot, CAN, or datasets.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import rerun as rr

from lerobot.cameras.opencv import OpenCVCamera, OpenCVCameraConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "setup"))


def resolve_cameras() -> dict[str, str]:
    """Every named camera env.sh knows about: the fixed one plus each cluster's wrist."""
    named = {"front": os.environ.get("FRONT"),
             "wrist_right": os.environ.get("WRIST_RIGHT"),
             "wrist_left": os.environ.get("WRIST_LEFT")}
    named = {k: v for k, v in named.items() if v}
    if named:
        return named
    print("FRONT/WRIST_* not set -- falling back to raw detection order, which carries NO\n"
          "meaning about which arm each camera belongs to. To label them properly:\n"
          "    python scripts/setup/identify_cameras.py\n"
          "    python scripts/setup/detect_cameras.py --front F --wrist-right R --write\n"
          "    source env.sh\n")
    from detect_cameras import find_color_cameras
    cams = find_color_cameras()
    if len(cams) < 2:
        raise SystemExit(f"Need 2 cameras, found {len(cams)}. Run scripts/setup/detect_cameras.py")
    return {f"cam{i}": c["by_path"] for i, c in enumerate(cams)}


def _diagnose(dev_path) -> str:
    """Say which of the two failure modes this is instead of guessing.

    They need opposite fixes: a device someone else holds frees up on its own once that
    process dies, while a device wedged below V4L2 needs a bus-level reset and will keep
    failing however long you wait.
    """
    if dev_path is None:
        return "bash scripts/check/usb_speed.sh    # confirm every camera link is up"

    node = Path(dev_path).resolve()
    holders = ""
    try:
        holders = subprocess.run(["fuser", str(node)], capture_output=True, text=True,
                                 timeout=5).stdout.strip()
    except Exception:
        pass

    if holders:
        return (f"Another process is holding {node} (pids: {holders}).\n"
                "Clear it, wait a few seconds for the kernel to release the USB bandwidth,\n"
                "then retry:\n"
                "  fuser -v /dev/video*\n"
                "  pkill -f rerun\n")

    # Nothing holds it, so either it is wedged or it dropped off the bus. `v4l2-ctl` streaming
    # is the discriminator: it bypasses OpenCV entirely, so if that hangs too the fault is
    # below V4L2 and only a reset clears it.
    return (f"Nothing else holds {node}, so this is not a busy device.\n"
            "The camera is most likely wedged after dropping off the USB bus -- check whether\n"
            "raw V4L2 can stream it at all:\n"
            f"  timeout 5 v4l2-ctl -d {node} --stream-mmap --stream-count=5\n"
            "If that hangs or errors, reset the port (no need to unplug):\n"
            "  sudo bash scripts/check/usb_reset.sh\n"
            "A camera that wedges whenever the arm moves has a cable problem, not a software\n"
            "one -- see TROUBLESHOOTING.md, 'A camera drops out when the arm moves'.\n")


def _link_mbps(dev_path) -> int | None:
    """Negotiated USB speed for the device behind a /dev/video* path, in Mbps."""
    try:
        node = Path(dev_path).resolve().name
        p = Path(f"/sys/class/video4linux/{node}/device").resolve()
        while p != p.parent and not (p / "speed").is_file():
            p = p.parent
        return int((p / "speed").read_text().strip())
    except Exception:
        return None


def main() -> int:
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

    # Spawn the viewer BEFORE any camera is opened. rr.init(spawn=True) forks a rerun
    # process, and a child inherits the parent's open file descriptors -- including the
    # cameras' V4L2 handles, which are not marked CLOEXEC. If this script then dies, that
    # rerun process outlives it still holding those handles: the devices stay busy, their
    # isochronous USB bandwidth stays reserved, and the NEXT run fails to open the last
    # camera with a read timeout that looks like broken hardware. Opening the viewer first
    # means there is nothing for it to inherit.
    rr.init("piper_cameras", spawn=True)

    opened: dict[str, OpenCVCamera] = {}
    failed_name = failed_path = None
    try:
        for name, path in resolve_cameras().items():
            p = Path(path)
            if not p.exists():
                print(f"[skip] {name}: not found at {path}")
                continue
            failed_name, failed_path = name, p
            cam = OpenCVCamera(OpenCVCameraConfig(index_or_path=p, width=640, height=480, fps=30))
            cam.connect()
            opened[name] = cam
            failed_name = failed_path = None
            print(f"[ok] {name}  ->  {p.resolve()}")
    except Exception as e:
        # Release whatever did open, or those devices stay busy for the next run too.
        for cam in opened.values():
            try:
                cam.disconnect()
            except Exception:
                pass
        raise SystemExit(
            f"\nFailed to open {failed_name or '?'}: {type(e).__name__}: {e}\n"
            f"Opened before the failure: {list(opened) or 'none'}\n\n"
            + _diagnose(failed_path)
        ) from e

    if not opened:
        raise SystemExit("could not open any camera")

    # Only warn when the links actually are USB 2.0. One 640x480@30 YUYV stream is ~147 Mbps
    # and USB 2.0 sustains ~280-320, so a third stream gets truncated there -- but on USB 3.0
    # there is ample headroom and the warning would be noise.
    slow = [n for n, c in opened.items() if _link_mbps(c.index_or_path) == 480]
    if len(opened) > 2 and slow:
        print(f"NOTE: {len(slow)} of {len(opened)} cameras are on a 480 Mbps USB 2.0 link "
              f"({', '.join(slow)}).\n"
              "      Three 640x480@30 streams oversubscribe USB 2.0 and the last one arrives\n"
              "      truncated -- a solid green band across part of the image. It is a bandwidth\n"
              "      limit, not a broken camera. Check with: bash scripts/check/usb_speed.sh\n")

    print(f"\nStreaming for {dur:.0f}s. Ctrl+C to stop early.")
    print("Panes are labelled from env.sh. To re-derive the labels from scratch:\n"
          "    python scripts/setup/identify_cameras.py\n")

    t0, n = time.perf_counter(), 0
    try:
        while time.perf_counter() - t0 < dur:
            for name, cam in opened.items():
                rr.log(name, rr.Image(cam.read()))
            n += 1
            time.sleep(1 / 30)
    except KeyboardInterrupt:
        pass
    finally:
        dt = time.perf_counter() - t0
        for cam in opened.values():
            cam.disconnect()
        print(f"\n{n} frames / {dt:.1f}s  =  {n/dt:.1f} fps per camera")
    return 0


if __name__ == "__main__":
    sys.exit(main())
