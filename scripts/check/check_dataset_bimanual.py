#!/usr/bin/env python3
"""Check a dataset per arm. Works for one arm or two.

    python scripts/check/check_dataset_bimanual.py <repo_id> [cycles_per_arm]   # default 1

For a bimanual task where each arm does its own pick-place, every episode must contain the
expected number of complete grasp-release cycles ON EACH ARM. A single-arm checker cannot
see that the left arm never moved.

Column indices are read from meta/info.json by NAME, so nothing here assumes an ordering.

Reports what rollout needs, per arm:
  |action - state|  -> --robot.left_max_relative_target / --robot.right_max_relative_target
  episode length    -> --duration

READ-ONLY. Never modifies the dataset.
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

OPEN, SHUT = 0.05, 0.02
FPS = 30


def cycles(g):
    """Complete (grasp, release) pairs. A trailing close with no reopen is the way home."""
    pairs, trailing, i, n = [], None, 0, len(g)
    while i < n:
        o = np.where(g[i:] > OPEN)[0]
        if not len(o):
            break
        i += o[0]
        c = np.where(g[i:] < SHUT)[0]
        if not len(c):
            break
        grasp = i + c[0]
        r = np.where(g[grasp:] > OPEN)[0]
        if not len(r):
            trailing = grasp
            break
        pairs.append((grasp, grasp + r[0]))
        i = grasp + r[0] + 1
    return pairs, trailing


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    repo = sys.argv[1]
    want = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    root = os.path.expanduser("~/.cache/huggingface/lerobot/") + repo
    info = json.load(open(f"{root}/meta/info.json"))
    names = info["features"]["action"]["names"]

    # Group columns by arm prefix. A single-arm dataset has no prefix -> one arm called "".
    arms = {}
    for i, n in enumerate(names):
        side = n.split("_")[0] if n.startswith(("left_", "right_")) else ""
        arms.setdefault(side, {"joints": [], "gripper": None})
        if n.endswith("gripper.pos"):
            arms[side]["gripper"] = i
        else:
            arms[side]["joints"].append(i)

    files = sorted(glob.glob(root + "/data/*/*.parquet"))
    if not files:
        print(f"no parquet under {root}")
        return 1
    df = pd.concat([pd.read_parquet(f) for f in files])
    a = np.stack(df["action"].values).astype(float)
    s = np.stack(df["observation.state"].values).astype(float)
    ep = df["episode_index"].values
    eps = np.unique(ep)
    lengths = np.array([(ep == e).sum() for e in eps]) / FPS

    print(f"=== {repo} ===")
    print(f"  {len(eps)} episodes · {len(df)} frames · {len(df)/FPS/60:.1f} min")
    print(f"  arms: {', '.join(k or '(single)' for k in arms)}   "
          f"expecting {want} cycle(s) per arm per episode\n")

    any_bad = False
    for side, cols in arms.items():
        label = side or "arm"
        gi, jc = cols["gripper"], cols["joints"]
        bad, counts, G, R = {}, [], [], []
        for e in eps:
            x = a[ep == e]
            pairs, trailing = cycles(x[:, gi])
            counts.append(len(pairs))
            if len(pairs) != want:
                why = f"{len(pairs)} cycle(s), expected {want}"
                if trailing is not None and len(pairs) < want:
                    why += " -- GRASPED BUT NEVER RELEASED"
                bad[int(e)] = why
            for g_i, r_i in pairs:
                G.append(x[g_i, jc])
                R.append(x[r_i, jc])

        lead = np.abs(a[:, jc] - s[:, jc])
        counts = np.array(counts)
        print(f"  --- {label.upper()} ---")
        if bad:
            any_bad = True
            print(f"    BROKEN ({len(bad)}/{len(eps)}): " +
                  ", ".join(f"ep{e}({w.split(',')[0]})" for e, w in sorted(bad.items())[:12]) +
                  (" ..." if len(bad) > 12 else ""))
            print(f"    indices: [{','.join(str(e) for e in sorted(bad))}]")
        else:
            print(f"    clean -- every episode has {want} complete cycle(s)")
        print(f"    cycles/ep       : min {counts.min()} median {int(np.median(counts))} max {counts.max()}")
        if len(G):
            print(f"    GRASP spread    : {np.round(np.degrees(np.array(G).max(0) - np.array(G).min(0)), 1)} deg")
            print(f"    RELEASE std     : {np.round(np.degrees(np.array(R).std(0)), 1)} deg  (small = box always hit)")
        flag = f"--robot.{side}_max_relative_target" if side else "--robot.max_relative_target"
        print(f"    |action-state|  : p99.9 {np.percentile(lead,99.9):.4f}  max {lead.max():.4f} rad")
        print(f"                      -> {flag}={np.ceil(lead.max()*10)/10 + 0.2:.1f}\n")

    print(f"  length (s)  : p50 {np.percentile(lengths,50):.1f}  p95 {np.percentile(lengths,95):.1f}"
          f"  max {lengths.max():.1f}")
    print(f"                -> --duration={int(np.ceil(np.percentile(lengths,95)/5)*5)}")
    print(f"  all-zero actions : {int((np.abs(a).sum(1) == 0).sum())} rows")
    print(f"  Steps for 16.7 epochs (batch 8): {int(16.7*len(df)/8):,}")
    if any_bad:
        print(f"\n  Remove broken episodes (union of the index lists above):")
        print(f"    lerobot-edit-dataset --repo_id={repo} --new_repo_id={repo}_clean \\")
        print(f"      --operation.type=delete_episodes --operation.episode_indices='[...]'")
    return 1 if any_bad else 0


if __name__ == "__main__":
    sys.exit(main())
