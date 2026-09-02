# Symptom → cause → fix

Every entry below actually happened on the reference rig and was verified.

---

## Start here

```bash
source env.sh
python scripts/check/preflight.py --can can_right
```

It catches most problems before you waste time on them.

---

## Cameras

### `Failed to open OpenCVCamera(...)`

The camera re-enumerated and changed node. Common after a replug or a USB bus reset.

```bash
python scripts/setup/detect_cameras.py
python scripts/setup/detect_cameras.py --wrist N --front M --write
source env.sh
```

### `cameras: Failed when parsing value=''`

`$CAMS_RIGHT` is empty — you opened a new terminal without loading it.

```bash
source env.sh && echo "$CAMS_RIGHT"      # must print the full dict
```

### Two cameras but `by-id` shows only one

Neither reports a serial, so both generate the same name; udev keeps a single symlink, pointing at whichever was plugged in last.

**Do not use `by-id`.** This repository uses `by-path` (USB port topology). Trade-off: each camera must stay in its port, so label both cable ends.

To try to recover the serial: unplug the camera, wait 5 seconds, plug it back in — the next enumeration sometimes reads the descriptor.

### A camera drops out when the arm moves — `errno=19 (No such device)`

```
failed VIDIOC_REQBUFS: errno=19 (No such device)
... exceeded maximum consecutive read failures
```

`errno=19` means the device **left the USB bus mid-stream**. Not bandwidth, not a busy device:
the camera physically disconnected. Confirm it re-enumerated by checking the symlink
timestamp and node against what the run reported at startup:

```bash
ls -l --time-style=+%H:%M:%S /dev/v4l/by-path/ | grep <port>
```

A fresh timestamp matching the moment of failure, and a different `/dev/videoN` than the one
printed at `[ok]`, is a drop-and-reconnect. Observed on this rig: `wrist_left` (port `8.3`)
came up as `video4`, and after the failure the symlink was stamped seconds later pointing at
`video5`.

**Cause is mechanical.** A wrist camera rides on the arm, so every motion works its USB
cable. A marginal connector or a cable with no slack breaks the link exactly when the arm
moves — which is why the viewer survives until you touch the robot.

Fix the cable, not the software: give it a service loop so arm motion cannot pull the
connector, secure it along the links, and swap the cable if the drop repeats. A USB 3.0
extension with a locking connector helps on the wrist run.

**What it means for recording** — the camera read raises after a few consecutive failures and
the read thread dies, so the episode in progress is lost. Which datasets are exposed depends
on the camera set, not on which arm you drive:

| Camera set | Uses the wrist_left camera | Safe while it is flaky |
|---|---|---|
| `$CAMS_RIGHT` (front + wrist_right) | no | yes |
| `$CAMS_LEFT` (front + wrist_left) | **yes** | no |
| `$CAMS_BOTH` (bimanual, all three) | **yes** | no |

So a single-cluster right-arm recording is unaffected. Anything using the left wrist camera —
including every bimanual recording — needs the cable fixed first.

### `TimeoutError: Timed out waiting for frame` when opening the last camera

A **leftover process is still holding a camera**. The device stays busy and its isochronous
USB bandwidth stays reserved, so the next run opens the first cameras fine and fails on the
last one — which reads as broken hardware, and is not.

```bash
fuser -v /dev/video*        # who holds them
pkill -f rerun              # the usual culprit
sleep 3                     # let the kernel release the USB bandwidth
```

**Wait the few seconds.** Immediately after the kill the open still fails; the reservation is
not released synchronously. Measured on this rig: the retry straight after `pkill` failed,
and every attempt a few seconds later succeeded — both camera orders, three times each.

**Where the leftover comes from:** `rr.init(spawn=True)` forks a rerun viewer, and a child
inherits the parent's open file descriptors, including V4L2 handles that are not marked
CLOEXEC. If the script then dies, that rerun process outlives it still holding the cameras.
`view_cameras.py` now calls `rr.init` **before** opening any camera, so there is nothing to
inherit, and releases whatever opened if a later camera fails.

Confirm the links are healthy before suspecting the camera:

```bash
bash scripts/check/usb_speed.sh    # expect 5000 Mbps per camera
```

An intermittent failure right after replugging a camera is different — udev needs a moment to
recreate the `by-path` symlink. Wait ~5 s and retry.

### A solid green band across part of the image

The USB link delivered a **partial frame**. The tail of the buffer is never written, and
unwritten YUYV decodes to that green. The camera is fine.

`ImageBuffer` still reads the full 900 KiB and the frame rate still reads 30 — the buffer is
allocated at full size and simply arrives half-filled — so **neither fps nor buffer size
catches this**. `preflight.py` checks for it explicitly.

Check the negotiated link speed:

```bash
for v in $(ls /sys/class/video4linux); do
  p=$(readlink -f /sys/class/video4linux/$v/device)
  while [ ! -f "$p/speed" ]; do p=$(dirname $p); done
  echo "$v $(cat $p/speed) Mbps"
done
```

**480 Mbps means USB 2.0.** One 640×480@30 YUYV stream is ~147 Mbps, and USB 2.0 sustains
roughly 280–320 Mbps in practice, so two streams just fit and a third does not. Measured on
this rig: each camera alone is clean, all three at once truncates one of them by 261 of 480
rows, and either two-camera recording set is clean at 31 fps.

A D435 needs **its own USB 3.0 cable** — a USB 2.0 cable in a USB 3.0 port still negotiates
480 Mbps, which is the usual cause. Compare `lsusb -t`: if the hub appears on both a 480M
bus and a 5000M bus, the hub is USB 3.0 capable and the cable is the limit.

Until the cables are changed: recording is unaffected (it opens two streams), but
`view_cameras.py` opens every camera at once and will show the band on one of them.

### Camera path is right but the image is the wrong stream

You opened a depth or IR node. A RealSense exposes 4–6 nodes per camera; only the one with **YUYV** is RGB.

```bash
v4l2-ctl -d /dev/videoN --list-formats     # must list 'YUYV'
```

`detect_cameras.py` filters automatically — use it instead of guessing.

---

## CAN bus

### `OSError: [Errno 100] Network is down`

That bus is down, or it is up but **has no bitrate**.

```bash
sudo bash scripts/can/fix_can.sh
ip -details link show can_right | grep -E "state UP|bitrate"     # both must appear
```

**`ip link set <iface> up` on its own is not enough.** A CAN interface cannot come up without a bitrate, and the command reports an error — do not add `2>/dev/null` and assume it worked.

If the name in the error is `can0` or `can1`, that is the real problem — see *A healthy cluster reads 0 fps* below.

The bitrate is lost on: reboot, adapter replug, `link down`.

### bus-off climbs during a rollout but a probe without cameras is clean

**The CAN adapter is sharing a USB hub with a camera.** This is the highest-value entry in
this file: it cost a full day and looks exactly like a CAN fault, a wiring fault, or a code
bug — none of which it is.

The gs_usb adapter is a **12 Mbit/s full-speed** USB device. A RealSense at 640x480 YUYV
30 fps pushes ~18 MB/s. Share a hub and the adapter's URBs get delayed in the host
controller's schedule, CAN frames go out late, and the controller bus-offs — while RX stays
a perfect 2422 fps, because listening does not need the transmit schedule.

Reproduce it in 10 seconds without touching the robot. Reload between runs so the counters
start from zero:

```bash
sudo bash scripts/can/fix_can.sh --can $CAN_RIGHT
python scripts/can/send_probe.py --can $CAN_RIGHT --mode both              # no cameras
sudo bash scripts/can/fix_can.sh --can $CAN_RIGHT
python scripts/can/send_probe.py --can $CAN_RIGHT --mode both --cameras    # with cameras
```

CLEAN then BUS-OFF confirms it. Measured on the reference rig:

| `send_probe --mode both` | bus-off |
|---|---|
| no cameras | 0 |
| with cameras, sharing a hub | 17 806 |
| with cameras, adapter moved off that hub | 0 |

Look at the topology and move the adapter:

```bash
lsusb -t | grep -B3 gs_usb        # no hub may hold BOTH a CAN adapter and a camera
```

Plug the CAN adapters straight into the motherboard, or onto a hub that carries nothing but
CAN adapters. **No code change is needed** — `scripts/can/can_env.sh` resolves adapters by
USB serial, so the interface name may change freely.

Still bus-off after separating them, in order of cost: switch the cameras to MJPG (~10x less
bandwidth), then move the cameras to a separate PCIe USB card.

### A driver refuses to enable — `Drivers still not enabled: [True, False, ...]`

`False` marks the driver that refused. The usual cause is a **latched overheat flag** on J2
or J5 — the two joints that carry the most gravity load.

```bash
python scripts/check/motor_faults.py --can $CAN_RIGHT
```

| flag | fix |
|---|---|
| `motor_overheating: True` | **power the follower off for 30 s**, on again, wait 10 s |
| `driver_overcurrent: True` | the arm is mechanically blocked — clear it, then power-cycle |
| `collision_status: True` | the arm hit something — power-cycle |
| `voltage_too_low: True` | check the 24 V supply |
| all `False` but still not enabling | the master arm is still powered and holding the bus |

**A low temperature reading next to `motor_overheating: True` is normal.** The flag latches
and PiPER keeps it until the arm is powered off. Do not judge by the temperature number.

What latches it: leaving the arm holding a pose. `park_arm.py` ends with *"Holding this
pose"* and holds indefinitely. **Press Ctrl+C as soon as it reports the joint error.** Park
in a folded pose (`--park`) rather than stretched out — J2's moment is largest when extended.

Straight after a power cycle the drivers need a few seconds to come up. `park_arm.py` waits
for `GetArmEnableStatus()` to confirm all six and fails loudly if they do not; a command sent
to a disabled driver is swallowed silently and looks like a mechanical fault.

### `SendCanMessage(SEND_MESSAGE_FAILED (100017))` flooding the log

Every **write** to the bus is failing while reads are fine. Nothing to do with cameras or the
dataset — it is the CAN transmit path, and it only shows up once something writes, i.e. a
rollout with `--robot.passive=false`. Recording (`passive=true`) never touches TX, so the
fault can sit unnoticed for a whole session.

```bash
ip -details -statistics link show can_right | grep -A1 re-started
cat /sys/class/net/can_right/statistics/tx_packets   # twice, a few seconds apart
```

The signature is a healthy RX beside a dead TX:

```
can state ERROR-WARNING restart-ms 0
re-started bus-errors arbit-lost error-warn error-pass bus-off
0          0          0          3202       72302      71292
RX 2425 fps   TX 0 fps
```

**`restart-ms 0` is the reason it never recovers.** One transient TX fault — the arm's
controller not yet ready right after power-on is enough — drives the counter to bus-off, and
with automatic recovery disabled the controller stays there. The arm keeps reporting at
200/s so everything *looks* alive, while the SDK retries forever.

**The gs_usb adapters on this rig cannot do automatic recovery.** Asking for it fails:

```
Error: Device doesn't support restart from Bus Off.
```

So `restart-ms` stays 0 whatever you do, and clearing a bus-off is **manual** — reload the
driver and set the bitrate again:

```bash
sudo bash scripts/can/fix_can.sh --can can_right
```

`fix_can.sh` requests `restart-ms` separately from the bitrate and tolerates the refusal. Do
not chain the two into one `ip link set`: the refusal then fails the whole command and the
interface comes up with **no bitrate at all**, which reads as a silent bus.

Those counters are **cumulative totals, not current state**. A large `bus-off` next to
`can state ERROR-WARNING` means it happened and passed; check `tx_packets` actually advancing
before concluding the bus is still broken.

Give the arm a few seconds after power-on before starting a rollout, and never start one while
another process still holds the bus.

### Silent bus (0 fps)

**Read the error counters first — they tell you which half of the problem you have.**

```bash
ip -details -statistics link show can_right | grep -A1 re-started
```

| Counters | Meaning |
|---|---|
| `bus-errors` / `error-warn` / `bus-off` **non-zero** | Electrical or protocol fault: wrong bitrate, CAN-H/CAN-L swapped, missing termination, or a wedged adapter. Reload the driver. |
| **all zero**, with `state ERROR-ACTIVE` | The adapter is healthy and nothing is transmitting. It is not a bus fault — look at power and at *which bus you are on*. |

`ERROR-ACTIVE` is the **normal** operating state of a working CAN node, not an error.

All-zero counters, in order of how cheap they are to check:

1. **That cluster is not powered on**, or its e-stop is engaged. A PiPER emits nothing without power.
2. **You are on the wrong bus.** See below — this is the one that wastes an afternoon.

```bash
lsusb | grep 1d50:606f                  # are both adapters still there
sudo bash scripts/can/fix_can.sh        # reload the driver, measure every bus
```

Reloading the driver is the only reliable way to clear a wedged adapter after a bus-off — `link down/up` does **not** reset the error counters.

### A healthy cluster reads 0 fps — you are on the other bus

This rig has **two adapters**, one per master-follower cluster. The kernel names gs_usb interfaces `can0`, `can1`, … **in USB enumeration order, which is not deterministic** — and `fix_can.sh` reloads the driver, which re-enumerates both. So `can0` and `can1` can swap between two runs of the same script. Every symptom follows: a healthy cluster reports 0 fps, or a command silently drives the arm you were not looking at.

**The fix is to stop using kernel names.** `80-piper-can.rules` pins each adapter to `can_left` / `can_right` by its USB serial:

```bash
sudo bash scripts/can/install_udev.sh   # once per machine
ip -br link show type can               # must show can_left / can_right, never can0/can1
```

The names are outside the `canN` pattern on purpose: the kernel only auto-assigns `canN`, so a rename into that namespace can collide with a name it already handed the other adapter.

If you still see `can0`/`can1`, the rule did not match. Compare serials:

```bash
for i in $(ip -br link show type can | awk '{print $1}'); do
  echo "$i $(udevadm info -p /sys/class/net/$i | sed -n 's/^E: ID_SERIAL_SHORT=//p')"
done
```

A serial not listed in the rule means a replaced adapter — add it and re-run `install_udev.sh`.

### Which bus am I actually on

```bash
sudo bash scripts/can/fix_can.sh                     # every bus, side by side
sudo bash scripts/can/fix_can.sh --can can_right     # or just one  (--can right works too)
python scripts/can/bus_scan.py 5 --can can_left
```

`bus_scan.py` and `park_arm.py` **refuse to guess** when more than one bus is up. That is deliberate: `park_arm.py` moves a real arm, and defaulting to the wrong cluster parks the wrong one.

### `0x2A1` at ~400/s instead of ~200/s

Both arms are in the reporting role, so the master–slave pairing is broken.

⚠️ **`MasterSlaveConfig` is a broadcast command** — running it while both arms are powered changes **both** roles and breaks a working rig. Power down one arm before changing roles.

### The log is clean but nothing reaches the arm

`bus.send()` only queues a frame on the socket and returns. The controller can fail below
that without the SDK ever noticing, so a rollout can report `send mean 0.16 ms` over 700
ticks while not one command lands. **A clean log does not prove the commands arrived.**

Check the two things that do prove it:

```bash
ip -details -statistics link show $CAN_RIGHT | grep -A1 re-started   # bus-off must stay 0
python scripts/check/motor_faults.py --can $CAN_RIGHT               # drivers enabled, no faults
```

Recording never writes to the bus (`passive=true`), so a dead transmit path can sit unnoticed
for a whole session and only surface during a rollout. `preflight.py` tests the write path
explicitly (`TX path healthy`) for that reason.

### Arm jerks or fights during a policy run

The master arm is still transmitting.

```bash
python scripts/can/bus_scan.py 5 --can can_right   # MUST show control = 0 frames
```

Power off the master arm before running a policy. No exceptions.

---

## Robot shakes

**This is the most expensive failure to diagnose. Work through it in this order.**

### 1. CPU governor — check this first

```bash
grep "cpu MHz" /proc/cpuinfo | awk '{s+=$4;n++} END{printf "%.0f MHz\n", s/n}'
```

Below 2500 MHz is broken (a floor set on the 4.6 GHz Xeon; this machine should read
~4900 MHz, so anything under ~3000 MHz here means the governor never ramped):
```bash
sudo bash scripts/setup/set_cpu_performance.sh
```

`powersave` pins the CPU at 1200 MHz because a 30 Hz loop sleeps 25 ms per tick, so average utilisation looks low. Ticks miss their deadline, commands reach the arm unevenly, and it shakes. `load average` still reads 0.4, which makes this very easy to miss.

### 2. Policy smoothness — measure before blaming the hardware

```bash
python scripts/check/check_smoothness.py <checkpoint> <repo_id> 50
```

| ratio | meaning |
|---|---|
| **≤ 5×** | smooth (reference good model: 3.5×) |
| 5–10× | borderline |
| **≥ 10×** | **will shake — train longer** |

Measured: a 40,000-step model scored **15.9×** and shook; 100,000 steps scored **3.5×** and ran smoothly. Same dataset, same algorithm.

`check_dataset.py` computes the step count needed for 16.7 epochs.

### 3. Clamp saturation

Count the `Relative goal position magnitude had to be clamped` lines:

| | meaning |
|---|---|
| Almost none | correct |
| A few at startup, then quiet | normal |
| **Firing continuously all run** | the arm cannot keep up |

When the clamp saturates continuously, `command = measured_position + 0.3` — the command tracks the measurement itself, forming a feedback loop that produces oscillation.

Fix: raise `--robot.move_speed_pct` (30 → 50 → 60) and/or `--robot.max_relative_target`. `check_dataset.py` gives the right value.

### 4. Cadence

Read the summary at the end of the run:
```
effective cadence: xx.xx Hz
ticks over the 33.3 ms work budget: x/xxx
```

| | |
|---|---|
| ≥ 29.5 Hz, over budget < 1% | good |
| < 29 Hz or over 5% | find the background job: `ps -eo pid,pcpu,etime,args --sort=-pcpu \| head` |

---

## Training

### `ValueError: 'repo_id' argument missing`

Policies default to `push_to_hub=True`. Add:
```bash
--policy.push_to_hub=false
```

### π0: `gated repo` 401 / 403

| code | meaning |
|---|---|
| **401** | not logged in → `hf auth login` |
| **403** | logged in but **license not accepted** on the model page |

Visit https://huggingface.co/google/paligemma-3b-pt-224 and accept. Do not use an ungated tokenizer mirror — that circumvents the Gemma license.

### π0: CUDA out of memory

```bash
--policy.dtype=bfloat16 --policy.train_expert_only=true --batch_size=8
```

Measured on the previous 50.9 GB A6000: `train_expert_only` at batch 8 = 30.1 GB · batch 32 =
**OOM**. This machine has a **32.6 GB RTX 5090**, so batch 8 already sits within ~2.5 GB of the
ceiling — drop to `--batch_size=4` here, and check nothing else holds VRAM:
`nvidia-smi --query-compute-apps=pid,used_memory --format=csv`

### Training unexpectedly slow

Something else is using the GPU or CPU:
```bash
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
ps -eo pid,pcpu,etime,args --sort=-pcpu --no-headers | head
```

Never train two models in parallel — contention makes both slower than running them back to back.

---

## Rollout

### The robot stops, then returns home on its own

Not the policy. That is `return_to_initial_position=True` firing when `--duration` expires.

```bash
--return_to_initial_position=false
```

Set `--duration` from `check_dataset.py` (episode p95 + 3 s). Too generous and the policy flails once the task is done, because an empty scene is entirely out of its training distribution.

### ACT grasps, then jitters in place

Multimodal data: some demonstrations teach "grasp, then don't place". ACT averages the conflicting modes.

Workaround: `--policy.n_action_steps=50` (instead of 15) so it commits longer per decision.
Real fix: use `check_dataset.py` to find and remove episodes missing the release, then retrain.

Test for clean data: it runs correctly at **both** `n_action_steps=15` and `50`.

### Diffusion Policy stutters badly

Missing the DDIM flags. The default is DDPM with 100 steps = **603 ms** per inference, stalling 56% of the loop.

```bash
--policy.noise_scheduler_type=DDIM --policy.num_inference_steps=10
```

### π0 stutters badly

RTC is mandatory. π0 takes **274 ms** per inference; under `sync` the arm freezes 0.27 s every 1.67 s.

```bash
--inference.type=rtc --inference.rtc.execution_horizon=10
```

Lowering `num_inference_steps` barely helps π0: the cost is **111 ms fixed** (the VLM pass) plus 16.3 ms per step. Unlike Diffusion Policy, where the reduction is nearly linear.

### π0 behaves completely wrongly

The rollout `--rename_map` must be **identical** to the one used at training. Otherwise images land in empty slots and the model sees nothing.

---

## Rerun

### `Exceeded gRPC proxy server memory limit (1.0 GiB)`

**Harmless.** The display buffer fills after ~19 s with two 640×480@30 streams (~55 MB/s). It drops the oldest frames, so you only lose scroll-back. Datasets and rollouts are unaffected.

To silence it: `--display_compressed_images=true`

### No rerun window appears

`rr.spawn()` attaches to an already-running viewer. Kill the old one:
```bash
pkill rerun
```

---

## When stuck

1. `python scripts/check/preflight.py --can can_right`
2. `python scripts/check/check_dataset.py <repo_id>`
3. `python scripts/check/check_smoothness.py <ckpt> <repo_id>`

These three narrow down almost anything: hardware, data, or model.
