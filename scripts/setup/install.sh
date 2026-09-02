#!/usr/bin/env bash
# Install everything this repo needs, into the conda env that is already active.
#
#     conda create -y -n piper python=3.12
#     conda activate piper
#     conda install -y -c conda-forge ffmpeg
#     bash scripts/setup/install.sh              # ACT + Diffusion Policy
#     bash scripts/setup/install.sh --pi0        # also pi0 (VLA)
#     bash scripts/setup/install.sh --pinned     # exact versions from requirements-pinned.txt
#     bash scripts/setup/install.sh --bimanual   # also the two-cluster plugins
#
# Do NOT run with sudo. Touches nothing outside the active conda env, except cloning
# LeRobot and piper_sdk into $LEROBOT_DIR / $PIPER_SDK_DIR (default: alongside this repo).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LEROBOT_COMMIT="223a8ad16c52dad961cc1104477ffc3369c5189a"   # verified revision
PIPER_SDK_COMMIT="c9e8a28174e71eeaac448593cb65f8ab258a92fe"
LEROBOT_DIR="${LEROBOT_DIR:-$(dirname "$REPO")/lerobot}"
PIPER_SDK_DIR="${PIPER_SDK_DIR:-$(dirname "$REPO")/piper_sdk}"

WITH_PI0=0; WITH_PINNED=0; WITH_BIMANUAL=0
for a in "$@"; do case "$a" in
  --pi0) WITH_PI0=1 ;;
  --pinned) WITH_PINNED=1 ;;
  --bimanual) WITH_BIMANUAL=1 ;;
  *) echo "unknown flag: $a" >&2; exit 1 ;;
esac; done

say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

say "0. Environment"
[ -n "${CONDA_PREFIX:-}" ] || { echo "No conda env active. Run: conda activate piper" >&2; exit 1; }
PYV=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
case "$PYV" in 3.1[2-9]) : ;; *) echo "Python >= 3.12 required, found $PYV" >&2; exit 1 ;; esac
echo "   env    : $CONDA_PREFIX"
echo "   python : $PYV"
command -v ffmpeg   >/dev/null || echo "   WARNING: ffmpeg missing   -> conda install -y -c conda-forge ffmpeg"
command -v v4l2-ctl >/dev/null || echo "   WARNING: v4l2-ctl missing -> sudo apt install v4l-utils"

say "1. LeRobot @ $LEROBOT_COMMIT"
if [ -d "$LEROBOT_DIR/.git" ]; then
  echo "   already present: $LEROBOT_DIR"
else
  git clone -q https://github.com/huggingface/lerobot.git "$LEROBOT_DIR"
fi
git -C "$LEROBOT_DIR" fetch -q origin "$LEROBOT_COMMIT" 2>/dev/null || true
git -C "$LEROBOT_DIR" checkout -q "$LEROBOT_COMMIT"
echo "   commit : $(git -C "$LEROBOT_DIR" rev-parse --short HEAD)"

say "2. Patch: bind Space to \"next phase\" during recording"
# One key toggles between recording an episode and the reset phase that follows it.
# Upstream only binds Right / n. Skipped silently if already applied.
if git -C "$LEROBOT_DIR" apply --check "$REPO/patches/0001-space-key-next-phase.patch" 2>/dev/null; then
  git -C "$LEROBOT_DIR" apply "$REPO/patches/0001-space-key-next-phase.patch"
  echo "   applied"
else
  echo "   already applied (or does not apply) -- skipping"
fi

say "3. Python dependencies"
if [ "$WITH_PINNED" = 1 ]; then
  echo "   exact versions from requirements-pinned.txt"
  pip install -q -r "$REPO/requirements-pinned.txt"
  pip install -q -e "$LEROBOT_DIR" --no-deps
else
  EXTRAS="core_scripts,training,diffusion"
  [ "$WITH_PI0" = 1 ] && EXTRAS="$EXTRAS,pi"
  echo "   lerobot[$EXTRAS]"
  pip install -q -e "$LEROBOT_DIR[$EXTRAS]"
  pip install -q python-can
fi

# Refuse to continue if `import lerobot` resolves anywhere else. A second project that
# pip-installs its own LeRobot checkout into this env silently wins, and the plugins then
# fail on imports that exist only at this commit.
RESOLVED=$(python -c 'import lerobot, os; print(os.path.realpath(os.path.dirname(lerobot.__file__)))')
EXPECTED=$(cd "$LEROBOT_DIR/src/lerobot" && pwd -P)
if [ "$RESOLVED" != "$EXPECTED" ]; then
  echo "   ERROR: this env already has a different LeRobot:" >&2
  echo "     import lerobot -> $RESOLVED" >&2
  echo "     expected       -> $EXPECTED" >&2
  echo "   Use a fresh conda env for this repo." >&2
  exit 1
fi
echo "   import lerobot -> $RESOLVED"

say "4. AgileX piper_sdk @ ${PIPER_SDK_COMMIT:0:7}"
if [ -d "$PIPER_SDK_DIR/.git" ]; then
  echo "   already present: $PIPER_SDK_DIR"
else
  git clone -q https://github.com/agilexrobotics/piper_sdk.git "$PIPER_SDK_DIR"
fi
git -C "$PIPER_SDK_DIR" checkout -q "$PIPER_SDK_COMMIT"
pip install -q -e "$PIPER_SDK_DIR" --no-deps

say "5. PiPER plugins"
# Single cluster.
pip install -q -e "$REPO/plugins/lerobot_robot_piper_bus" --no-deps
pip install -q -e "$REPO/plugins/lerobot_teleoperator_piper_master" --no-deps
if [ "$WITH_BIMANUAL" = 1 ]; then
  # These import the two above, so they must be installed after them.
  pip install -q -e "$REPO/plugins/lerobot_robot_piper_bimanual" --no-deps
  pip install -q -e "$REPO/plugins/lerobot_teleoperator_piper_master_bimanual" --no-deps
fi

say "6. Verify"
WANT_BIMANUAL=$WITH_BIMANUAL python - <<'PY'
import os, sys
from lerobot.utils.import_utils import register_third_party_plugins
register_third_party_plugins()
from lerobot.robots.config import RobotConfig
from lerobot.teleoperators.config import TeleoperatorConfig
import torch

r = sorted(c for c in RobotConfig.get_known_choices() if "piper" in c)
t = sorted(c for c in TeleoperatorConfig.get_known_choices() if "piper" in c)
print(f"   robot  : {r}")
print(f"   teleop : {t}")
need_r, need_t = {"piper_bus"}, {"piper_master"}
if os.environ.get("WANT_BIMANUAL") == "1":
    need_r |= {"piper_bimanual"}; need_t |= {"piper_master_bimanual"}
missing = (need_r - set(r)) | (need_t - set(t))
if missing:
    sys.exit(f"   ERROR: plugins not discovered: {sorted(missing)}")
arch = torch.cuda.get_arch_list()[-1] if torch.cuda.is_available() else "-"
print(f"   torch  : {torch.__version__} | cuda {torch.cuda.is_available()} | {arch}")
PY

cat <<EOF

$(printf '\033[1m')Installation complete.$(printf '\033[0m')  LeRobot: $LEROBOT_DIR

Next:
  1. sudo bash scripts/setup/set_cpu_performance.sh    # REQUIRED, otherwise the robot shakes
  2. cp env.sh.example env.sh
     python scripts/setup/detect_cameras.py            # probe cameras, note the indices
     python scripts/setup/identify_cameras.py          # which camera belongs to which arm
     python scripts/setup/detect_cameras.py --front F --wrist-right R --wrist-left L --write
  3. Edit the two serials in scripts/can/can_env.sh    # see README section 1.5
     sudo bash scripts/can/fix_can.sh
  4. source env.sh && source scripts/can/can_env.sh
     python scripts/check/preflight.py --teleop --can \$CAN_RIGHT   # must be ALL PASS

Read next: README.md   Something broke: TROUBLESHOOTING.md
EOF
