#!/usr/bin/env python3
"""lerobot-record for the colour task: the prompt is chosen PER EPISODE.

    python scripts/record/record_colors.py <the usual lerobot-record flags> [--plan_seed=0]

Takes exactly the flags lerobot-record takes (README PART 3 has the full command);
--dataset.single_task is ignored, because the task is what this script decides.

WHY NOT lerobot-record
lerobot-record holds ONE --dataset.single_task for the whole run, so changing colour means
quitting and restarting with --resume -- a reconnect and a 30 s "move the master" check per
episode. This runs the same record() with two things swapped in: the task for each episode,
and a keyboard listener that also knows one number key per colour. Everything else -- the loop, the
cadence report, saving, re-recording -- is LeRobot's own code, unchanged.

THE PLAN
The colours are COLORS in scripts/voice/commands.py (now yellow and green). Episodes come
in groups of one per colour, one LAYOUT each: the cubes stay where they are, and each
episode of the group picks a different colour into the jar. Put the picked cube back where
it was before the next one. Then shuffle all cubes for the next layout. With two colours:

    episode   0  1 | 2  3 | 4  5 | 6 ...
    layout    1  1 | 2  2 | 3  3 | 4 ...
    colour    yellow/green in a shuffled order, different every layout

Episodes with the same picture and different prompts are what force the policy to read
the prompt; recorded one colour at a time, it can learn "the cube over there" instead.
The order is shuffled so "first episode of a new layout" is not always the same colour.

The order is a function of (plan_seed, episode index) only, so --resume=true continues the
plan exactly where it stopped, and a re-recorded episode keeps its colour.

KEYS (added to LeRobot's own)
    Space / n / ->    end the current phase (episode -> reset -> next episode)
    r / <-            re-record the episode (same colour)
    q / Esc           stop and save
    1 2 ...           override the colour of the NEXT episode, in COLORS order (1=yellow 2=green).
                      The colour it displaces takes the overridden colour's later slot, so
                      the layout still gets one of each. Not remembered across --resume, and
                      it cannot undo a colour already recorded in the layout;
                      check_dataset_colors.py reports any layout that ends up uneven.

The colour is also spoken ("Next, red cube") during every reset, and printed with the
episode number, so there is never a need to look at the plan.
"""
from __future__ import annotations

import json
import logging
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "voice"))
sys.path.insert(0, str(HERE.parents[1] / "check"))
from commands import COLORS, KEY_FOR_COLOR, prompt_for  # noqa: E402

PER_LAYOUT = len(COLORS)


def planned(seed: int, index: int) -> tuple[int, int, str]:
    """(layout, slot, colour) of episode `index`. Pure: resume and re-record agree with it."""
    layout, slot = divmod(index, PER_LAYOUT)
    order = random.Random(f"{seed}:{layout}").sample(COLORS, PER_LAYOUT)
    return layout, slot, order[slot]


class ColorPlan:
    """Which colour each episode gets, and the one-shot key override."""

    def __init__(self, seed: int, say):
        self.seed = seed
        self._say = say
        self.override: str | None = None
        self.locked: tuple[int, str] | None = None     # (episode index, colour) being recorded
        # layout -> {colour taken early by an override: the colour it displaced}. Keeps the
        # layout one-of-each: the displaced colour moves to the slot the override came from.
        self.swaps: dict[int, dict[str, str]] = {}
        self.phase = "start"
        self.next_index: int | None = None

    def _planned(self, index: int) -> str:
        layout, _, color = planned(self.seed, index)
        return self.swaps.get(layout, {}).get(color, color)

    def peek(self, index: int) -> tuple[str, str]:
        if self.override:
            return self.override, "key override"
        if self.locked and self.locked[0] == index:
            return self.locked[1], "re-record, same colour"
        color = self._planned(index)
        return color, ("plan" if color == planned(self.seed, index)[2] else "moved here by an override")

    def take(self, index: int) -> tuple[str, str]:
        color, why = self.peek(index)
        if self.override:
            displaced = self._planned(index)
            if displaced != color:
                self.swaps.setdefault(planned(self.seed, index)[0], {})[color] = displaced
        self.override = None
        self.locked = (index, color)
        return color, why

    def press(self, color: str) -> None:
        self.override = color
        when = "the NEXT episode (this one keeps its colour)" if self.phase == "record" else "the next episode"
        print(f"\n  >>> key {KEY_FOR_COLOR[color]}: {color.upper()} for {when}\n", flush=True)
        self._say(f"Next, {color} cube")


def argv_value(flag: str, default: str | None = None) -> str | None:
    """--flag=value or --flag value from sys.argv, as draccus will see it."""
    for i, a in enumerate(sys.argv):
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def pop_flag(flag: str, default: str) -> str:
    """Take one of OUR flags out of sys.argv before draccus sees it (it rejects unknowns)."""
    value = default
    for i, a in enumerate(list(sys.argv)):
        if a.startswith(flag + "="):
            value = a.split("=", 1)[1]
            sys.argv.remove(a)
        elif a == flag and i + 1 < len(sys.argv):
            value = sys.argv[i + 1]
            del sys.argv[i:i + 2]
    return value


def existing_episodes(repo: str | None, root: str | None) -> int:
    if not repo:
        return 0
    base = Path(root) if root else Path(os.path.expanduser(f"~/.cache/huggingface/lerobot/{repo}"))
    try:
        return int(json.loads((base / "meta/info.json").read_text())["total_episodes"])
    except (OSError, KeyError, ValueError):
        return 0


def main() -> int:
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        return 0
    seed = int(pop_flag("--plan_seed", "0"))

    # The plan decides the task; a stray --dataset.single_task would only mislead the reader
    # of the command line. Keep one in argv regardless, since the dataset card reads it.
    given = argv_value("--dataset.single_task")
    if given is not None:
        print(f"  NOTE: --dataset.single_task={given!r} is ignored -- the plan sets each episode's task.")
        sys.argv = [a for a in sys.argv if not a.startswith("--dataset.single_task")]
    sys.argv.append(f"--dataset.single_task={prompt_for(COLORS[0])}")

    repo = argv_value("--dataset.repo_id")
    root = argv_value("--dataset.root")
    resume = (argv_value("--resume", "false") or "false").lower() == "true"
    play = (argv_value("--play_sounds", "true") or "true").lower() == "true"
    if not resume and argv_value("--dataset.no_stamp", "false").lower() != "true":
        print("  NOTE: without --dataset.no_stamp=true LeRobot appends a timestamp to the repo id,"
              " and --resume later cannot find it.")

    # Import after argv is final: lerobot_record builds its CLI parser at import time.
    from lerobot.scripts import lerobot_record as R
    from lerobot.utils import keyboard_input as K
    from lerobot.utils.import_utils import register_third_party_plugins
    from lerobot.utils.utils import log_say

    def say(text: str) -> None:
        log_say(text, play)

    plan = ColorPlan(seed, say)

    # ---- keyboard: LeRobot's controls, plus a number per colour -------------------------------------------
    def init_keyboard_listener():
        events = {"exit_early": False, "rerecord_episode": False, "stop_recording": False}
        keys = {v: c for c, v in KEY_FOR_COLOR.items()}

        def on_key(name: str) -> None:
            key = name.lower()
            if key in keys:
                plan.press(keys[key])
            elif key in ("right", "n", "space"):
                K.apply_recording_control("right", events)
            elif key in ("left", "r"):
                K.apply_recording_control("left", events)
            elif key in ("esc", "q"):
                K.apply_recording_control("esc", events)

        help_ = "Space/n=next, r=re-record, q=quit, " + " ".join(f"{v}={c}" for c, v in KEY_FOR_COLOR.items())
        return K.create_key_listener(on_key, controls_help=help_), events

    R.init_keyboard_listener = init_keyboard_listener

    # ---- the task, per episode ------------------------------------------------------------
    record_loop = R.record_loop

    def record_loop_with_plan(*args, **kw):
        dataset, events = kw.get("dataset"), kw["events"]
        if dataset is not None:
            # A recording phase. Decide now and hold it for the whole episode: record_loop
            # stamps kw["single_task"] onto every frame it adds.
            index = dataset.num_episodes
            color, why = plan.take(index)
            layout, slot, _ = planned(seed, index)
            kw["single_task"] = prompt_for(color)
            plan.phase, plan.next_index = "record", index
            print("\n" + "=" * 72)
            print(f"  EPISODE {index}   layout {layout + 1}, cube {slot + 1}/{PER_LAYOUT}   ->   "
                  f"{color.upper()}   ({why})")
            print(f"  task: {kw['single_task']!r}")
            print("=" * 72 + "\n", flush=True)
            say(f"{color} cube")
        else:
            # A reset phase. Say what comes next, so the operator can set the table for it.
            last = plan.next_index if plan.next_index is not None else dataset_start
            index = last if events.get("rerecord_episode") else last + 1
            color, _ = plan.peek(index)
            _, slot, _ = planned(seed, index)
            plan.phase = "reset"
            if events.get("rerecord_episode"):
                print(f"  reset: put the {color} cube back -- episode {index} is re-recorded, same colour")
                say(f"Put it back. Again, {color} cube")
            elif slot == 0:
                print(f"  reset: NEW LAYOUT -- shuffle all the cubes. Next: {color.upper()}")
                say(f"New layout. Shuffle the cubes. Next, {color} cube")
            else:
                print(f"  reset: put the cube back where it was. Next: {color.upper()}")
                say(f"Put it back. Next, {color} cube")
        return record_loop(*args, **kw)

    R.record_loop = record_loop_with_plan

    # ---- what the operator needs before the first episode starts --------------------------
    dataset_start = existing_episodes(repo, root) if resume else 0
    layout, slot, _ = planned(seed, dataset_start)
    first, _ = plan.peek(dataset_start)
    print("\n" + "=" * 72)
    print(f"  colour recording {'/'.join(COLORS)}  --  plan_seed={seed}   dataset {repo}"
          f"{'  (resuming at episode %d)' % dataset_start if resume else ''}")
    print(f"  FIRST: episode {dataset_start}, layout {layout + 1}, cube {slot + 1}/{PER_LAYOUT}"
          f"  ->  {first.upper()}")
    if slot == 0:
        print(f"  Lay out the {len(COLORS)} cubes now; recording starts as soon as the master is confirmed.")
    else:
        print(f"  Mid-layout: set the table exactly as layout {layout + 1} was before continuing.")
    print("  keys: Space=next  r=re-record  q=quit  " + " ".join(f"{v}={c}" for c, v in KEY_FOR_COLOR.items()))
    print("=" * 72 + "\n", flush=True)

    register_third_party_plugins()
    try:
        R.record()
    finally:
        # Also after Ctrl+C: the balance is what decides whether to record more.
        logging.disable(logging.NOTSET)
        if repo:
            try:
                from check_dataset_colors import summarize
                print()
                summarize(repo, root)
            except SystemExit:
                pass
            except Exception as e:  # never hide the recording's own traceback behind this
                print(f"  (could not summarise the dataset: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
