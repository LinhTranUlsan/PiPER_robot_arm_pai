# PiPER + LeRobot

Imitation learning on AgileX PiPER arms: **record → train → rollout**.

LeRobot plugins (`piper_bus` robot, `piper_master` teleoperator) plus the CAN, camera, and
preflight tooling needed to run them on real hardware.

```
PiPER_robot_arm_pai/
├── plugins/            piper_bus · piper_master (+ 2 bimanual)
├── scripts/
│   ├── setup/          install.sh · bootstrap.sh · detect_cameras.py · identify_cameras.py
│   │                   set_cpu_performance.sh
│   ├── can/            fix_can.sh · can_env.sh · bus_scan.py · send_probe.py · tx_stress.py
│   ├── check/          preflight.py · motor_faults.py · joint_limits.py · powerup_trace.py
│   │                   check_dataset.py · check_dataset_multi.py · check_dataset_bimanual.py
│   │                   check_smoothness.py · check_smoothness_bimanual.py
│   │                   compare_arms.py · compare_firmware.py
│   └── deploy/         park_arm.py
├── patches/            one patch applied to the LeRobot checkout
├── env.sh.example      camera paths template -> bootstrap.sh copies it to env.sh
├── env_all.sh.example  optional 4th "overview" camera -> copy to env_all.sh
├── deploy_spec.json    joint names, limits, named poses
└── requirements-pinned.txt
```

| doc | when |
|---|---|
| **README.md** | SETUP once, then PART 1 (one arm pair) or PART 2 (two arm pairs) |
| **TROUBLESHOOTING.md** | something broke |

Reference rig: 2 clusters × 2 AgileX PiPER (firmware master–follower, one CAN bus each) ·
3 Intel RealSense D435 (1 fixed `front` + 1 wrist per cluster) · 2 gs_usb CAN adapters
(`1d50:606f`, 1 Mbps) · RTX 5090 · Ubuntu 24.04.

---

# SETUP — once per machine

Shared by PART 1 and PART 2. Run the steps in order.

## Step 1 — System packages

```bash
sudo apt update
sudo apt install -y git v4l-utils can-utils linux-tools-common linux-tools-$(uname -r)
```

`v4l-utils` = camera probing · `can-utils` = CAN tooling · `linux-tools-*` = `cpupower`.

## Step 2 — Conda env

```bash
conda create -y -n piper_pai python=3.12      # LeRobot requires >= 3.12
conda activate piper_pai
conda install -y -c conda-forge ffmpeg        # 8.x -- torchcodec needs it to decode video
```

Do **not** name the env `lerobot` — `import lerobot` would resolve to another checkout.

## Step 3 — Clone and install

```bash
git clone https://github.com/LinhTranUlsan/PiPER_robot_arm_pai.git
cd PiPER_robot_arm_pai

bash scripts/setup/install.sh                 # ACT + Diffusion Policy
# bash scripts/setup/install.sh --pi0         # also pi0 (VLA)
# bash scripts/setup/install.sh --pinned      # exact versions from requirements-pinned.txt
# bash scripts/setup/install.sh --bimanual    # also the two-cluster plugins (PART 2)
```

Must end with `robot : ['piper_bus']` · `teleop : ['piper_master']` · `cuda True`.

## Step 4 — CPU governor (REQUIRED)

```bash
sudo bash scripts/setup/set_cpu_performance.sh
```

A 30 Hz loop sleeps 25 ms per tick, so `powersave` never ramps the clock and **the robot
shakes** while `load average` reads 0.4.

## Step 5 — Bootstrap this machine

```bash
bash scripts/setup/bootstrap.sh --from /home/pai/linh/PiPER/lerobot/piper   # reuse a working checkout
# bash scripts/setup/bootstrap.sh                                          # first machine, no source to copy
```

Reads each CAN adapter's USB serial from sysfs, builds `env.sh` / `env_all.sh`, then verifies:

```
== 3. Verify ==
   FRONT        ok
   WRIST_RIGHT  ok
   WRIST_LEFT   ok
   ALL          ok
   CAN_LEFT     -> can_left
   CAN_RIGHT    -> can_right

== READY ==
```

**Wait for `== READY ==`.** Anything less names what is missing and prints the fix, and exits
non-zero.

---

# PART 1 — ONE ARM PAIR

One master–follower cluster on one CAN bus: record → train → rollout.

## 1.1 Open a session

Run this at the start of **every** terminal. The variables live only in that shell.

```bash
conda activate piper_pai
cd ~/PiPER_robot_arm_pai

source env_all.sh                            # pulls in env.sh, then adds $ALL + $CAMS_*_ALL
# source env.sh                              # use this instead if you have no 4th camera
source scripts/can/can_env.sh                # CAN adapters by USB serial -> $CAN_LEFT $CAN_RIGHT

export CAN_PORT=$CAN_RIGHT                   # park_arm.py reads this
CAN=$CAN_PORT
CAMS="$CAMS_RIGHT_ALL"                       # front + wrist_right + all
# CAMS="$CAMS_RIGHT"                         # front + wrist_right, no 4th camera
SPEC=deploy_spec.json                        # park_arm.py always opens this
TASK="pick the cube and place it"

echo "CAN=$CAN"                              # empty -> can_env.sh not sourced
echo "$CAMS"                                 # empty -> env.sh not sourced
```

LEFT cluster — change two lines only:

```bash
export CAN_PORT=$CAN_LEFT
CAMS="$CAMS_LEFT_ALL"
```

**Never type an interface name directly.** Use `$CAN_RIGHT` / `$CAN_LEFT`.

## 1.2 Power-on order

```
1. Follower FIRST
2. Master   SECOND      (RECORD only; for ROLLOUT leave the master OFF)
3. Wait 10 s            a controller that is not ready yet is enough to bus-off the line
```

## 1.3 Bring up the bus

```bash
sudo bash scripts/can/fix_can.sh --can $CAN  # reload driver + set bitrate + measure
```

## 1.4 Checks

```bash
python scripts/setup/detect_cameras.py                              # 4 with the overview camera, else 3
python scripts/check/view_cameras.py 30                             # live view of every named camera
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

The `front` view must match how it looked while recording — same angle, distance, lighting.
`0x15x: 0/s` means "master off **or** powered and at rest"; check the switch by hand.

## 1.5 RECORD (master POWERED ON)

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
- `--robot.passive=true` is mandatory — the firmware master–slave link drives the follower
- Every episode complete: open → approach → grasp → lift → move → **RELEASE** → home.
  Miss the release and press `r` at once
- Vary only the **object position** (4×5 grid over ~20×30 cm); keep cameras and lighting fixed
- End-of-session cadence must be **≥ 29.5 Hz**, `ticks over budget` near zero

## 1.6 Check the dataset

```bash
lerobot-dataset-viz --repo-id=$USER/piper_right --episode-index=0   # by eye
python scripts/check/check_dataset.py $USER/piper_right             # numbers
```

It prints the two numbers rollout needs: `|action-state|` → `--robot.max_relative_target`,
episode length → `--duration`. Dataset lives in `~/.cache/huggingface/lerobot/$USER/piper_right/`.

Drop bad episodes:

```bash
lerobot-edit-dataset \
  --repo_id=$USER/piper_right --new_repo_id=$USER/piper_right_clean \
  --operation.type=delete_episodes --operation.episode_indices='[1,5,9]'
```

## 1.7 TRAIN

Steps = `16.7 × frames ÷ batch_size`. Run them one after another, never in parallel.

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

Do not set `n_action_steps` or the scheduler at train time — those are run-time parameters.

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

## 1.8 Smoothness — MANDATORY before touching the robot

```bash
python scripts/check/check_smoothness.py \
  outputs/act_right/checkpoints/last/pretrained_model $USER/piper_right 50
```

Last argument is an **episode index** (0..N-1), not a count.
**≤ 5×** = smooth, safe to run · **≥ 10×** = will shake, train longer.

## 1.9 ROLLOUT (master POWERED OFF)

Power the master arm off **by its switch**, then wait 10 s and run all four checks:

```bash
python scripts/can/bus_scan.py 5 --can $CAN                         # 0x2A1 = 200/s, control = 0
python scripts/check/motor_faults.py --can $CAN                     # 6/6 joints clean
python scripts/check/preflight.py --teleop --can $CAN               # ALL PASS
python scripts/can/send_probe.py --can $CAN --mode both --cameras   # CLEAN
```


`worst joint error` < 0.01 rad, then **Ctrl+C immediately** — holding the pose overheats
J2/J5 and latches a driver fault. `drivers not enabled after 5s` → run `motor_faults.py`; on
`motor_overheating: True` power the follower off for 30 s and start this section again.

```bash
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

Diffusion Policy — add three lines, or 100 DDPM steps stall 56% of the control loop:

```bash
  --policy.noise_scheduler_type=DDIM \
  --policy.num_inference_steps=10 \
  --policy.n_action_steps=32 \
```

Add `--interactive=true` to start and stop by hand (`/start`, `/stop`) instead of a timer.

## 1.10 After every run

```bash
ip -details -statistics link show $CAN | grep -A1 re-started   # bus-off must still be 0
python scripts/check/motor_faults.py --can $CAN                # no joint faulted
```

`bus.send()` only queues a frame and returns — **a clean log does not prove the commands
arrived.**


## 1.11 The robot arm off. After that only turn follower on
### 1.11.1 Power follower robot arm on, wait 10 s
```bash
Follower RIGHT cluster : ON
Master   RIGHT cluster : OFF     (by its switch -- go and check, do not trust any script)
Wait 10 s -- a controller that is not ready yet is enough to bus-off the line
```
### 1.11.2 Open the terminal
```bash
conda activate piper_pai
cd ~/PiPER_robot_arm_pai

source env_all.sh                            # pulls in env.sh, then adds $ALL + $CAMS_*_ALL
# source env.sh                              # use this instead if you have no 4th camera
source scripts/can/can_env.sh                # prints CAN_LEFT / CAN_RIGHT

export CAN_PORT=$CAN_RIGHT                   # park_arm.py reads this
CAN=$CAN_PORT
CAMS="$CAMS_RIGHT_ALL"                       # must match what the dataset was recorded with
SPEC=deploy_spec.json
TASK="pick the cube and place it"

echo "CAN=$CAN"                              # empty -> can_env.sh not sourced
echo "$CAMS"                                 # empty -> env.sh not sourced
```

### 1.11.3 Bus
```bash
sudo bash scripts/can/fix_can.sh --can $CAN  # reload driver + bitrate + measure
```

### 1.11.4 Four checks — all must pass
```bash
python scripts/can/bus_scan.py 5 --can $CAN                         # 0x2A1 = 200/s, control = 0
python scripts/check/motor_faults.py --can $CAN                     # 6/6 joints clean
python scripts/check/preflight.py --teleop --can $CAN               # ALL PASS
python scripts/can/send_probe.py --can $CAN --mode both --cameras   # CLEAN
```

### 1.11.5 Park
```bash
python scripts/deploy/park_arm.py --can $CAN --spec $SPEC \
       --pose 0 0.3 -0.3 1.4 19.9 -2.0
```

### 1.11.6 Rollout
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

## 1.12 Rollout checklist

```
[ ] Master arm POWERED OFF -- by its switch, not by reading a script
[ ] Waited 10 s after powering the follower on
[ ] source env_all.sh  AND  source scripts/can/can_env.sh
[ ] sudo bash scripts/can/fix_can.sh --can $CAN
[ ] motor_faults  -> 6/6 clean
[ ] preflight     -> ALL PASS, including "TX path healthy"
[ ] send_probe --cameras -> CLEAN
[ ] park_arm      -> < 0.01 rad, then Ctrl+C AT ONCE
[ ] Workspace clear
[ ] After the run: bus-off still 0, no joint faulted
```

Anything fails → **TROUBLESHOOTING.md**.

## 1.13 Flag reference

| flag | value | why |
|---|---|---|
| `--robot.passive` | `true` record · `false` rollout | record: firmware master–slave drives, PC stays quiet. rollout: PC drives |
| `--robot.max_relative_target` | `0.6` | measured lead p99.9 = 0.1675, max 0.3232 rad; the default 0.3 clips the peak |
| `--robot.move_speed_pct` | `50` | 30 saturates the gripper into oscillation; 60 overshoots |
| `--policy.n_action_steps` | `50` (ACT) | at 15 it re-plans every 0.5 s and judders in place |
| `--duration` | `20` | measured p50 15.6 s · p95 17.5 s · max 23.6 s |
| `--policy.push_to_hub=false` | train | default `true` → `repo_id missing` error |
| `--dataset.push_to_hub=false` | record | default `true` → demands an HF login |
| `--dataset.no_stamp=true` | record | without it every run creates a new timestamped dataset |
| `--spec deploy_spec.json` | `park_arm.py` | it always opens this file, by a **relative** path |

---

# PART 2 — TWO ARM PAIRS

## 2.1 Two separate CAN buses — requirement

**No software configuration can share one bus.** PiPER uses fixed CAN IDs: every follower
reports on `0x2A1-0x2A8`, every master commands on `0x151-0x159`. Two nodes under the same ID
cannot be resolved by arbitration, and both bus-off.

Each bus needs exactly two 120 Ω terminators: **60 Ω** CAN_H to CAN_L, everything powered off
and both adapters unplugged. 40 Ω = one terminator too many, the harnesses are joined.

Verify they are separate — power **only the right cluster**, plug in both adapters:

```bash
conda activate piper_pai
source scripts/can/can_env.sh
sudo bash scripts/can/fix_can.sh
python scripts/can/bus_scan.py 5 --can $CAN_LEFT      # must be 0 fps
python scripts/can/bus_scan.py 5 --can $CAN_RIGHT     # must be ~2420 fps
```

Any traffic on the unpowered cluster's bus means they are still joined.

## 2.2 One cluster at a time — two independent datasets

Two terminals, each with its own variables and its own `repo_id` / `output_dir`:

```bash
# terminal RIGHT                          
export CAN_PORT=$CAN_RIGHT                
CAN=$CAN_PORT                             
CAMS="$CAMS_RIGHT_ALL"  
                  
# terminal LEFT
export CAN_PORT=$CAN_LEFT
CAN=$CAN_PORT
CAMS="$CAMS_LEFT_ALL"
```

All of PART 1 then applies unchanged.

## 2.3 Bimanual — both clusters as one robot

Use this when the task needs both arms in **one** dataset: they hand over an object, or act in
a sequence the policy must learn. `piper_bimanual` wraps two `piper_bus` instances and
prefixes every key `left_` / `right_`, so the action vector is 14 wide. It adds no writes of
its own; each cluster keeps its own firmware master-slave link.

### 2.3.1 Install the two extra plugins

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

### 2.3.2 Open a session

```bash
conda activate piper_pai && cd ~/PiPER_robot_arm_pai
source env_all.sh                            # pulls in env.sh, then adds $ALL + $CAMS_*_ALL
source scripts/can/can_env.sh

CAMS="$CAMS_BOTH_ALL"                        # front + both wrists + all (4 streams)
# CAMS="$CAMS_BOTH"                          # front + both wrists, no 4th camera
SPEC=deploy_spec.json
REPO=$USER/piper_bimanual
TASK="right arm picks the red block into the box, then left arm picks the yellow block into the box"
```

### 2.3.3 Power on and check BOTH clusters

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
`park_arm` or rollout left that arm in CAN control — power-cycle that follower.

### 2.3.4 RECORD (both masters POWERED ON)

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

- Pass `--robot.left_port` / `--robot.right_port` explicitly — the plugin defaults are the
  literal strings `can_left` / `can_right`, and the kernel may have named them `can0` / `can1`
- On connect the teleoperator asks you to move each master in turn (30 s each); without it a
  silent master records a column of zeros
- Always the same order, every episode. The waiting arm must stay **completely still**
- Cadence must still read **≥ 29.5 Hz** — 4 cameras plus 2 CAN buses is the heaviest config

### 2.3.5 Check the dataset — per arm

```bash
python scripts/check/check_dataset_bimanual.py $REPO 1     # 1 pick-place cycle per arm
```

`check_dataset.py` reads a single gripper column, so on a two-arm dataset it cannot see that
one arm never moved. This one splits by name and reports LEFT and RIGHT separately:

```
--- LEFT ---   -> --robot.left_max_relative_target=0.5
--- RIGHT ---  -> --robot.right_max_relative_target=0.5
               -> --duration=35
Steps for 16.7 epochs (batch 8): 264,947
```

Several objects handled by one arm: `check_dataset_multi.py <repo_id> <n_objects>`.

### 2.3.6 TRAIN

```bash
lerobot-train --policy.type=act \
  --dataset.repo_id=$REPO --output_dir=outputs/act_bimanual \
  --policy.push_to_hub=false --policy.device=cuda \
  --steps=<from the checker> --wandb.enable=false
```

Episodes run about twice as long, so the step count roughly doubles. `--batch_size=16` halves
the wall-clock and still fits in 32 GB.

### 2.3.7 Smoothness — MANDATORY, per arm

```bash
python scripts/check/check_smoothness_bimanual.py \
  outputs/act_bimanual/checkpoints/last/pretrained_model $REPO 50
```

`check_smoothness.py` slices `[:, :6]` = the **left arm only**, so a jittery right arm would
go unreported. This version ends with `WORST ARM`, which must be ≤ 5× before the robot is
touched.

### 2.3.8 ROLLOUT (both masters POWERED OFF)

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

- Parking **is** correct here: a rollout wants the arms in CAN control, which is what
  `park_arm` leaves behind
- Size `--duration` from the checker's **max**, not p95 — p95 chops the slowest runs off
  before the second arm releases
- Run the first few at `move_speed_pct=30` with a hand on the power switch; `Ctrl+C` if the
  arms drift toward each other

### 2.3.9 After every run

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
