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
│   │                   check_dataset_colors.py
│   │                   check_smoothness.py · check_smoothness_bimanual.py
│   │                   compare_arms.py · compare_firmware.py
│   ├── record/         record_colors.py      (PART 3: one prompt per episode)
│   ├── voice/          voice_test.py · commands.py · audio.py · asr.py · voiceprint.py
│   └── deploy/         park_arm.py · voice_rollout.py
├── patches/            one patch applied to the LeRobot checkout
├── env.sh.example      camera paths template -> bootstrap.sh copies it to env.sh
├── env_all.sh.example  optional 4th "overview" camera -> copy to env_all.sh
├── deploy_spec.json    joint names, limits, named poses
└── requirements-pinned.txt
```

| doc | when |
|---|---|
| **README.md** | SETUP once, then PART 1 (one arm pair), PART 2 (two arm pairs) or PART 3 (voice, colour cubes) |
| **TROUBLESHOOTING.md** | something broke |

Reference rig: 2 clusters × 2 AgileX PiPER (firmware master–follower, one CAN bus each) ·
3 Intel RealSense D435 (1 fixed `front` + 1 wrist per cluster) · 2 gs_usb CAN adapters
(`1d50:606f`, 1 Mbps) · RTX 5090 · Ubuntu 24.04.

---

# SETUP — once per machine

Shared by PART 1 and PART 2. Run the steps in order.

> **One box = one paste.** Copy a box, run it, read what it prints, then move to the next.
> Pasting a whole section at once is how most of the failures in TROUBLESHOOTING.md start.

## Step 1 — System packages

```bash
sudo apt update
```

```bash
sudo apt install -y git v4l-utils can-utils linux-tools-common linux-tools-$(uname -r)
```

`v4l-utils` = camera probing · `can-utils` = CAN tooling · `linux-tools-*` = `cpupower`.

## Step 2 — Conda env

```bash
conda create -y -n piper_pai python=3.12
```

LeRobot requires Python >= 3.12.

```bash
conda activate piper_pai
```

The prompt must now read `(piper_pai)`.

```bash
conda install -y -c conda-forge ffmpeg
```

ffmpeg 8.x — torchcodec needs it to decode video.

Not named `lerobot`, or `import lerobot` resolves to another checkout. Activate it before
Step 3 — `install.sh` refuses `base`.

## Step 3 — Clone and install

```bash
git clone https://github.com/LinhTranUlsan/PiPER_robot_arm_pai.git
```

```bash
cd PiPER_robot_arm_pai
```

```bash
bash scripts/setup/install.sh
```

| instead of the line above | for |
|---|---|
| `bash scripts/setup/install.sh --pi0` | also pi0 (VLA) |
| `bash scripts/setup/install.sh --pinned` | exact versions from requirements-pinned.txt |
| `bash scripts/setup/install.sh --bimanual` | also the two-cluster plugins (PART 2) |
| `bash scripts/setup/install.sh --smolvla` | also SmolVLA + Whisper (PART 3) |

Must end with `robot : ['piper_bus']` · `teleop : ['piper_master']` · `cuda True`.

**Do not move or delete this directory.** The plugins are editable installs pointing into
`plugins/` right here; re-cloning means re-running `install.sh`.

## Step 4 — CPU governor (REQUIRED)

```bash
sudo bash scripts/setup/set_cpu_performance.sh
```

A 30 Hz loop sleeps 25 ms per tick, so `powersave` never ramps the clock and **the robot
shakes** at `load average` 0.4.

## Step 5 — Bootstrap this machine

```bash
bash scripts/setup/bootstrap.sh --from /home/pai/linh/PiPER/lerobot/piper
```

No checkout to copy from? Use `bash scripts/setup/bootstrap.sh` instead.

Builds `env.sh` / `env_all.sh` and records the CAN serials. Both are gitignored (by-path
embeds this machine's PCI id), so a fresh clone always needs this.

**Wait for `== READY ==`** — every line `ok`, both buses resolved. `--from` brings the camera
roles with it and lands there directly; anything less names what is missing and exits
non-zero.

## Step 6 — CAN bus

```bash
sudo bash scripts/can/install_udev.sh         # pin the adapters to can_left / can_right
```

`install_udev.sh` once per machine. The buses are brought up by `fix_can.sh` in 1.3.

```bash
source env_all.sh && source scripts/can/can_env.sh
```

Must print `can_left` / `can_right`.

> **USB wiring rule:** no hub may carry BOTH a CAN adapter and a camera — `lsusb -t | grep -B3 gs_usb`.
> Camera hubs must be USB 3.0 (`bash scripts/check/usb_speed.sh`, 5000 Mbps).

---

# PART 1 — ONE ARM PAIR

One master–follower cluster on one CAN bus: record → train → rollout.

## 1.1 Open a session

Run this at the start of **every** terminal. The variables live only in that shell.

**1** — the env and the directory:

```bash
conda activate piper_pai
cd ~/PiPER_robot_arm_pai
```

**2** — `env_all.sh` pulls in `env.sh`, then adds `$ALL` + `$CAMS_*_ALL`; `can_env.sh` finds
the adapters by USB serial and sets `$CAN_LEFT` / `$CAN_RIGHT`:

```bash
source env_all.sh
source scripts/can/can_env.sh
```

No 4th camera? `source env.sh` instead of `env_all.sh`.

**3** — the session variables. `CAN_PORT` is what `park_arm.py` reads; `SPEC` is the file it
always opens; `CAMS` is front + wrist_right + all:

```bash
export CAN_PORT=$CAN_RIGHT
CAN=$CAN_PORT
CAMS="$CAMS_RIGHT_ALL"
SPEC=deploy_spec.json
TASK="pick the cube and place it"
```

No 4th camera? `CAMS="$CAMS_RIGHT"`. LEFT cluster? `export CAN_PORT=$CAN_LEFT` and
`CAMS="$CAMS_LEFT_ALL"` — those two lines only.

**4** — prove both files were sourced. Empty means they were not:

```bash
echo "CAN=$CAN"
echo "$CAMS"
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
python scripts/setup/detect_cameras.py
```

4 cameras with the overview one, else 3.

```bash
python scripts/check/view_cameras.py 30
```

Live view. The tail line must read ~30 fps.

```bash
python scripts/check/motor_faults.py --can $CAN
```

`driver_error_status: False` ×6 — no joint has latched a fault.

```bash
python scripts/check/preflight.py --teleop --can $CAN
```

`ALL PASS`. `0x2A1 = 200/s` is exactly one arm reporting, so the master–slave pairing is
intact; `TX path healthy` is the **write** path, which RX says nothing about.

The `front` view must match how it looked while recording — same angle, distance, lighting.
`0x15x: 0/s` means "master off **or** powered and at rest"; check the switch by hand.
`view_cameras.py` must end at **~30 fps**; below 25 it prints the USB link check.

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

By eye:

```bash
lerobot-dataset-viz --repo-id=$USER/piper_right --episode-index=0
```

By numbers:

```bash
python scripts/check/check_dataset.py $USER/piper_right
```

It prints the two numbers rollout needs: `|action-state|` → `--robot.max_relative_target`,
episode length → `--duration`. Dataset lives in `~/.cache/huggingface/lerobot/$USER/piper_right/`.

Drop bad episodes (option):

```bash
lerobot-edit-dataset \
  --repo_id=$USER/piper_right --new_repo_id=$USER/piper_right_clean \
  --operation.type=delete_episodes --operation.episode_indices='[type your numbers]'
```

## 1.7 TRAIN

Steps = `16.7 × frames ÷ batch_size`. Run them one after another, never in parallel.
`check_dataset.py` prints the number for your own dataset; the table is for planning.

| episodes | frames | steps @ batch 8 | steps @ batch 16 | time |
|---|---|---|---|---|
| 50 | 20,500 | 42,794 | 21,397 | ~0.9 h |
| 80 | 32,800 | 68,470 | 34,235 | ~1.4 h |
| **100** | **41,000** | **85,588** | **42,794** | **~1.7 h** |
| 150 | 61,500 | 128,381 | 64,191 | ~2.6 h |

410 frames per episode (13.7 s), 3 cameras. **The time is the same at either batch size** —
batch 16 halves the step count but each step costs twice as much. Batch 8 is the safer choice
on a small dataset, because it takes twice as many optimiser steps to cover the same epochs.

ACT:

```bash
lerobot-train --policy.type=act \
  --dataset.repo_id=$USER/piper_right --output_dir=outputs/act_right \
  --policy.push_to_hub=false --policy.device=cuda \
  --batch_size=8 --steps=85588 --wandb.enable=false
```

Diffusion Policy, about 3x slower than ACT (option):

```bash
lerobot-train --policy.type=diffusion \
  --dataset.repo_id=$USER/piper_right --output_dir=outputs/dp_right \
  --policy.push_to_hub=false --policy.device=cuda \
  --batch_size=8 --steps=85588 --wandb.enable=false
```

Do not set `n_action_steps` or the scheduler at train time — those are run-time parameters.

> Times measured on the reference rig (RTX 5090): 20,000 steps in 24 min 25 s at batch 8 with
> 3 cameras, and in 62 min 54 s at batch 16 with 4. Both work out at ~3.0 ms per sample per
> camera, which is what the tables extrapolate. Scale by your own GPU.

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
python scripts/can/bus_scan.py 5 --can $CAN
```

`0x2A1 = 200/s`, control = 0.

```bash
python scripts/check/motor_faults.py --can $CAN
```

6/6 joints clean.

```bash
python scripts/check/preflight.py --teleop --can $CAN
```

`ALL PASS`.

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
  --fps=30 --duration=0 \
  --interactive=true \
  --return_to_initial_position=false \
  --display_data=true
```

Type `/start` to run and `/stop` to exit.

(Only apply syntax below if you use DP for your training) Diffusion Policy — add three lines to lerobot-rollout, or 100 DDPM steps stall 56% of the control loop:

```bash
  --policy.noise_scheduler_type=DDIM \
  --policy.num_inference_steps=10 \
  --policy.n_action_steps=32 \
```

## 1.10 After every run

```bash
ip -details -statistics link show $CAN | grep -A1 re-started
```

bus-off must still be 0.

```bash
python scripts/check/motor_faults.py --can $CAN
```

No joint faulted.

`bus.send()` only queues a frame and returns — **a clean log does not prove the commands
arrived.**

## 1.11 Rollout checklist

```
[ ] Master arm POWERED OFF -- by its switch, not by reading a script
[ ] Waited 10 s after powering the follower on
[ ] source env_all.sh  AND  source scripts/can/can_env.sh
[ ] sudo bash scripts/can/fix_can.sh --can $CAN
[ ] motor_faults  -> 6/6 clean
[ ] preflight     -> ALL PASS, including "TX path healthy"
[ ] Workspace clear
[ ] After the run: bus-off still 0, no joint faulted
```

Anything fails → **TROUBLESHOOTING.md**.

## 1.12 Flag reference

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

# PART 2 — BIMANUAL (two arm pairs, one dataset)

Both clusters as a single robot, driven from **one terminal**. Use this when the task needs
both arms in one dataset: they hand over an object, or act in a sequence the policy must
learn. `piper_bimanual` wraps two `piper_bus` instances and prefixes every key `left_` /
`right_`, so the action vector is 14 wide. It adds no writes of its own; each cluster keeps
its own firmware master-slave link.

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
```

```bash
sudo bash scripts/can/fix_can.sh
```

Must be **0 fps** — nothing is powered on that bus:

```bash
python scripts/can/bus_scan.py 5 --can $CAN_LEFT
```

Must be **~2420 fps**:

```bash
python scripts/can/bus_scan.py 5 --can $CAN_RIGHT
```

Any traffic on the unpowered cluster's bus means they are still joined.

## 2.2 Install the two extra plugins

```bash
bash scripts/setup/install.sh --bimanual
```

Or, if LeRobot is already installed, these two instead:

```bash
pip install -e plugins/lerobot_robot_piper_bimanual --no-deps
```

```bash
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

## 2.3 Open a session

```bash
conda activate piper_pai && cd ~/PiPER_robot_arm_pai
```

```bash
source env_all.sh
source scripts/can/can_env.sh
```

`CAMS_BOTH_ALL` is front + both wrists + all, 4 streams. No 4th camera? `CAMS="$CAMS_BOTH"`.

```bash
CAMS="$CAMS_BOTH_ALL"
SPEC=deploy_spec.json
REPO=$USER/piper_bimanual
TASK="right arm picks the red block into the box, then left arm picks the yellow block into the box"
```

```bash
echo "$CAN_RIGHT $CAN_LEFT"; echo "$CAMS" | grep -o "8\.[0-9.]*" | tr '\n' ' '
```

Two bus names and four USB ports. A blank means `env_all.sh` or `can_env.sh` was not sourced.

## 2.4 Power on and check BOTH clusters

```
Right cluster: follower ON -> wait 5 s -> master ON
Left  cluster: follower ON -> wait 5 s -> master ON
Wait another 10 s
```

```bash
sudo bash scripts/can/fix_can.sh
```

Then, per bus: `0x2A1 = 200/s` on each · 6/6 clean with `ctrl_mode STANDBY` · `ALL PASS`.

```bash
for C in $CAN_RIGHT $CAN_LEFT; do
  echo "===== $C ====="
  python scripts/can/bus_scan.py 5 --can $C
  python scripts/check/motor_faults.py --can $C
  python scripts/check/preflight.py --teleop --can $C
done
```

`ctrl_mode : STANDBY(0x0)` is required before recording. `CAN_CTRL(0x1)` means a previous
`park_arm` or rollout left that arm in CAN control — power-cycle that follower.

## 2.5 Sync the followers — do NOT park before recording

`park_arm` leaves an arm in CAN control, which is what 2.4 flags as `CAN_CTRL(0x1)`. Nudge
each master by hand for a few seconds instead, then check the follower tracked it:

```bash
python scripts/check/joint_limits.py --can $CAN_RIGHT
```

```bash
python scripts/check/joint_limits.py --can $CAN_LEFT
```

The `gap` column (master minus follower) must read ~0 on every joint. `master not
transmitting` means that arm was not moved — nudge it while the script runs.

Then bring both followers to the start pose **by moving the masters**, never with `park_arm`.

## 2.6 RECORD (both masters POWERED ON)

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

## 2.7 Check the dataset — per arm

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

## 2.8 TRAIN

| episodes | frames | steps @ batch 8 | steps @ batch 16 | time |
|---|---|---|---|---|
| 100 | 66,800 | 139,445 | 69,722 | ~3.7 h |
| **150** | **100,200** | **209,168** | **104,584** | **~5.6 h** |
| 180 | 120,240 | 251,001 | 125,500 | ~6.7 h |
| 200 | 133,600 | 278,890 | 139,445 | ~7.4 h |

668 frames per episode (22.3 s), 4 cameras. Episodes run about twice as long as single-arm
and carry a fourth camera, so the same episode count costs roughly 3× the time.

```bash
lerobot-train --policy.type=act \
  --dataset.repo_id=$REPO --output_dir=outputs/act_bimanual \
  --policy.push_to_hub=false --policy.device=cuda \
  --batch_size=16 --steps=104584 --wandb.enable=false
```

Batch 16 uses about 12 GB, well inside 32 GB. It does not shorten the run — it halves the
step count while doubling the cost of each step — but it is the usual choice here because
100k+ episodes give the larger batch enough data to be stable.

## 2.9 Smoothness — MANDATORY, per arm

```bash
python scripts/check/check_smoothness_bimanual.py \
  outputs/act_bimanual/checkpoints/last/pretrained_model $REPO 50
```

`check_smoothness.py` slices `[:, :6]` = the **left arm only**, so a jittery right arm would
go unreported. This version ends with `WORST ARM`, which must be ≤ 5× before the robot is
touched.

## 2.10 ROLLOUT (both masters POWERED OFF)

Per bus: control = 0, then `VERDICT: CLEAN`.

```bash
for C in $CAN_RIGHT $CAN_LEFT; do
  python scripts/can/bus_scan.py 5 --can $C
  python scripts/can/send_probe.py --can $C --mode both --cameras
done
```

Park each arm, **Ctrl+C after each**:

```bash
python scripts/deploy/park_arm.py --can $CAN_RIGHT --spec $SPEC --pose 0 0.3 -0.3 1.4 19.9 -2.0
```

```bash
python scripts/deploy/park_arm.py --can $CAN_LEFT  --spec $SPEC --pose 0 0.3 -0.3 1.4 19.9 -2.0
```

Place the objects and the box, clear the space between the arms.

```bash
lerobot-rollout \
  --strategy.type=base \
  --policy.path=outputs/act_bimanual/checkpoints/last/pretrained_model \
  --policy.n_action_steps=50 \
  --robot.type=piper_bimanual --robot.passive=false --robot.id=followers \
  --robot.left_port=$CAN_LEFT --robot.right_port=$CAN_RIGHT \
  --robot.move_speed_pct=30 \
  --robot.left_max_relative_target=0.5 \
  --robot.right_max_relative_target=0.5 \
  --robot.cameras="$CAMS" --task="$TASK" \
  --fps=30 --duration=0 \
  --interactive=true \
  --return_to_initial_position=false --display_data=true
```

- Parking **is** correct here: a rollout wants the arms in CAN control, which is what
  `park_arm` leaves behind
- Size `--duration` from the checker's **max**, not p95 — p95 chops the slowest runs off
  before the second arm releases
- Run the first few at `move_speed_pct=30` with a hand on the power switch; `Ctrl+C` if the
  arms drift toward each other

## 2.11 After every run

bus-off must stay 0, no joint faulted:

```bash
for C in $CAN_RIGHT $CAN_LEFT; do
  ip -details -statistics link show $C | grep -A1 re-started
  python scripts/check/motor_faults.py --can $C
done
```

---

# PART 3 — VOICE: COLOUR CUBES, ONE POLICY (SmolVLA)

Two cubes — **yellow** and **green** — lie on the table, and a jar stands in a fixed place.
You say a colour; the arm puts that cube in the jar and comes back, ready for the next one.

One SmolVLA policy does both. It reads the camera images, the arm state **and a text
prompt**, and the prompt is what picks the cube: `Put the yellow cube in the jar.` or
`Put the green cube in the jar.` The voice side only turns what you said into one of those
prompts. ACT and Diffusion Policy take no text, so they cannot do this task.

```
record_colors.py   one prompt PER EPISODE, both colours on every table layout
      |
lerobot-train      smolvla_base, fine-tuned on both colours together
      |
voice_rollout.py   headset mic -> Whisper + your voice profile -> one colour -> the policy
```

One arm pair on `can_right`, as in PART 1. Do SETUP and PART 1 up to 1.4 first. The
colours are `COLORS` in `scripts/voice/commands.py`; recording, checking and the rollout
all follow it — change it only **before** recording a dataset.

| once per machine | every session |
|---|---|
| 3.1 install · 3.2 pair the headset · 3.5 enrol your voice | 3.3 open a session · 3.4 headset mode |

## 3.1 Install SmolVLA and Whisper (once)

```bash
conda activate piper_pai && cd ~/PiPER_robot_arm_pai
```

```bash
pip install -e "../lerobot[smolvla]"
```

That is `$LEROBOT_DIR` from `install.sh`, next to this repo. It adds `transformers` (Whisper
runs on it too) and `num2words`, and leaves torch alone. A fresh machine can use
`bash scripts/setup/install.sh --smolvla` instead.

```bash
python -c "import transformers, num2words; print('transformers', transformers.__version__)"
```

Must print `transformers 5.5.x`.

## 3.2 Pair the headset (once)

```bash
bluetoothctl show | head -3
```

Must print `Controller ...`. `No default controller available` means the adapter never came
up — TROUBLESHOOTING.md, *Voice*, first entry.

Put the headset in pairing mode, then:

```bash
bluetoothctl
```

Inside `bluetoothctl`, one line at a time; `XX:XX:...` is the address `scan on` prints next
to the headset's name:

```
power on
scan on
pair XX:XX:XX:XX:XX:XX
trust XX:XX:XX:XX:XX:XX
connect XX:XX:XX:XX:XX:XX
exit
```

`trust` makes it reconnect on its own whenever it is switched on.

## 3.3 Open a session

Run this at the start of **every** terminal, as in 1.1:

```bash
conda activate piper_pai
cd ~/PiPER_robot_arm_pai
source env_all.sh
source scripts/can/can_env.sh
```

```bash
export CAN_PORT=$CAN_RIGHT
CAN=$CAN_PORT
CAMS="$CAMS_RIGHT_ALL"
SPEC=deploy_spec.json
REPO=$USER/piper_colors
SMOLVLA_RENAME='{"observation.images.front": "observation.images.camera1", "observation.images.wrist": "observation.images.camera2", "observation.images.all": "observation.images.camera3"}'
```

```bash
echo "CAN=$CAN"; echo "$CAMS"; echo "$SMOLVLA_RENAME"
```

`smolvla_base` was pre-trained on three cameras called `camera1/2/3`; the map sends
`front`, `wrist` and `all` to them. **The same map at train and at rollout time**, or each
image lands in the wrong slot. No 4th camera? `CAMS="$CAMS_RIGHT"` and drop the `all` entry
from the map.

## 3.4 Headset mode — every session, and after every reconnect

A Bluetooth headset comes up in its music mode (A2DP), which has **no microphone**, and
tends to take over as the default speaker. Three lines fix both. Replace `T14` with any part
of your headset's name.

**1** — connected:

```bash
bluetoothctl devices Connected
```

Your headset must be listed. Not listed: switch it on, or `bluetoothctl connect XX:XX:...`.

**2** — microphone mode at 16 kHz, as the default microphone:

```bash
python scripts/voice/voice_test.py --headset T14
```

Must print `headset-head-unit-msbc   codec msbc, 16000 Hz`. It picks the profile by name.
**16000 Hz is the requirement**: the 8 kHz phone codec (CVSD) still records, but Whisper
hears *"yo"* for *yellow* — measured on the reference headset, 5/5 recognised on mSBC, 0/4
on CVSD. A `WARNING: 8000 Hz` means this headset has no 16 kHz mode here: use a wired or USB
microphone instead (`--voice.mic=<its name>` below).

**3** — the robot speaks on the monitor, not in the headset:

```bash
python scripts/voice/voice_test.py --set-output HDMI
```

It says *"This is the robot speaker"* on the monitor.

**4** — check:

```bash
python scripts/voice/voice_test.py --list
```

`*` next to HDMI under SPEAKERS, `*` next to the headset under MICROPHONES, and
`Bluetooth mic ...: codec msbc, 16000 Hz` at the end.

## 3.5 Voice checks

**1** — the chain without a microphone (once per machine). Silent: it synthesises speech in
memory and runs it through speech detection, Whisper and the colour parser:

```bash
python scripts/voice/voice_test.py --selftest
```

Must end with `wrong colour / false start: 0` and `speech detection misses: 0`. The
`understood` count is informational — the synthetic voice says the words worse than you will.

**2** — teach it YOUR voice (once per person and headset). Whisper expects textbook
English; with an accent it writes *"hello"* for *yellow* and finds no colour. A voice
profile matches what you say against your own earlier takes instead, so pronunciation does
not matter — only that you say it the same way each time. English or Vietnamese (*vàng*,
*xanh lá*), your choice:

```bash
python scripts/voice/voice_test.py --enroll
```

It asks for each colour 5 times, then 6 things that are **not** a colour — *hello*, *okay*,
your name, a cough. Those are what stop a similar-sounding word from counting as a command,
so say real, different things. It ends with `every take is recognised as itself` and writes
`outputs/voice_profile.npz`, which everything below uses automatically. Enrol in the
headset mode you will use (3.4) — a profile recorded at 16 kHz does not match 8 kHz audio.

**3** — the real microphone, ten rounds (every session). It asks *"Say a color"* and says
back what it understood; nothing moves:

```bash
python scripts/voice/voice_test.py --rounds 10 --csv outputs/voice_mic_test.csv
```

**10/10 before touching the robot.** Each round prints who decided:

| `[...]` | meaning |
|---|---|
| `whisper + profile` | both agree — the strongest case |
| `whisper` | Whisper read the colour; the profile was unsure |
| `profile` | Whisper found no colour word; your profile recognised it |
| `refused` | two colours, a correction (*"yellow, no, green"*), *red* (not on the table), no colour, or Whisper and profile disagree — **never guessed**, it asks again |

With only green on the table, bare *xanh* means green. Vietnamese Whisper: add
`--language vi`. Many refusals: headset mode first (3.4, 16000 Hz), then `--enroll` again.
`peak ... vs threshold ...` barely above the threshold = the mic is too far or too quiet.

## 3.6 Set the table

- The jar in ONE place for the whole dataset — tape its outline on the table
- Both cubes on the table in every episode, inside the reach you want to test
- Cameras, lighting and the `front` view fixed, as in PART 1

## 3.7 RECORD (master POWERED ON)

Power-on order as in 1.2 — follower first, master second, wait 10 s. Then:

```bash
sudo bash scripts/can/fix_can.sh --can $CAN
```

```bash
python scripts/check/view_cameras.py 30
```

```bash
python scripts/check/motor_faults.py --can $CAN
```

```bash
python scripts/check/preflight.py --teleop --can $CAN
```

`~30 fps`, 6/6 clean, `ALL PASS` — as in 1.4. Then record:

```bash
python scripts/record/record_colors.py \
  --robot.type=piper_bus --robot.port=$CAN --robot.passive=true --robot.id=follower_right \
  --robot.cameras="$CAMS" \
  --teleop.type=piper_master --teleop.port=$CAN --teleop.id=master_right \
  --dataset.repo_id=$REPO \
  --dataset.no_stamp=true \
  --dataset.num_episodes=100 --dataset.fps=30 \
  --dataset.episode_time_s=300 --dataset.reset_time_s=300 \
  --dataset.push_to_hub=false \
  --dataset.streaming_encoding=true --dataset.encoder_threads=2 \
  --dataset.num_image_writer_processes=1 \
  --display_data=true
```

The same flags as 1.5, minus `--dataset.single_task` — the script sets the prompt of every
episode itself. 100 episodes = **50 table layouts × 2 colours**, 50 per colour.

How a layout goes — the script says each step out loud and prints it:

```
place both cubes                             "New layout. Shuffle the cubes. Next, green cube"
episode: pick GREEN into the jar, go home    "green cube"           Space = episode done
reset: put the green cube back where it was  "Put it back. Next, yellow cube"   Space = start
episode: pick YELLOW into the jar, go home   "yellow cube"          Space = episode done
reset: move BOTH cubes somewhere new         "New layout. Shuffle the cubes. Next, ..."
```

- **Put the picked cube back exactly where it was** — the two episodes of a layout must start
  from the same picture; only the prompt differs. That is what forces the policy to read the
  prompt instead of learning "the cube over there"
- Move both cubes to genuinely new places for every layout: 50 layouts = 50 positions to
  learn from, and the positions the rollout will see must be among them
- The colour order is shuffled per layout, so the first cube is not always the same colour
- Keys: `Space` end the phase · `r` re-record (same colour) · `q` stop and save ·
  `1` = next episode yellow, `2` = green, instead of the plan — only to fix a mistake
- Continue another day: **the same command + `--resume=true`**. The plan carries on from the
  next episode; if it stopped mid-layout, set the table as that layout was
- Every episode complete, as in 1.5: open → approach → grasp → lift → jar → **RELEASE** → home
- End-of-session cadence **≥ 29.5 Hz**, as in 1.5

When it stops it prints the per-colour count (next section).

## 3.8 Check the dataset

Per colour and per layout:

```bash
python scripts/check/check_dataset_colors.py $REPO
```

Both colours within 20 % of each other, and `every complete layout has one episode of each
colour`. Then grasp / release / lengths, as in 1.6:

```bash
python scripts/check/check_dataset.py $REPO
```

Note its **max** episode length: it sizes `--duration` in 3.11. By eye, one episode of each
colour:

```bash
lerobot-dataset-viz --repo-id=$REPO --episode-index=0
```

**Train early once.** After ~20 layouts (40 episodes), train 5,000 steps (3.9 with
`--steps=5000`) and try 3.11. If the arm heads for the same cube whatever you say, the prompt
is not being read — fix the recording (same picture within a layout) before recording more.

## 3.9 TRAIN — SmolVLA

```bash
lerobot-train --policy.path=lerobot/smolvla_base \
  --policy.push_to_hub=false --policy.device=cuda \
  --dataset.repo_id=$REPO --rename_map="$SMOLVLA_RENAME" \
  --output_dir=outputs/smolvla_colors \
  --batch_size=64 --steps=30000 --save_freq=5000 \
  --num_workers=4 --wandb.enable=false
```

- **30,000 steps, not the 16.7-epoch rule of PART 1.** SmolVLA's learning rate decays over
  30,000 steps (`cosine_decay_with_warmup`, `num_decay_steps: 30000`); stopped before that it
  is still learning fast and shakes. Measured on this rig: 9,371 steps (16.7 epochs of 100
  episodes) scored **10.7× — will shake**
- `--save_freq=5000` keeps a checkpoint every 5,000 steps, so 3.10 can pick the smoothest
- `--policy.path=` fine-tunes the pre-trained model; `--policy.type=smolvla` would start from
  random weights
- The first run downloads `smolvla_base` and SmolVLM2 (~2 GB) into `~/.cache/huggingface`
- The log line `step:... step_s:` is seconds per step: × 30,000 = the run time
- `CUDA out of memory`: `--batch_size=32`, same `--steps`

**Stopped early, or trained too few steps?** Continue the same run — weights, optimizer,
step count and learning-rate schedule all come back:

```bash
lerobot-train \
  --config_path=outputs/smolvla_colors/checkpoints/last/pretrained_model/train_config.json \
  --resume=true --steps=30000 --save_freq=5000
```

Continuing to 30,000 ends where training from scratch to 30,000 would. Train from scratch
(the first command, with a **new** `--output_dir`, e.g. `outputs/smolvla_colors_v2`) only
when the dataset changed — episodes added, deleted or trimmed. An existing `--output_dir`
without `--resume=true` is refused.

## 3.10 Smoothness — MANDATORY before touching the robot

The same check as 1.8; it reads the camera map out of the checkpoint itself. One episode of
each colour — the last argument is an **episode index**:

```bash
python scripts/check/check_smoothness.py \
  outputs/smolvla_colors/checkpoints/last/pretrained_model $REPO 50
```

```bash
python scripts/check/check_smoothness.py \
  outputs/smolvla_colors/checkpoints/last/pretrained_model $REPO 51
```

**≤ 5×** safe · **≥ 10×** will shake. Borderline at `last`? Try an earlier one — replace
`last` with `025000` or `020000` — and roll out the smoothest.

## 3.11 VOICE ROLLOUT (master POWERED OFF)

Master off **by its switch**, wait 10 s, then the three checks of 1.9:

```bash
python scripts/can/bus_scan.py 5 --can $CAN
```

`0x2A1 = 200/s`, control = 0.

```bash
python scripts/check/motor_faults.py --can $CAN
```

6/6 clean.

```bash
python scripts/check/preflight.py --teleop --can $CAN
```

`ALL PASS`. Headset in microphone mode (3.4) — check it once more:

```bash
python scripts/voice/voice_test.py --list | tail -2
```

`codec msbc, 16000 Hz`. **Between turns the arm returns to the pose it is in when the
script starts**: start it where the demonstrations started — the home pose the master held
at the start of every episode. Put both cubes out, then:

```bash
python scripts/deploy/voice_rollout.py \
  --strategy.type=base \
  --policy.path=outputs/smolvla_colors/checkpoints/last/pretrained_model \
  --rename_map="$SMOLVLA_RENAME" \
  --robot.type=piper_bus --robot.port=$CAN --robot.passive=false --robot.id=follower_right \
  --robot.move_speed_pct=50 \
  --robot.max_relative_target=0.6 \
  --robot.cameras="$CAMS" \
  --fps=30 --duration=25 \
  --return_to_initial_position=true \
  --display_data=true \
  --voice.mic=bluez
```

How a turn goes:

```
"Ready"                          models loaded, arm connected
"Say a color"                    it listens -- ONLY now
you: "yellow"                    or "put the yellow cube in the jar"
"yellow cube"                    it understood; the arm goes
... --duration seconds ...       the policy runs, then the arm returns to its start pose
"Say a color"                    next turn
```

- Say the colour **after** *"Say a color"*. Nothing said while the arm moves is taken
- *"Sorry, say one color"* = refused (unclear, two colours, a correction): say it again
- Say *"stop"* while it listens to end; `Ctrl+C` stops it at any moment
- `--duration` = the dataset's **max** episode length from 3.8, plus a few seconds
- No `--interactive`, no `--task`: the voice loop is the interactive part and sets the task
- Every turn is appended to `outputs/voice_runs.csv`: what was heard, the colour, the times
- Judders at every chunk boundary (every 1.67 s)? Add
  `--inference.type=rtc --inference.rtc.execution_horizon=10`. It smooths the joins; it does
  not fix a model that 3.10 says will shake

## 3.12 The headset dropped

**The audio link failed, the headset is still connected.** The script notices — 3 s of pure
digital silence — says *"Microphone lost. Reconnecting."* and reopens the microphone by
itself. Wait for it, then say the colour again.

**The headset disconnected.** `Ctrl+C`, then:

```bash
bluetoothctl connect XX:XX:XX:XX:XX:XX
```

```bash
python scripts/voice/voice_test.py --headset T14
```

```bash
python scripts/voice/voice_test.py --set-output HDMI
```

```bash
python scripts/voice/voice_test.py --rounds 3
```

3/3, then the rollout command of 3.11 again. Drops keep coming back: charge the headset, sit
nearer the Bluetooth adapter, and keep a USB Bluetooth dongle off the hub of the CAN adapters.
A wired or USB microphone never drops: `--voice.mic=<its name from --list>`.

## 3.13 After every run

```bash
ip -details -statistics link show $CAN | grep -A1 re-started
```

bus-off must still be 0.

```bash
python scripts/check/motor_faults.py --can $CAN
```

No joint faulted. As in 1.10: a clean log does not prove the commands arrived.

## 3.14 Evaluation — 10 turns

Ten turns, **5 yellow – 5 green**, order shuffled, and the cube positions changed between
turns (the jar stays). Write the order down before starting, e.g.:

```
green  yellow  yellow  green  yellow  green  green  yellow  green  yellow
```

Add two flags to the command in 3.11:

```bash
  --voice.turns=10 \
  --voice.score=true \
```

After each turn it asks for the result in the terminal — one key:

| key | result |
|---|---|
| `o` | ok — right cube, in the jar |
| `c` | wrong colour picked |
| `g` | right cube, grasp failed |
| `r` | grasped, release in the jar failed |
| `x` | other failure |
| `s` | skip, not counted |

It ends with the score table. Recognition errors need no key: a refused command never
starts a turn and is logged in the CSV as `refused: ...`, and a misheard colour shows up as
`heard` ≠ what you said.

## 3.15 Rollout checklist

```
[ ] Master arm POWERED OFF -- by its switch
[ ] Waited 10 s after powering the follower on
[ ] source env_all.sh  AND  source scripts/can/can_env.sh  AND  the 3.3 variables
[ ] sudo bash scripts/can/fix_can.sh --can $CAN
[ ] motor_faults  -> 6/6 clean
[ ] preflight     -> ALL PASS, including "TX path healthy"
[ ] check_smoothness -> <= 5x, both colours
[ ] voice_test.py --headset T14  -> codec msbc, 16000 Hz
[ ] voice_test.py --set-output HDMI  -> heard on the monitor
[ ] voice_test.py --rounds 3  -> 3/3
[ ] Arm at the start pose, both cubes out, workspace clear
[ ] After the run: bus-off still 0, no joint faulted
```

## 3.16 Flag reference

| flag | value | why |
|---|---|---|
| `--policy.path` (train) | `lerobot/smolvla_base` | fine-tune; `--policy.type=smolvla` starts from scratch |
| `--rename_map` | `$SMOLVLA_RENAME` | **train and rollout**: smolvla_base names its cameras camera1/2/3. Missing at rollout → `Visual feature mismatch` |
| `--steps` | `30000` | SmolVLA's learning-rate schedule length; 9,371 measured 10.7× (shakes) |
| `--save_freq` | `5000` | checkpoints to pick the smoothest from |
| `--batch_size` | `64` | SmolVLA's reference setting; 32 if out of memory |
| `--duration` | dataset max + a few s | the length of ONE turn in voice_rollout |
| `--return_to_initial_position` | `true` | also go home at the end of the session |
| `--voice.mic` | part of the mic's name | default: PipeWire's default microphone |
| `--voice.profile` | `outputs/voice_profile.npz` | your enrolled voice (3.5); `none` = Whisper only |
| `--voice.model` | `openai/whisper-small` | `openai/whisper-large-v3-turbo` is bigger; compare both with 3.5 step 3 |
| `--voice.language` | `en` | `vi` for Vietnamese colour words |
| `--voice.turns` | `0` | stop after N turns; 0 = until "stop" |
| `--voice.score` | `false` | ask for each turn's result (3.14) |
| `--plan_seed` (record) | `0` | the colour order; keep it the same across `--resume` |
| `voice_test.py --headset` | headset name | 16 kHz microphone mode, picked by name |

---

## Licence and credits

This repository is Apache-2.0 (see `LICENSE`), matching LeRobot, which its plugins subclass.

LeRobot is Apache-2.0 (https://github.com/huggingface/lerobot).
`piper_sdk` is MIT, by AgileX Robotics (https://github.com/agilexrobotics/piper_sdk).
Neither is vendored here — `install.sh` clones both at pinned commits.
