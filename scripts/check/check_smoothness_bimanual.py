#!/usr/bin/env python3
"""Smoothness of a policy's trajectory, reported PER ARM. Works for one arm or two.

    python scripts/check/check_smoothness_bimanual.py <checkpoint> <repo_id> [episode]

check_smoothness.py slices [:, :6], which on a bimanual dataset is the LEFT arm only -- a
jittery right arm would go unreported. This one splits the columns by name.

Thresholds measured on this rig:
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


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    ck, dsid = sys.argv[1], sys.argv[2]
    ep = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    rename = None
    if "pi0" in ck:
        rename = {"observation.images.front": "observation.images.base_0_rgb",
                  "observation.images.wrist": "observation.images.left_wrist_0_rgb"}

    ds = LeRobotDataset(dsid)
    cfg = PreTrainedConfig.from_pretrained(ck)
    cfg.pretrained_path = ck
    cfg.device = "cuda"
    pol = make_policy(cfg, ds_meta=ds.meta, rename_map=rename).eval()
    pre, post = make_pre_post_processors(
        policy_cfg=cfg, pretrained_path=ck,
        preprocessor_overrides={"device_processor": {"device": "cuda"},
                                "rename_observations_processor": {"rename_map": rename or {}}})

    names = ds.meta.info["features"]["action"]["names"]
    # Joints only: the gripper is a linear stroke, its jerk is not comparable to a joint's.
    arms = {}
    for i, n in enumerate(names):
        if n.endswith("gripper.pos"):
            continue
        side = n.split("_")[0] if n.startswith(("left_", "right_")) else ""
        arms.setdefault(side, []).append(i)

    lo = int(ds.meta.episodes["dataset_from_index"][ep])
    hi = int(ds.meta.episodes["dataset_to_index"][ep])
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

    print(f"checkpoint : {ck}")
    print(f"episode {ep} ({hi - lo} frames)   arms: {', '.join(k or '(single)' for k in arms)}\n")
    worst = 0.0
    for side, cols in arms.items():
        p, t = P[:, cols], T[:, cols]
        jp = np.sqrt((np.diff(p, n=2, axis=0) ** 2).mean())
        jt = np.sqrt((np.diff(t, n=2, axis=0) ** 2).mean())
        r = jp / jt if jt else float("inf")
        worst = max(worst, r)
        verdict = "SMOOTH" if r <= 5 else "WILL SHAKE - train longer" if r >= 10 else "BORDERLINE"
        print(f"  --- {(side or 'arm').upper()} ---")
        print(f"    RMS accel GROUND TRUTH  {jt:.5f}")
        print(f"    RMS accel PREDICTED     {jp:.5f}")
        print(f"    ratio                   {r:.1f}x   ->  {verdict}")
        print(f"    MAE                     {np.degrees(np.abs(p - t).mean()):.2f} deg\n")

    print(f"  WORST ARM: {worst:.1f}x -- "
          f"{'safe to run' if worst <= 5 else 'DO NOT RUN, train longer' if worst >= 10 else 'borderline'}")
    return 0 if worst <= 5 else 1


if __name__ == "__main__":
    sys.exit(main())
