#!/usr/bin/env python3
"""Check the colour dataset: how many episodes per colour, and per table layout.

    python scripts/check/check_dataset_colors.py <repo_id>

record_colors.py records every table layout once per colour (COLORS in
scripts/voice/commands.py), so the policy sees the same image under different prompts and
has to read the prompt to act. This
checks that the dataset actually looks like that:

  per colour   episode count and length. Uneven counts teach the policy a favourite.
  per layout   each group of len(COLORS) episodes must be one of each colour. A layout with a colour twice
               and another missing is where the prompt carried no information.
  foreign      tasks that are not one of the colour prompts in scripts/voice/commands.py --
               a typo'd --dataset.single_task, or episodes from another dataset.

READ-ONLY. Run check_dataset.py as well: this one knows nothing about grasp or release.
"""
from __future__ import annotations

import glob
import os
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "voice"))
from commands import COLORS, color_of_prompt  # noqa: E402

PER_LAYOUT = len(COLORS)


def load_episodes(repo: str, root: str | None = None) -> pd.DataFrame:
    base = Path(root) if root else Path(os.path.expanduser(f"~/.cache/huggingface/lerobot/{repo}"))
    files = sorted(glob.glob(str(base / "meta/episodes/*/*.parquet")))
    if not files:
        sys.exit(f"No episode metadata under {base}/meta/episodes")
    df = pd.concat([pd.read_parquet(f, columns=["episode_index", "tasks", "length"]) for f in files])
    return df.sort_values("episode_index").reset_index(drop=True)


def summarize(repo: str, root: str | None = None, fps: int = 30) -> int:
    df = load_episodes(repo, root)
    # One task per episode: record_colors.py sets it before the episode starts and never
    # changes it mid-episode. More than one means something else wrote this dataset.
    df["color"] = [color_of_prompt(t[0]) if len(t) == 1 else None for t in df["tasks"]]
    foreign = df[df["color"].isna()]

    print(f"=== {repo} -- {len(df)} episodes ===\n")
    print("  colour   episodes   length p50 / max (s)")
    counts = Counter(df["color"].dropna())
    for c in COLORS:
        lens = df.loc[df["color"] == c, "length"].to_numpy() / fps
        stat = f"{np.median(lens):5.1f} / {lens.max():5.1f}" if len(lens) else "    -"
        print(f"  {c:7} {counts.get(c, 0):9d}   {stat}")
    bad = 0
    if counts:
        lo, hi = min(counts.get(c, 0) for c in COLORS), max(counts.values())
        if hi and lo < 0.8 * hi:
            bad = 1
            print(f"\n  UNEVEN: {lo} vs {hi}. Record the short colours until they are within 20 %.")

    if len(foreign):
        bad = 1
        print(f"\n  FOREIGN TASKS in {len(foreign)} episode(s) -- not one of the colour prompts:")
        for task, n in Counter(tuple(t) for t in foreign["tasks"]).most_common(5):
            print(f"     {n:4d} x {list(task)}")
        print(f"     indices: {foreign['episode_index'].tolist()[:20]}{' ...' if len(foreign) > 20 else ''}")

    if not counts:
        print("\n  No episode carries one of the colour prompts -- not a record_colors.py dataset.")
        return 1

    # Layout k = episodes k*N .. k*N+N-1 (N colours), which is how record_colors.py numbers them.
    broken, partial = [], None
    n_layouts = (len(df) + PER_LAYOUT - 1) // PER_LAYOUT
    for k in range(n_layouts):
        group = df.iloc[k * PER_LAYOUT:(k + 1) * PER_LAYOUT]
        got = list(group["color"])
        if len(group) < PER_LAYOUT:
            partial = (k, got)
        elif sorted(c for c in got if c) != sorted(COLORS):
            broken.append((k, got))
    print(f"\n  layouts   : {len(df) // PER_LAYOUT} complete"
          + (f", 1 in progress ({len(partial[1])}/{PER_LAYOUT})" if partial else ""))
    if broken:
        bad = 1
        print(f"  NOT ONE-OF-EACH ({len(broken)}): a key override, or a re-recorded episode "
              "that picked another cube")
        for k, got in broken[:10]:
            print(f"     layout {k + 1:3d} (episodes {k * PER_LAYOUT}-{k * PER_LAYOUT + PER_LAYOUT - 1}): {got}")
    else:
        print("  every complete layout has one episode of each colour")

    print(f"\n  Steps for 16.7 epochs (batch 8): {int(16.7 * df['length'].sum() / 8):,}")
    return bad


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    return summarize(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
