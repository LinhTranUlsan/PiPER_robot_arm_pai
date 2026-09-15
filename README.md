# PiPER + LeRobot

Imitation learning on AgileX PiPER arms: **record → train → rollout**.

LeRobot plugins (`piper_bus` robot, `piper_master` teleoperator) plus the CAN, camera, and
preflight tooling needed to run them on real hardware.

```
PiPER_robot_arm_pai/
├── plugins/            piper_bus · piper_master (+ 2 bimanual)
├── scripts/
│   ├── setup/          install.sh · detect_cameras.py · identify_cameras.py · set_cpu_performance.sh
│   ├── can/            fix_can.sh · can_env.sh · bus_scan.py · send_probe.py · tx_stress.py
│   ├── check/          preflight.py · motor_faults.py · joint_limits.py · powerup_trace.py
│   │                   check_dataset.py · check_dataset_multi.py · check_dataset_bimanual.py
│   │                   check_smoothness.py · check_smoothness_bimanual.py
│   │                   compare_arms.py · compare_firmware.py
│   └── deploy/         park_arm.py
├── patches/            one patch applied to the LeRobot checkout
├── env.sh.example      camera paths template -> copy to env.sh
├── env_all.sh.example  optional 4th "overview" camera -> copy to env_all.sh
├── deploy_spec.json    joint names, limits, named poses
└── requirements-pinned.txt
```

| doc | when |
|---|---|
| **README.md** | install, then record → train → rollout |
| **TROUBLESHOOTING.md** | something broke |

Reference rig: 2 clusters × 2 AgileX PiPER (firmware master–follower, one CAN bus each) ·
3 Intel RealSense D435 (1 fixed `front` + 1 wrist per cluster) · 2 gs_usb CAN adapters
(`1d50:606f`, 1 Mbps) · RTX 5090 · Ubuntu 24.04.

---

# PART 0 — INSTALL

## 0.1 System packages

```bash
sudo apt update
sudo apt install -y git v4l-utils can-utils linux-tools-common linux-tools-$(uname -r)
```

| package | needed for |
|---|---|
| `v4l-utils` | camera probing (`v4l2-ctl`) — `detect_cameras.py` cannot run without it |
| `can-utils` | CAN tooling |
| `linux-tools-*` | `cpupower` — needed to set the CPU governor |

## 0.2 Conda env

```bash
conda create -y -n piper_pai python=3.12      # LeRobot requires >= 3.12
conda activate piper_pai
conda install -y -c conda-forge ffmpeg        # 8.x -- torchcodec needs it to decode video
```

Do **not** name the env `lerobot` if another project already pip-installs a different
LeRobot checkout into an env by that name — `import lerobot` would resolve to that one.

## 0.3 Clone and install

```bash
git clone https://github.com/LinhTranUlsan/PiPER_robot_arm_pai.git
cd PiPER_robot_arm_pai

bash scripts/setup/install.sh                 # ACT + Diffusion Policy
# bash scripts/setup/install.sh --pi0         # also pi0 (VLA)
# bash scripts/setup/install.sh --pinned      # exact versions from requirements-pinned.txt
# bash scripts/setup/install.sh --bimanual    # also the two-cluster plugins
```

The script clones LeRobot at the verified commit `223a8ad1`, applies the Space-key patch,
installs the dependencies, clones `piper_sdk` at `c9e8a28`, installs the plugins editable,
and verifies LeRobot discovers them. It writes nothing outside the active conda env except
those two clones.

Put LeRobot elsewhere: `LEROBOT_DIR=/your/path bash scripts/setup/install.sh`

It must end with:

```
robot  : ['piper_bus']
teleop : ['piper_master']
torch  : 2.11.0+cu130 | cuda True | sm_120
```

## 0.4 CPU governor — REQUIRED

```bash
sudo bash scripts/setup/set_cpu_performance.sh
grep "cpu MHz" /proc/cpuinfo | awk '{s+=$4;n++} END{printf "%.0f MHz\n", s/n}'
```

Not optional. A 30 Hz control loop does a short burst of work then sleeps 25 ms, so average
utilisation looks low and `powersave` never ramps the clock. Ticks miss their deadline and
**the robot shakes**, while `load average` reads 0.4. The script installs a systemd unit so
the setting survives reboots.

## 0.5 CAN adapters

Find each adapter's USB serial, then record it:

```bash
sudo bash scripts/can/fix_can.sh                       # bring every CAN interface up
for i in $(ls /sys/class/net | grep -E '^can'); do
  echo -n "$i : "; udevadm info -p /sys/class/net/$i | sed -n 's/^E: ID_SERIAL_SHORT=//p'
done
```

Put the two serials into `scripts/can/can_env.sh` (replace the `REPLACE-WITH-...`
placeholders), then:

```bash
source scripts/can/can_env.sh                          # prints CAN_LEFT / CAN_RIGHT
```

That resolves each adapter **by serial at run time**, so it is correct whether the kernel
named it `can_right`, `can0`, or `can1`. Interface names are global to a machine, so this is
the only approach that survives a shared PC.

Optional, for stable names in `ip link` output:

```bash
# put your serials into scripts/can/80-piper-can.rules first
sudo bash scripts/can/install_udev.sh                  # pins can_left / can_right
```

Re-run `fix_can.sh` after: reboot · replugging an adapter · power-cycling an arm · any bus-off.
`ip link set <if> up` alone is **not** enough — a CAN interface cannot come up without a
bitrate, and that command fails quietly.

## 0.6 USB layout — read this before wiring

```
NO hub may carry BOTH a CAN adapter and a camera.
```

A gs_usb adapter is a **12 Mbit/s full-speed** device; a RealSense at 640x480 YUYV 30 fps
pushes ~18 MB/s. Share a hub and the adapter's transfers get delayed, CAN frames go out late,
and the controller bus-offs — while RX still reads a perfect 2422 fps. It looks exactly like
a CAN fault and is not one. See TROUBLESHOOTING.md.

```bash
lsusb -t | grep -B3 gs_usb                             # check the topology
```

Camera hubs must be **USB 3.0**: 1 camera = 18.4 MB/s, 2 = 37 MB/s, 3 = 55 MB/s, and a
USB 2.0 hub tops out around 35–40 MB/s.

## 0.7 Cameras

```bash
cp env.sh.example env.sh
python scripts/setup/detect_cameras.py                 # list colour cameras, note [0] [1] [2]
python scripts/setup/identify_cameras.py               # shake each arm; it identifies them
```

Read its output, then write the assignment with the **real indices** (`F R L` below are
placeholders, not values):

```
cam0 (8.3.1) -> LEFT  arm   8.48x   = WRIST LEFT
cam1 (8.3.2) NO RESPONSE            = FRONT (fixed, not on either arm)
cam2 (8.3.4) -> RIGHT arm  18.49x   = WRIST RIGHT
```

```bash
python scripts/setup/detect_cameras.py --front F --wrist-right R --wrist-left L --write
source env.sh
echo "$CAMS_RIGHT"                                     # must print the full dict
```

**Never guess roles from the listing order.** That order comes from `by-path` sorting, not
geometry. A swapped front/wrist means the policy looks at the wrong arm — it runs blind and
reports no error. `identify_cameras.py` decides by measurement: a wrist camera rides on J6,
so shaking that arm moves the whole frame.

`env.sh` is gitignored because `by-path` embeds this machine's PCI id and each camera's USB
port. Re-run 0.7 whenever a camera changes port. **Each camera must stay in its assigned
port** — label both ends of every cable.

### Optional: a 4th "overview" camera

A camera facing the rig from opposite `front`, seeing both arms and the box at once.

```bash
cp env_all.sh.example env_all.sh
python scripts/setup/detect_cameras.py          # find its by-path
# put that by-path into ALL= in env_all.sh, then:
source env.sh && source env_all.sh              # adds $ALL and $CAMS_*_ALL
```

| variable | cameras | streams |
|---|---|---|
| `$CAMS_RIGHT` / `$CAMS_LEFT` / `$CAMS_BOTH` | unchanged, no overview camera | 2 / 2 / 3 |
| `$CAMS_RIGHT_ALL` / `$CAMS_LEFT_ALL` | front + wrist + all | 3 |
| `$CAMS_BOTH_ALL` | front + both wrists + all | 4 |

It lives in a **separate file on purpose**: `detect_cameras.py --write` regenerates `env.sh`
from scratch and only knows the three standard roles, so anything added there is wiped on the
next run. The trade-off is that `ALL=` is updated by hand when that camera changes port.

Measured on the reference rig: 3 and 4 cameras both sustain the same throughput with **zero
truncated frames** — the 4th costs nothing. Still a USB 3.0 hub, still no CAN adapter on it.

## 0.8 Acceptance

```bash
source env.sh && source scripts/can/can_env.sh
python scripts/check/preflight.py --teleop --can $CAN_RIGHT
```

Must report **ALL PASS**, including `TX path healthy`.

---

# PART 1 — FULL PIPELINE: record → train → rollout

## 1.1 Open a session

Run this at the start of **every** terminal. The variables live only in that shell.

```bash
conda activate piper_pai
cd ~/PiPER_robot_arm_pai

source env.sh                                # camera paths -> $FRONT $WRIST_* $CAMS_*
source scripts/can/can_env.sh                # CAN adapters by USB serial -> $CAN_LEFT $CAN_RIGHT

export CAN_PORT=$CAN_RIGHT                   # park_arm.py reads this
CAN=$CAN_PORT
CAMS="$CAMS_RIGHT"                           # front + wrist_right
SPEC=deploy_spec.json                        # park_arm.py always opens this
TASK="pick the cube and place it"

echo "CAN=$CAN"                              # empty -> can_env.sh not sourced
echo "$CAMS"                                 # empty -> env.sh not sourced
```

**Never type an interface name directly.** Use `$CAN_RIGHT` / `$CAN_LEFT`.

For the LEFT cluster, change two lines only:

```bash
export CAN_PORT=$CAN_LEFT
CAMS="$CAMS_LEFT"
```

## 1.2 Power-on order

```
1. Follower FIRST
2. Master   SECOND      (only for RECORD; for ROLLOUT leave the master OFF)
3. Wait 10 s            a controller that is not ready yet is enough to bus-off the line
```

## 1.3 Bring up the bus

```bash
sudo bash scripts/can/fix_can.sh --can $CAN  # reload driver + set bitrate + measure
```

## 1.4 Camera checks

```bash
python scripts/setup/detect_cameras.py       # expect 3 colour cameras
python scripts/check/view_cameras.py 30      # live view in rerun for 30 s
```

Compare the `front` view against how it looked while recording: same angle, same distance,
same lighting. A moved `front` camera makes the policy run blind **with no error message**.

Moved a camera to a different USB port? Redo section 0.7. That needs **no code change**
(`env.sh` is generated) and **no re-recording** — the dataset stores images by feature name,
not by device path. Only a change in a camera's *physical viewpoint* invalidates the data.

## 1.5 Preflight

```bash
python scripts/check/motor_faults.py --can $CAN                     # 6/6 joints clean
python scripts/check/preflight.py --teleop --can $CAN               # ALL PASS
# python scripts/can/send_probe.py --can $CAN --mode both --cameras   # CLEAN
```

| line to look for | meaning |
|---|---|
| `0x2A1 = 200/s` | exactly one arm reporting — master–slave pairing intact |
| `driver_error_status: False` ×6 | no joint has latched a fault |
| `TX path healthy` | the **write** path works — RX says nothing about TX |
| `VERDICT: CLEAN` | survives the USB load a real rollout puts on the host |

`0x15x: 0/s` means **"master off OR powered and at rest"**. It does not prove the master is
off — a teach-mode master is silent until touched. Check the switch by hand.

## 1.6 Park to the demo start pose (pass)

```bash
# python scripts/deploy/park_arm.py --can $CAN --spec $SPEC \
#        --pose 0 0.3 -0.3 1.4 19.9 -2.0 --dry-run     # preview, sends nothing
# python scripts/deploy/park_arm.py --can $CAN --spec $SPEC \
#        --pose 0 0.3 -0.3 1.4 19.9 -2.0
```

`worst joint error` must be **< 0.01 rad**. Then press **Ctrl+C immediately**.

Leaving the arm holding a pose overheats J2/J5 and latches a driver fault. It is the single
most common cause of "the robot stopped working".

## 1.7 RECORD (master POWERED ON)

```bash
lerobot-record \
  --robot.type=piper_bus --robot.port=$CAN --robot.passive=true --robot.id=follower_right \
  --robot.cameras="$CAMS" \
  --teleop.type=piper_master --teleop.port=$CAN --teleop.id=master_right \
  --dataset.repo_id=$USER/piper_right \
  --dataset.no_stamp=true \
  --dataset.single_task="$TASK" \
  --dataset.num_episodes=100 --dataset.fps=30 \
  --dataset.episode_time_s=300 --dataset.reset_time_s=300 \
  --dataset.push_to_hub=false \
  --dataset.streaming_encoding=true --dataset.encoder_threads=2 \
  --dataset.num_image_writer_processes=1 \
  --display_data=true
```

- Continue a later session: **the same command + `--resume=true`**
- Keys: `space` end the current phase · `r` re-record the last episode · `q` quit
- `--robot.passive=true` is mandatory: the firmware master–slave link drives the follower,
  so the PC must stay off the wire
- Every episode must be complete: open gripper → approach → grasp → lift → move →
  **RELEASE** → home. Miss the release and press `r` at once
- Vary only the **object position** (4×5 grid over ~20×30 cm). Keep camera placement,
  lighting, and the way you finish constant
- End-of-session cadence must be **≥ 29.5 Hz** with `ticks over budget` near zero

## 1.8 Check the dataset

```bash
lerobot-dataset-viz --repo-id=$USER/piper_right --episode-index=0   # by eye
python scripts/check/check_dataset.py $USER/piper_right             # numbers
```

`check_dataset.py` prints the two numbers you need for rollout:
`|action-state|` → `--robot.max_relative_target`, and episode length → `--duration`.
Use what it prints; the values below were measured on the reference dataset.

Drop bad episodes:

```bash
lerobot-edit-dataset \
  --repo_id=$USER/piper_right --new_repo_id=$USER/piper_right_clean \
  --operation.type=delete_episodes --operation.episode_indices='[1,5,9]'
```

Dataset location: `~/.cache/huggingface/lerobot/$USER/piper_right/`

## 1.9 TRAIN

Steps = `16.7 × frames ÷ batch_size`. For 46 984 frames at batch 8 → 98 079.

```bash
# ACT -- 52M params, ~5h
lerobot-train --policy.type=act \
  --dataset.repo_id=$USER/piper_right --output_dir=outputs/act_right \
  --policy.push_to_hub=false --policy.device=cuda \
  --steps=125000 --wandb.enable=false

# Diffusion Policy -- 263M params, ~8h
lerobot-train --policy.type=diffusion \
  --dataset.repo_id=$USER/piper_right --output_dir=outputs/dp_right \
  --policy.push_to_hub=false --policy.device=cuda \
  --steps=125000 --wandb.enable=false
```

Run them one after another, never in parallel. Do not set `n_action_steps` or the scheduler
at train time — those are run-time parameters.

<details>
<summary>pi0 — fine-tune, ~12h, 30 GB VRAM</summary>

```bash
lerobot-train \
  --policy.path=lerobot/pi0_base \
  --policy.push_to_hub=false --policy.device=cuda \
  --policy.dtype=bfloat16 \
  --policy.train_expert_only=true \
  --dataset.repo_id=$USER/piper_right \
  --rename_map='{"observation.images.front": "observation.images.base_0_rgb", "observation.images.wrist": "observation.images.left_wrist_0_rgb"}' \
  --output_dir=outputs/pi0_right \
  --batch_size=8 --steps=30000 \
  --num_workers=4 --wandb.enable=false
```

pi0 uses `--policy.path=` (fine-tune), not `--policy.type=`. `--rename_map` is only accepted
alongside `--policy.path`, and must be identical at train and rollout time. One-time: accept
the licence at https://huggingface.co/google/paligemma-3b-pt-224 then `hf auth login`.
</details>

## 1.10 Smoothness check — MANDATORY before touching the robot

```bash
python scripts/check/check_smoothness.py \
  outputs/act_right/checkpoints/last/pretrained_model $USER/piper_right 50
```

The last argument is an **episode index** (0..N-1), not a count.

| ratio | meaning |
|---|---|
| ≤ 5× | smooth, safe to run |
| ≥ 10× | **will shake** — train longer |

Costs one minute and touches no hardware. Skipping it costs an afternoon tuning the wrong
control parameters.

## 1.11 ROLLOUT (master POWERED OFF)

```bash
# POWER THE MASTER ARM OFF by its switch, then:
python scripts/can/bus_scan.py 5 --can $CAN
python scripts/deploy/park_arm.py --can $CAN --spec $SPEC \
       --pose 0 0.3 -0.3 1.4 19.9 -2.0                # Ctrl+C once it reports the error
# Clear the workspace, place the object

lerobot-rollout \
  --strategy.type=base \
  --policy.path=outputs/act_right/checkpoints/last/pretrained_model \
  --policy.n_action_steps=50 \
  --robot.type=piper_bus --robot.port=$CAN --robot.passive=false --robot.id=follower_right \
  --robot.move_speed_pct=50 \
  --robot.max_relative_target=0.6 \
  --robot.cameras="$CAMS" \
  --task="$TASK" \
  --fps=30 --duration=20 \
  --return_to_initial_position=false \
  --display_data=true
```

Diffusion Policy — add three lines. Without DDIM it runs 100 DDPM steps = 603 ms per
inference, which stalls 56% of the control loop:

```bash
  --policy.noise_scheduler_type=DDIM \
  --policy.num_inference_steps=10 \
  --policy.n_action_steps=32 \
```

Add `--interactive=true` to start and stop by hand (`/start`, `/stop`) instead of a timer.

## 1.12 Always check after a run

```bash
ip -details -statistics link show $CAN | grep -A1 re-started   # bus-off must still be 0
python scripts/check/motor_faults.py --can $CAN                # no joint faulted
```

`bus.send()` only queues a frame on the socket and returns. The controller can fail below
that without the SDK noticing — **a clean log does not prove the commands arrived.**

## 1.13 Flag reference

| flag | value | why |
|---|---|---|
| `--robot.passive` | `true` record · `false` rollout | record: firmware master–slave is driving, PC stays quiet. rollout: PC drives, and `connect()` calls `EnableArm` |
| `--robot.max_relative_target` | `0.6` | measured: lead p99.9 = 0.1675, max 0.3232 rad. The code default 0.3 clips the peak |
| `--robot.move_speed_pct` | `50` | 30 saturates the gripper into oscillation; 60 overshoots |
| `--policy.n_action_steps` | `50` (ACT) | at 15 it re-plans every 0.5 s and judders in place |
| `--duration` | `20` | measured: p50 15.6 s · p95 17.5 s · max 23.6 s |
| `--policy.push_to_hub=false` | train | default `true` → `repo_id missing` error |
| `--dataset.push_to_hub=false` | record | default `true` → demands an HF login |
| `--dataset.no_stamp=true` | record | without it every run creates a new timestamped dataset |
| `--spec deploy_spec.json` | `park_arm.py` | it always opens this file, by a **relative** path |

---

# PART 2 — ROLLOUT FROM SCRATCH, RIGHT AFTER POWERING THE FOLLOWER ON

## 2.0 Power on, wait 10 s

```
Follower RIGHT cluster : ON
Master   RIGHT cluster : OFF     (by its switch -- go and check, do not trust any script)
Wait 10 s -- a controller that is not ready yet is enough to bus-off the line
```

## 2.1 Open the session

```bash
conda activate piper_pai
cd ~/PiPER_robot_arm_pai

source env.sh                                # camera paths
source scripts/can/can_env.sh                # prints CAN_LEFT / CAN_RIGHT

export CAN_PORT=$CAN_RIGHT                   # park_arm.py reads this
CAN=$CAN_PORT
CAMS="$CAMS_RIGHT"
SPEC=deploy_spec.json
TASK="pick the cube and place it"

echo "CAN=$CAN"                              # empty -> can_env.sh not sourced
echo "$CAMS"                                 # empty -> env.sh not sourced
```

## 2.2 Bus

```bash
sudo bash scripts/can/fix_can.sh --can $CAN  # reload driver + bitrate + measure
```

## 2.3 Four checks — all must pass

```bash
python scripts/can/bus_scan.py 5 --can $CAN                         # 0x2A1 = 200/s, control = 0
python scripts/check/motor_faults.py --can $CAN                     # 6/6 joints clean
python scripts/check/preflight.py --teleop --can $CAN               # ALL PASS
python scripts/can/send_probe.py --can $CAN --mode both --cameras   # CLEAN
```

| expected | meaning |
|---|---|
| `0x2A1 = 200/s` | exactly one arm reporting — pairing intact |
| `driver_error_status: False` ×6 | no latched joint fault |
| `TX path healthy` | the write path works |
| `VERDICT: CLEAN` | survives the rollout's USB load |

## 2.4 Park

```bash
python scripts/deploy/park_arm.py --can $CAN --spec $SPEC \
       --pose 0 0.3 -0.3 1.4 19.9 -2.0
```

`worst joint error` < 0.01 rad, then **Ctrl+C immediately**.

`drivers not enabled after 5s` → run `motor_faults.py`. If any joint shows
`motor_overheating: True`, power the follower off for 30 s and restart from 2.0.

## 2.5 Rollout

```bash
lerobot-rollout \
  --strategy.type=base \
  --policy.path=outputs/act_right/checkpoints/last/pretrained_model \
  --policy.n_action_steps=50 \
  --robot.type=piper_bus --robot.port=$CAN --robot.passive=false --robot.id=follower_right \
  --robot.move_speed_pct=50 --robot.max_relative_target=0.6 \
  --robot.cameras="$CAMS" --task="$TASK" \
  --fps=30 --duration=20 --return_to_initial_position=false --display_data=true
```

## 2.6 After the run

```bash
ip -details -statistics link show $CAN | grep -A1 re-started   # bus-off must still be 0
python scripts/check/motor_faults.py --can $CAN                # no joint faulted
```

## 2.7 Checklist

```
[ ] Master arm POWERED OFF -- by its switch, not by reading a script
[ ] Waited 10 s after powering the follower on
[ ] source env.sh  AND  source scripts/can/can_env.sh
[ ] sudo bash scripts/can/fix_can.sh --can $CAN
[ ] motor_faults  -> 6/6 clean
[ ] preflight     -> ALL PASS, including "TX path healthy"
[ ] send_probe --cameras -> CLEAN
[ ] park_arm      -> < 0.01 rad, then Ctrl+C AT ONCE
[ ] Workspace clear
[ ] After the run: bus-off still 0, no joint faulted
```

Anything fails → **TROUBLESHOOTING.md**.

---

# PART 3 — TWO CLUSTERS

**Two clusters must be two physically separate CAN buses.** No software configuration can
share one: PiPER uses fixed CAN IDs — every follower reports on `0x2A1-0x2A8` and every
master commands on `0x151-0x159`. Two nodes sending different payloads under the same ID
cannot be resolved by arbitration, and both bus-off.

Each bus needs exactly two 120 Ω terminators: **60 Ω** measured CAN_H to CAN_L with
everything powered off and both adapters unplugged. 40 Ω means one terminator too many —
the two harnesses are joined somewhere.

Verify they are separate. Power **only the right cluster**, plug in both adapters:

```bash
source scripts/can/can_env.sh
sudo bash scripts/can/fix_can.sh
python scripts/can/bus_scan.py 5 --can $CAN_LEFT      # must be 0 fps
python scripts/can/bus_scan.py 5 --can $CAN_RIGHT     # must be ~2420 fps
```

Any traffic on the unpowered cluster's bus means they are still joined.

## 3.1 One cluster at a time

Two terminals, each with its own variables and its own `repo_id` / `output_dir`:

```bash
# terminal RIGHT                          # terminal LEFT
export CAN_PORT=$CAN_RIGHT                export CAN_PORT=$CAN_LEFT
CAN=$CAN_PORT                             CAN=$CAN_PORT
CAMS="$CAMS_RIGHT"                        CAMS="$CAMS_LEFT"
```

Everything in PART 1 then applies unchanged.

---

## 3.2 Bimanual — both clusters as one robot

Use this when the task needs both arms in **one** dataset: they hand over an object, or they
act in sequence and the policy must learn the order. Two single-arm datasets cannot express
that — the timing between the arms is lost.

`piper_bimanual` wraps two `piper_bus` instances and prefixes every key `left_` / `right_`,
so the action vector is 14 wide: `left_joint_1.pos` … `left_gripper.pos`, then the same for
`right_`. It adds no writes of its own; each cluster keeps its own firmware master-slave link.

### Install the two extra plugins

```bash
bash scripts/setup/install.sh --bimanual        # or, if already installed:
pip install -e plugins/lerobot_robot_piper_bimanual --no-deps
pip install -e plugins/lerobot_teleoperator_piper_master_bimanual --no-deps
```

Verify — `piper_bimanual` and `piper_master_bimanual` must both appear:

```bash
cd /tmp && python -c "
from lerobot.utils.import_utils import register_third_party_plugins; register_third_party_plugins()
from lerobot.robots.config import RobotConfig
from lerobot.teleoperators.config import TeleoperatorConfig
print(sorted(c for c in RobotConfig.get_known_choices() if 'piper' in c))
print(sorted(c for c in TeleoperatorConfig.get_known_choices() if 'piper' in c))"
```

### Open a session

```bash
conda activate piper_pai && cd ~/PiPER_robot_arm_pai
source env.sh
source env_all.sh                            # skip if you are not using the 4th camera
source scripts/can/can_env.sh

CAMS="$CAMS_BOTH_ALL"                        # or "$CAMS_BOTH" for 3 cameras
SPEC=deploy_spec.json
REPO=$USER/piper_bimanual
TASK="right arm picks the red block into the box, then left arm picks the yellow block into the box"
```

### Power on and check BOTH clusters

```
Right cluster: follower ON -> wait 5 s -> master ON
Left  cluster: follower ON -> wait 5 s -> master ON
Wait another 10 s
```

```bash
sudo bash scripts/can/fix_can.sh
for C in $CAN_RIGHT $CAN_LEFT; do
  echo "===== $C ====="
  python scripts/can/bus_scan.py 5 --can $C            # 0x2A1 = 200/s on each
  python scripts/check/motor_faults.py --can $C        # 6/6 clean, ctrl_mode STANDBY
  python scripts/check/preflight.py --teleop --can $C  # ALL PASS
done
```

`ctrl_mode : STANDBY(0x0)` is required before recording. `CAN_CTRL(0x1)` means a previous
`park_arm` or rollout left that arm in CAN control and its master-slave link will track
poorly — power-cycle that follower.

Do **not** park before recording, for the same reason. Nudge each master instead and check
the follower has synced:

```bash
python scripts/check/joint_limits.py --can $CAN_RIGHT   # "gap" column ~0
python scripts/check/joint_limits.py --can $CAN_LEFT
```

### RECORD (both masters POWERED ON)

```bash
lerobot-record \
  --robot.type=piper_bimanual --robot.passive=true --robot.id=followers \
  --robot.left_port=$CAN_LEFT --robot.right_port=$CAN_RIGHT \
  --robot.cameras="$CAMS" \
  --teleop.type=piper_master_bimanual --teleop.id=masters \
  --teleop.left_port=$CAN_LEFT --teleop.right_port=$CAN_RIGHT \
  --dataset.repo_id=$REPO \
  --dataset.no_stamp=true \
  --dataset.single_task="$TASK" \
  --dataset.num_episodes=150 --dataset.fps=30 \
  --dataset.episode_time_s=300 --dataset.reset_time_s=300 \
  --dataset.push_to_hub=false \
  --dataset.streaming_encoding=true --dataset.encoder_threads=4 \
  --dataset.num_image_writer_processes=2 \
  --display_data=true
```

Pass `--robot.left_port` / `--robot.right_port` explicitly: the plugin defaults are the
literal strings `can_left` / `can_right`, and the kernel may have named them `can0` / `can1`.

On connect the teleoperator asks you to move each master in turn (30 s each). That is
deliberate — without it, a silent master records a column of zeros.

**Collection discipline.** Always the same order, every episode. The waiting arm must stay
completely still: its follower holding position while the other works is part of what the
policy learns. Vary only the object positions; the box, the cameras, and the lighting stay
put. Watch the cadence summary — 4 cameras plus 2 CAN buses is the heaviest configuration
here, and it must still read **≥ 29.5 Hz**.

### Check the dataset — per arm

```bash
python scripts/check/check_dataset_bimanual.py $REPO 1     # 1 pick-place cycle per arm
```

`check_dataset.py` reads a single gripper column and stops at the first cycle, so on a
two-arm dataset it cannot see that one arm never moved. This one splits the columns by name
and reports LEFT and RIGHT separately, with the numbers rollout needs:

```
--- LEFT ---   -> --robot.left_max_relative_target=0.5
--- RIGHT ---  -> --robot.right_max_relative_target=0.5
               -> --duration=35
Steps for 16.7 epochs (batch 8): 264,947
```

For several objects handled by one arm, `check_dataset_multi.py <repo_id> <n_objects>` does
the same counting on a single-arm dataset.

### TRAIN

```bash
lerobot-train --policy.type=act \
  --dataset.repo_id=$REPO --output_dir=outputs/act_bimanual \
  --policy.push_to_hub=false --policy.device=cuda \
  --steps=<from the checker> --wandb.enable=false
```

Episodes run about twice as long as a single-arm task, so the step count roughly doubles.
`--batch_size=16` halves the wall-clock and still fits comfortably in 32 GB.

### Smoothness — MANDATORY, per arm

```bash
python scripts/check/check_smoothness_bimanual.py \
  outputs/act_bimanual/checkpoints/last/pretrained_model $REPO 50
```

`check_smoothness.py` slices `[:, :6]`, which on a bimanual dataset is the **left arm only** —
a jittery right arm would go unreported. This version prints both and ends with `WORST ARM`.
That figure must be ≤ 5× before the robot is touched.

### ROLLOUT (both masters POWERED OFF)

```bash
for C in $CAN_RIGHT $CAN_LEFT; do
  python scripts/can/bus_scan.py 5 --can $C                        # control = 0
  python scripts/can/send_probe.py --can $C --mode both --cameras  # CLEAN
done
python scripts/deploy/park_arm.py --can $CAN_RIGHT --spec $SPEC --pose 0 0.3 -0.3 1.4 19.9 -2.0
python scripts/deploy/park_arm.py --can $CAN_LEFT  --spec $SPEC --pose 0 0.3 -0.3 1.4 19.9 -2.0
# Ctrl+C after each. Place the objects and the box, clear the space between the arms.

lerobot-rollout \
  --strategy.type=base \
  --policy.path=outputs/act_bimanual/checkpoints/last/pretrained_model \
  --policy.n_action_steps=50 \
  --robot.type=piper_bimanual --robot.passive=false --robot.id=followers \
  --robot.left_port=$CAN_LEFT --robot.right_port=$CAN_RIGHT \
  --robot.move_speed_pct=30 \
  --robot.left_max_relative_target=<from the checker> \
  --robot.right_max_relative_target=<from the checker> \
  --robot.cameras="$CAMS" --task="$TASK" \
  --fps=30 --duration=<from the checker> \
  --return_to_initial_position=false --display_data=true
```

Parking **is** correct here: a rollout wants the arms in CAN control, which is exactly what
`park_arm` leaves behind.

Size `--duration` from the checker's **max**, not its p95 suggestion: cutting at p95 chops
the slowest 5% of runs off mid-task, before the second arm releases.

Run the first few at `move_speed_pct=30` with a hand on the power switch. During recording
you drove the arms one at a time; a policy commands all 14 joints every tick, and the
"waiting" arm is being told to hold rather than genuinely idle. Stop with `Ctrl+C` if the
arms drift toward each other.

### After every run

```bash
for C in $CAN_RIGHT $CAN_LEFT; do
  ip -details -statistics link show $C | grep -A1 re-started   # bus-off must stay 0
  python scripts/check/motor_faults.py --can $C
done
```

---

## Licence and credits

This repository is Apache-2.0 (see `LICENSE`), matching LeRobot, which its plugins subclass.

LeRobot is Apache-2.0 (https://github.com/huggingface/lerobot).
`piper_sdk` is MIT, by AgileX Robotics (https://github.com/agilexrobotics/piper_sdk).
Neither is vendored here — `install.sh` clones both at pinned commits.
