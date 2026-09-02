#!/usr/bin/env python3
"""Check dataset quality before spending hours on training.

    python scripts/check/check_dataset.py <repo_id>

Reports:
  - episodes missing a step (no open / no grasp / NO RELEASE)
  - spread of grasp and release positions
  - action smoothness
  - |action - state| -> pick max_relative_target
  - episode length -> pick --duration for rollout

READ-ONLY. Never modifies the dataset.
"""
import glob
import os
import sys

import numpy as np
import pandas as pd


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    repo = sys.argv[1]
    root = os.path.expanduser(f"~/.cache/huggingface/lerobot/{repo}")
    files = sorted(glob.glob(root + "/data/*/*.parquet"))
    if not files:
        print(f"No data found at {root}")
        return 1

    df = pd.concat([pd.read_parquet(f) for f in files])
    a = np.stack(df["action"].values).astype(float)
    s = np.stack(df["observation.state"].values).astype(float)
    ep = df["episode_index"].values
    eps = np.unique(ep)

    bad, G, R = {}, [], []
    for e in eps:
        x = a[ep == e]
        g = x[:, 6]
        o = np.where(g > 0.05)[0]
        if not len(o):
            bad[int(e)] = "gripper never opened"; continue
        t = np.where(g[o[0]:] < 0.02)[0]
        if not len(t):
            bad[int(e)] = "never grasped"; continue
        t = o[0] + t[0]
        G.append(x[t, :6])
        af = np.where(g[t:] > 0.05)[0]
        if not len(af):
            bad[int(e)] = "NEVER RELEASED"; continue
        R.append(x[t + af[0], :6])

    L = np.array([(ep == e).sum() for e in eps]) / 30.0
    lead = np.abs(a[:, :6] - s[:, :6])
    jerk = np.sqrt((np.diff(a[:, :6], n=2, axis=0) ** 2).mean())
    G, R = np.array(G), np.array(R)

    print(f"=== {repo} ===")
    print(f"  {len(eps)} episodes · {len(df)} frames · {len(df)/30/60:.1f} min")
    print()
    if bad:
        print(f"  BROKEN EPISODES ({len(bad)}):")
        for e, why in sorted(bad.items()):
            print(f"     ep {e:3d}: {why}")
        print(f"\n  Remove them:")
        print(f"     lerobot-edit-dataset --repo_id={repo} --new_repo_id={repo}_clean \\")
        print(f"       --operation.type=delete_episodes \\")
        print(f"       --operation.episode_indices='{sorted(bad)}'")
    else:
        print("  BROKEN EPISODES: none -- clean")
    print()
    print(f"  complete          : {len(R)}/{len(eps)}")
    print(f"  length (s)        : p50 {np.median(L):.1f}  p95 {np.percentile(L,95):.1f}  max {L.max():.1f}")
    print(f"                      -> use --duration={int(np.percentile(L,95))+3} for rollout")
    if len(G):
        print(f"  GRASP range (deg) : {np.degrees(G.max(0)-G.min(0)).round(1)}   (spatial coverage)")
    if len(R):
        print(f"  RELEASE std (deg) : {np.degrees(R.std(0)).round(1)}   (smaller is better)")
    print(f"  |action-state|    : p99.9 {np.percentile(lead,99.9):.4f}  max {lead.max():.4f} rad")
    print(f"                      -> max_relative_target >= {np.ceil(lead.max()*10)/10 + 0.2:.1f}")
    print(f"  RMS acceleration  : {jerk:.5f}   (reference good set: ~0.0027)")
    print(f"  all-zero actions  : {int((np.abs(a).sum(1)==0).sum())} rows")
    print()
    n_frames = len(df)
    print(f"  Steps for 16.7 epochs (batch 8): {int(16.7*n_frames/8):,}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
