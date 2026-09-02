"""Measure how smooth the policy's trajectory is compared to the ground truth.

    python check_smoothness.py <checkpoint_path> <repo_id> [episode]

Empirical thresholds on this rig:
    ratio <= 5x   -> smooth, safe to run on the robot
    ratio >= 10x  -> will shake. Train longer.

READ-ONLY. Writes nothing, never touches the robot.
"""
import sys
import numpy as np
import torch
from lerobot.configs.policies import PreTrainedConfig
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.policies import make_pre_post_processors
from lerobot.policies.factory import make_policy

CK = sys.argv[1]
DSID = sys.argv[2]
EP = int(sys.argv[3]) if len(sys.argv) > 3 else 50
RENAME = None
if "pi0" in CK:
    RENAME = {"observation.images.front": "observation.images.base_0_rgb",
              "observation.images.wrist": "observation.images.left_wrist_0_rgb"}

ds = LeRobotDataset(DSID)
cfg = PreTrainedConfig.from_pretrained(CK)
cfg.pretrained_path = CK
cfg.device = "cuda"
pol = make_policy(cfg, ds_meta=ds.meta, rename_map=RENAME).eval()
pre, post = make_pre_post_processors(
    policy_cfg=cfg, pretrained_path=CK,
    preprocessor_overrides={"device_processor": {"device": "cuda"},
                            "rename_observations_processor": {"rename_map": RENAME or {}}})

lo = int(ds.meta.episodes["dataset_from_index"][EP])
hi = int(ds.meta.episodes["dataset_to_index"][EP])
pol.reset()
P, T = [], []
for i in range(lo, hi):
    it = ds[i]
    b = {"observation.state": it["observation.state"][None], "task": it.get("task", "")}
    for k in it:
        if k.startswith("observation.images."):
            b[k] = it[k][None]
    with torch.no_grad():
        P.append(post(pol.select_action(pre(b))).cpu().numpy().reshape(-1))
    T.append(it["action"].numpy())
P = np.array(P)[:, :6]
T = np.array(T)[:, :6]
jp = np.sqrt((np.diff(P, n=2, axis=0) ** 2).mean())
jt = np.sqrt((np.diff(T, n=2, axis=0) ** 2).mean())
r = jp / jt
print(f"checkpoint : {CK}")
print(f"episode {EP} ({hi-lo} frames)")
print(f"  RMS accel GROUND TRUTH  {jt:.5f}")
print(f"  RMS accel PREDICTED     {jp:.5f}")
print(f"  ratio                   {r:.1f}x   ->  {'SMOOTH' if r <= 5 else 'WILL SHAKE - train longer' if r >= 10 else 'BORDERLINE'}")
print(f"  MAE                     {np.degrees(np.abs(P-T).mean()):.2f} deg")
