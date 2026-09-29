#!/usr/bin/env python3
"""Measure, and optionally cut, the still period at the start of every episode.

    python scripts/dataset/trim_idle.py $USER/piper_right              # measure only
    python scripts/dataset/trim_idle.py $USER/piper_right --write      # cut -> <repo>_trim

Between pressing `space` and the master arm actually turning, an operator reaches for the
arm, grips it and takes up the slack. The follower does not move, so those frames teach the
policy that standing still at the start pose is correct -- and at rollout it reproduces
exactly that: the arm waits, or in the worst case never leaves the pose at all, because a
command equal to the measured position leaves the state unchanged and the loop is closed.

Cutting them is the fix that needs no re-recording. --keep leaves a few frames of stillness
so the policy still sees the start pose it will be parked at.

Nothing is overwritten: a new dataset is written and the original is left alone.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

AUTO = ("index", "episode_index", "frame_index", "timestamp", "task_index", "task")


def joint_columns(names: list[str]) -> list[int]:
    """Indices of the joint columns, grippers excluded.

    Slicing [:, :6] would be the LEFT arm alone on a bimanual dataset, whose action vector is
    14 wide -- and in a hand-over task that arm waits through most of the episode, so the
    idle period would read as most of the recording. Grippers are dropped because they are
    metres, not radians, and a gripper twitch is not the arm setting off.
    """
    return [i for i, n in enumerate(names) if "gripper" not in n]


def idle_lengths(repo: str, thresh_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Frames each episode stays within `thresh_deg` of its own first action."""
    root = os.path.expanduser(f"~/.cache/huggingface/lerobot/{repo}")
    files = sorted(glob.glob(root + "/data/*/*.parquet"))
    if not files:
        sys.exit(f"No data at {root}")
    with open(f"{root}/meta/info.json") as f:
        cols = joint_columns(json.load(f)["features"]["action"]["names"])
    df = pd.concat([pd.read_parquet(f) for f in files])
    a = np.stack(df["action"].values).astype(float)[:, cols]
    ep = df["episode_index"].values
    eps = np.unique(ep)
    out = []
    for e in eps:
        x = np.degrees(a[ep == e])
        moved = np.abs(x - x[0]).max(1) > thresh_deg
        out.append(int(np.argmax(moved)) if moved.any() else len(moved))
    return eps, np.array(out)


def report(name: str, idle: np.ndarray, fps: int) -> None:
    print(f"  {name:22} median {np.median(idle):5.0f} f = {np.median(idle)/fps:5.2f} s"
          f"   mean {idle.mean()/fps:5.2f} s   max {idle.max()/fps:5.2f} s"
          f"   over 0.5 s: {(idle > fps//2).sum()}/{len(idle)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo_id")
    ap.add_argument("--out", help="new repo_id (default: <repo_id>_trim)")
    ap.add_argument("--threshold", type=float, default=0.2,
                    help="degrees of motion that count as 'started' (default 0.2, below "
                         "sensor noise on this arm)")
    ap.add_argument("--keep", type=int, default=5,
                    help="frames of stillness to leave in front (default 5)")
    ap.add_argument("--write", action="store_true", help="actually write the trimmed dataset")
    a = ap.parse_args()

    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    src = LeRobotDataset(a.repo_id)
    fps = src.meta.fps
    eps, idle = idle_lengths(a.repo_id, a.threshold)

    print(f"\n=== {a.repo_id} — {len(eps)} episodes, {src.meta.total_frames:,} frames ===")
    report("idle at episode start", idle, fps)
    cut = np.maximum(idle - a.keep, 0)
    print(f"\n  would cut {cut.sum():,} frames "
          f"({cut.sum()/src.meta.total_frames*100:.1f}% of the dataset), keeping {a.keep} in front")
    print(f"  frames left: {src.meta.total_frames - cut.sum():,}"
          f"   -> 16.7 epochs at batch 8 = "
          f"{round(16.7*(src.meta.total_frames-cut.sum())/8):,} steps")

    if not a.write:
        print("\n  measure only. Add --write to produce the trimmed dataset.")
        return 0

    out_id = a.out or f"{a.repo_id}_trim"
    if Path(os.path.expanduser(f"~/.cache/huggingface/lerobot/{out_id}")).exists():
        sys.exit(f"\n{out_id} already exists. Delete it or pass --out.")

    feats = {k: v for k, v in src.meta.features.items() if k not in AUTO}
    dst = LeRobotDataset.create(out_id, fps, feats, robot_type=src.meta.robot_type,
                                use_videos=True)
    print(f"\n  writing {out_id} ...")
    for n, e in enumerate(eps):
        lo = int(src.meta.episodes["dataset_from_index"][e])
        hi = int(src.meta.episodes["dataset_to_index"][e])
        start = lo + int(cut[n])
        for i in range(start, hi):
            it = src[i]
            frame = {"task": it.get("task", "")}
            for k in feats:
                v = it[k]
                v = v.numpy() if hasattr(v, "numpy") else v
                if k.startswith("observation.images."):
                    # dataset frames arrive CHW float 0..1; add_frame wants HWC uint8
                    if v.ndim == 3 and v.shape[0] in (1, 3):
                        v = v.transpose(1, 2, 0)
                    if v.dtype != np.uint8:
                        v = (v * 255).clip(0, 255).astype(np.uint8)
                frame[k] = v
            dst.add_frame(frame)
        dst.save_episode()
        print(f"    episode {e:3d}: {hi-lo:4d} -> {hi-start:4d} frames "
              f"(cut {cut[n]})", flush=True)

    # Without this the last parquet file is still an open buffer, and reading the dataset
    # back fails on "Parquet magic bytes not found in footer".
    dst.finalize()

    _, idle2 = idle_lengths(out_id, a.threshold)
    print(f"\n=== {out_id} — {len(idle2)} episodes ===")
    report("idle BEFORE", idle, fps)
    report("idle AFTER ", idle2, fps)
    print(f"\n  train on it with:")
    n2 = sum(int(src.meta.episodes['dataset_to_index'][e]) -
             int(src.meta.episodes['dataset_from_index'][e]) - int(cut[i])
             for i, e in enumerate(eps))
    print(f"    lerobot-train --policy.type=act --dataset.repo_id={out_id} \\")
    print(f"      --output_dir=outputs/act_trim --policy.push_to_hub=false "
          f"--policy.device=cuda \\")
    print(f"      --batch_size=8 --steps={round(16.7*n2/8)} --wandb.enable=false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
