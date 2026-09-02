#!/usr/bin/env python3
"""Scan a CAN bus with raw python-can (bypassing the piper_sdk parser).

    python bus_scan.py [seconds]                  # default 15
    python bus_scan.py 5 --can can_right
    python bus_scan.py 5 --can right              # same thing, shorthand

ID classes, from the SDK's can_id.py:
    0x2Ax  feedback  an arm is REPORTING its state (follower / normal arm)
    0x15x  control   an arm is SENDING commands (teach / master arm)
    0x47x  config    configuration commands and their replies

This rig has one bus per master-follower cluster, so the bus is never implicit when more
than one is up: pass --can (or set CAN_PORT) rather than let the script pick. Guessing is
what makes a healthy cluster look like a dead one.
"""

import argparse
import collections
import glob
import os
import sys
import time

import can

FEEDBACK = range(0x2A1, 0x2A9)
CONTROL = range(0x150, 0x160)


def kind(i):
    if i in FEEDBACK:
        return "feedback"
    if i in CONTROL:
        return "control"
    return "config" if 0x470 <= i <= 0x47F else "other"


def can_interfaces():
    """Every CAN interface the kernel currently has, named or not."""
    out = []
    for p in sorted(glob.glob("/sys/class/net/*/type")):
        try:
            with open(p) as f:
                # ARPHRD_CAN == 280
                if f.read().strip() == "280":
                    out.append(p.split("/")[-2])
        except OSError:
            pass
    return out


def normalize(name):
    """`right` -> `can_right`. Anything already starting with `can` is left alone."""
    return name if name.startswith("can") else "can_" + name


def resolve_channel(explicit):
    if explicit:
        return normalize(explicit)
    if os.environ.get("CAN_PORT"):
        return normalize(os.environ["CAN_PORT"])
    found = can_interfaces()
    if len(found) == 1:
        return found[0]
    if not found:
        sys.exit("No CAN interface exists. Plug in an adapter, then: sudo bash scripts/can/fix_can.sh")
    sys.exit(
        "%d CAN interfaces are present (%s) -- say which one.\n"
        "  python scripts/can/bus_scan.py --can %s" % (len(found), ", ".join(found), found[0])
    )


ap = argparse.ArgumentParser()
ap.add_argument("seconds", nargs="?", type=float, default=15.0)
ap.add_argument("--can", default=None,
                help="CAN interface: can_right | can_left (or just right | left). Falls back to $CAN_PORT, then the only bus up.")
args = ap.parse_args()

DUR = args.seconds
CHAN = resolve_channel(args.can)

try:
    bus = can.interface.Bus(channel=CHAN, interface="socketcan")
except OSError as e:
    sys.exit("Cannot open %s: %s\nIs it up with a bitrate? ip -details link show %s" % (CHAN, e, CHAN))

cnt, n, t0, first, last = collections.Counter(), 0, time.time(), None, None
try:
    while time.time() - t0 < DUR:
        m = bus.recv(timeout=0.5)
        if m is None:
            continue
        first = m.timestamp if first is None else first
        last = m.timestamp
        cnt[m.arbitration_id] += 1
        n += 1
except can.CanError as e:
    # "Network is down" lands here, not at Bus(): opening a socket on a DOWN interface
    # succeeds, and only the first recv() fails.
    bus.shutdown()
    sys.exit("%s: %s\n"
             "The interface exists but is not up with a bitrate:\n"
             "  sudo bash scripts/can/fix_can.sh --can %s" % (CHAN, e, CHAN))
except KeyboardInterrupt:
    bus.shutdown()
    sys.exit(130)
bus.shutdown()

print("%s: %d frames in %.0fs  (%.0f fps)" % (CHAN, n, DUR, n / DUR))
if not n:
    print("BUS SILENT.")
    print("  - is that cluster powered on, and its e-stop released?")
    print("  - is the arm cable on THIS adapter? other interfaces: %s"
          % (", ".join(i for i in can_interfaces() if i != CHAN) or "none"))
    sys.exit(1)
print("traffic spanned %.2fs -> %s"
      % (last - first, "continuous" if last - first > DUR * 0.8 else "BURSTS then silence"))
print()
for i, k in sorted(cnt.items()):
    print("  0x%03X  %-9s %6d  (%.0f/s)" % (i, kind(i), k, k / DUR))
print()
fb = sum(v for i, v in cnt.items() if i in FEEDBACK)
ct = sum(v for i, v in cnt.items() if i in CONTROL)
print("feedback = %d frames   control = %d frames" % (fb, ct))
if fb == 0:
    print(">>> No arm is reporting state -> no follower / normal arm on the bus.")
    print(">>> Consistent with both arms sitting in teach mode (0xFA).")
else:
    r = cnt.get(0x2A1, 0) / DUR
    print(">>> 0x2A1 = %.0f/s -> about %d arm(s) reporting" % (r, max(1, round(r / 200))))
    if r > 300:
        print(">>> EXPECTED ~200/s. Both arms took the reporting role: the master-slave")
        print(">>> pairing is broken. See TROUBLESHOOTING.md.")
if ct == 0:
    print(">>> control = 0: the master arm is powered off, or powered and resting.")
    print(">>> A teach-mode master is silent until moved -- required state for a policy run,")
    print(">>> but during recording you must see 0x155-0x157.")
