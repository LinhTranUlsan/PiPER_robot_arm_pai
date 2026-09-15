#!/usr/bin/env python3
"""Check a MULTI-OBJECT dataset: every episode must contain N complete pick-place cycles.

    python scripts/check/check_dataset_multi.py <repo_id> [n_objects]     # default 2

check_dataset.py stops at the FIRST open-grasp-release cycle, so on a two-object task it
reports an episode as clean even when the second pick is missing. This one counts them.

Also reports the two numbers rollout needs:
  |action - state|  -> --robot.max_relative_target
  episode length    -> --duration

READ-ONLY. Never modifies the dataset.
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

OPEN, SHUT = 0.05, 0.02      # gripper metres: above OPEN = open, below SHUT = closed on an object
FPS = 30


def cycles(g):
    """Complete (grasp, release) pairs, plus a trailing grasp that was never released.

    One pick-place reads as  open -> close(grasp) -> open(release).  The demos then close
    the gripper again on the way home, which is NOT a second grasp: it is only a real one
    if an open follows it.  So a trailing close is returned separately, not counted.
    """
    pairs, trailing, i, n = [], None, 0, len(g)
    while i < n:
        o = np.where(g[i:] > OPEN)[0]           # opened, ready to approach
        if not len(o):
            break
        i += o[0]
        c = np.where(g[i:] < SHUT)[0]           # closed = grasp
        if not len(c):
            break
        grasp = i + c[0]
        r = np.where(g[grasp:] > OPEN)[0]       # opened again = release
        if not len(r):
            trailing = grasp                    # closed at the end, never reopened
            break
        release = grasp + r[0]
        pairs.append((grasp, release))
        i = release + 1
    return pairs, trailing


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    repo = sys.argv[1]
    want = int(sys.argv[2]) if len(sys.argv) > 2 else 2

    root = os.path.expanduser("~/.cache/huggingface/lerobot/") + repo
    files = sorted(glob.glob(root + "/data/*/*.parquet"))
    if not files:
        print(f"no parquet under {root}")
        return 1
    df = pd.concat([pd.read_parquet(f) for f in files])

    a = np.stack(df["action"].values).astype(float)
    s = np.stack(df["observation.state"].values).astype(float)
    ep = df["episode_index"].values
    eps = np.unique(ep)

    bad, counts, grasps, releases = {}, [], [], []
    for e in eps:
        x = a[ep == e]
        pairs, trailing = cycles(x[:, 6])
        counts.append(len(pairs))
        if len(pairs) < want:
            missing = want - len(pairs)
            why = f"only {len(pairs)} complete pick-place cycles, expected {want}"
            if trailing is not None:
                why += " -- last object was GRASPED BUT NEVER RELEASED"
            bad[int(e)] = why
        elif len(pairs) > want:
            bad[int(e)] = f"{len(pairs)} pick-place cycles, expected {want}"
        for gi, ri in pairs:
            grasps.append(x[gi, :6])
            releases.append(x[ri, :6])

    lengths = np.array([(ep == e).sum() for e in eps]) / FPS
    lead = np.abs(a[:, :6] - s[:, :6])
    counts = np.array(counts)

    print(f"=== {repo}  (expecting {want} objects per episode) ===")
    print(f"  {len(eps)} episodes · {len(df)} frames · {len(df)/FPS/60:.1f} min\n")

    if bad:
        print(f"  BROKEN EPISODES ({len(bad)} of {len(eps)}):")
        for e, why in sorted(bad.items()):
            print(f"     ep {e:3d}: {why}")
        idx = ",".join(str(e) for e in sorted(bad))
        print("\n  Remove them:")
        print(f"     lerobot-edit-dataset --repo_id={repo} --new_repo_id={repo}_clean \\")
        print(f"       --operation.type=delete_episodes --operation.episode_indices='[{idx}]'")
    else:
        print(f"  BROKEN EPISODES: none -- every episode has {want} complete cycles")

    print(f"\n  cycles/episode    : min {counts.min()}  median {int(np.median(counts))}  max {counts.max()}")
    print(f"  length (s)        : p50 {np.percentile(lengths,50):.1f}"
          f"  p95 {np.percentile(lengths,95):.1f}  max {lengths.max():.1f}")
    print(f"                      -> use --duration={int(np.ceil(np.percentile(lengths,95)/5)*5)} for rollout")

    if len(grasps):
        G = np.array(grasps)
        print(f"\n  GRASP spread (deg): {np.round(np.degrees(G.max(0) - G.min(0)), 1)}")
    if len(releases):
        R = np.array(releases)
        print(f"  RELEASE std (deg) : {np.round(np.degrees(R.std(0)), 1)}   (smaller is better --"
              " every release should land in the same box)")

    p999, mx = np.percentile(lead, 99.9), lead.max()
    print(f"\n  |action-state|    : p99.9 {p999:.4f}  max {mx:.4f} rad")
    print(f"                      -> --robot.max_relative_target >= {np.ceil(mx*10)/10 + 0.2:.1f}")
    print(f"  RMS acceleration  : {np.sqrt((np.diff(a[:,:6],n=2,axis=0)**2).mean()):.5f}")
    print(f"  all-zero actions  : {int((np.abs(a).sum(1) == 0).sum())} rows")
    print(f"\n  Steps for 16.7 epochs (batch 8): {int(16.7*len(df)/8):,}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
