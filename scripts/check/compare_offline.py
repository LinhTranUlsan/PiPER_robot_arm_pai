#!/usr/bin/env python3
"""Compare several policies offline on the same episodes -- before running the robot.

    python scripts/check/compare_offline.py <repo_id> <ten>=<checkpoint> [<ten>=<ckpt> ...] [--eps 20,50,80]

Vi du:
    python scripts/check/compare_offline.py linh/piper_2cam \
        ACT=outputs/act_2cam_full/checkpoints/last/pretrained_model \
        DP=outputs/dp_2cam_full/checkpoints/last/pretrained_model \
        pi0=~/PiPER_teleoperation/pi0_out/pi0_2cam/checkpoints/last/pretrained_model

Prints MAE before/after the grasp. Any name containing "pi0" gets the rename_map.
READ-ONLY. Never touches the robot.
"""
import os
import sys

import numpy as np
import torch

from lerobot.configs.policies import PreTrainedConfig
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.policies import make_pre_post_processors
from lerobot.policies.factory import make_policy

PI0_MAP = {
    "observation.images.front": "observation.images.base_0_rgb",
    "observation.images.wrist": "observation.images.left_wrist_0_rgb",
}


def replay(name, ckpt, ds, episodes):
    rename = PI0_MAP if "pi0" in name.lower() else None
    cfg = PreTrainedConfig.from_pretrained(ckpt)
    cfg.pretrained_path = ckpt
    cfg.device = "cuda"
    if "pi0" not in name.lower() and getattr(cfg, "type", "") == "diffusion":
        cfg.noise_scheduler_type = "DDIM"
        cfg.num_inference_steps = 10
    pol = make_policy(cfg, ds_meta=ds.meta, rename_map=rename).eval()
    pre, post = make_pre_post_processors(
        policy_cfg=cfg, pretrained_path=ckpt,
        preprocessor_overrides={
            "device_processor": {"device": "cuda"},
            "rename_observations_processor": {"rename_map": rename or {}},
        })
    print(f"--- {name} ---")
    print("  ep   MAE pre-grasp   MAE post-grasp   MAE gripper   roughness")
    rows = []
    for e in episodes:
        lo = int(ds.meta.episodes["dataset_from_index"][e])
        hi = int(ds.meta.episodes["dataset_to_index"][e])
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
        P, T = np.array(P), np.array(T)
        g = T[:, 6]
        o = np.where(g > 0.05)[0]
        t = o[0] + np.where(g[o[0]:] < 0.02)[0][0]
        pre_mae = np.degrees(np.abs(P[:t, :6] - T[:t, :6]).mean())
        post_mae = np.degrees(np.abs(P[t:, :6] - T[t:, :6]).mean())
        grip = np.abs(P[:, 6] - T[:, 6]).mean() * 1000
        ratio = (np.sqrt((np.diff(P[:, :6], n=2, axis=0) ** 2).mean())
                 / np.sqrt((np.diff(T[:, :6], n=2, axis=0) ** 2).mean()))
        rows.append((pre_mae, post_mae, grip, ratio))
        print(f"  {e:3d}     {pre_mae:6.2f} deg      {post_mae:6.2f} deg     "
              f"{grip:5.2f} mm      {ratio:5.1f}x")
    r = np.array(rows)
    flag = "SMOOTH" if r[:, 3].mean() <= 5 else "WILL SHAKE" if r[:, 3].mean() >= 10 else "BORDERLINE"
    print(f"  AVG     {r[:,0].mean():6.2f} deg      {r[:,1].mean():6.2f} deg     "
          f"{r[:,2].mean():5.2f} mm      {r[:,3].mean():5.1f}x  <- {flag}\n")
    del pol
    torch.cuda.empty_cache()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    eps = [20, 50, 80]
    for a in sys.argv[1:]:
        if a.startswith("--eps="):
            eps = [int(x) for x in a.split("=", 1)[1].split(",")]
    if len(args) < 2:
        print(__doc__)
        return 1
    ds = LeRobotDataset(args[0])
    for spec in args[1:]:
        name, _, ckpt = spec.partition("=")
        ckpt = os.path.expanduser(ckpt)
        if not os.path.isdir(ckpt):
            print(f"--- {name} --- (checkpoint not found: {ckpt})\n")
            continue
        replay(name, ckpt, ds, eps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
