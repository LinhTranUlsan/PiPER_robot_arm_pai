"""The colour commands, and the ONE place their prompts are defined.

Recording (scripts/record/record_colors.py) writes these prompts into the dataset, and the
voice rollout (scripts/deploy/voice_rollout.py) sends them to the policy. Both import them
from here, because a policy trained on "Put the red cube in the jar." and run on "put the
red cube into the jar" is being asked something it never saw. Change the wording here and
nowhere else -- and only before recording, since the prompt is baked into every frame.

    >>> parse_color("Put the yellow cube in the jar.")
    Parsed(color='yellow', command=None, reason='')
    >>> parse_color("yellow, no, green").color is None  # two colours -> refuse
    True
    >>> parse_color("but no green").color is None       # a correction -> refuse
    True
    >>> parse_color("red").color is None                # not on the table -> refuse
    True
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# The cubes on the table. Recording, the checker and the rollout all follow this tuple:
# two colours = layouts of two episodes, keys 1-2. For all four, put back
# ("red", "blue", "green", "yellow") -- BEFORE recording, never in the middle of a dataset.
COLORS = ("yellow", "green")
PROMPT = "Put the {color} cube in the jar."

# Keys 1..4 in record_colors.py, in this order.
KEY_FOR_COLOR = {c: str(i + 1) for i, c in enumerate(COLORS)}

# Spoken forms -> colour. Longest match wins, so "xanh lá cây" is tried before "xanh".
#
# "read" and "blew" are here on purpose: a single word said on its own gives Whisper no
# context, and it writes the homophone about as often as the colour. Nothing else in this
# task is ever read or blown, so accepting them costs nothing.
#
# Bare "xanh" is NOT here: in Vietnamese it covers both blue and green, and guessing
# between two cubes is exactly the failure this parser exists to refuse.
ALIASES = {
    "red": "red", "read": "red", "đỏ": "red", "màu đỏ": "red",
    "blue": "blue", "blew": "blue",
    "xanh dương": "blue", "xanh da trời": "blue", "xanh nước biển": "blue", "xanh biển": "blue",
    "green": "green", "xanh lá": "green", "xanh lá cây": "green",
    "yellow": "yellow", "vàng": "yellow", "màu vàng": "yellow",
}

# Session control words, only honoured while listening -- never while the arm moves.
STOP_WORDS = ("stop", "quit", "exit", "dừng lại", "kết thúc")

# A correction mid-sentence ("red, no, blue") is the speaker changing their mind, and
# Whisper routinely drops the first half of it ("but no blue") -- leaving one clean colour
# that is the wrong one. Any of these words means: refuse and ask again.
CORRECTION_WORDS = ("no", "not", "don t", "dont", "cancel", "wait", "sorry", "không", "đừng")


def prompt_for(color: str) -> str:
    if color not in COLORS:
        raise ValueError(f"unknown colour {color!r}; expected one of {COLORS}")
    return PROMPT.format(color=color)


def color_of_prompt(prompt: str) -> str | None:
    """Inverse of prompt_for, for reading a dataset's tasks back. None if it is not ours."""
    for c in COLORS:
        if prompt == prompt_for(c):
            return c
    return None


@dataclass(frozen=True)
class Parsed:
    color: str | None       # set only when exactly one colour was heard
    command: str | None     # "stop" when a stop word was heard and no colour was
    reason: str             # why nothing was accepted; empty on success


def _normalise(text: str) -> str:
    # NFC so a Vietnamese "đỏ" typed or decoded as combining marks still matches.
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return f" {' '.join(text.split())} "


def parse_color(text: str) -> Parsed:
    """Exactly one colour -> that colour. None, or two different ones -> refuse, with why."""
    norm = _normalise(text)
    if not norm.strip():
        return Parsed(None, None, "heard nothing")

    found: list[str] = []
    rest = norm
    for alias in sorted(ALIASES, key=len, reverse=True):
        pat = f" {alias} "
        while pat in rest:
            found.append(ALIASES[alias])
            # Blank it out so "xanh lá cây" does not also count as "xanh lá".
            rest = rest.replace(pat, " # ", 1)
    # Bare "xanh" is blue OR green in Vietnamese. With only one of them on the table it is
    # not ambiguous any more, so it means that one.
    xanh = [c for c in ("blue", "green") if c in COLORS]
    if " xanh " in rest and len(xanh) == 1:
        found.append(xanh[0])
        rest = rest.replace(" xanh ", " # ")
    off_table = sorted({c for c in found if c not in COLORS})
    colors = sorted({c for c in found if c in COLORS}, key=COLORS.index)

    if off_table and not colors:
        return Parsed(None, None, f"{', '.join(off_table)} is not on the table -- say {' or '.join(COLORS)}")
    if off_table:
        return Parsed(None, None, f"heard {', '.join(colors + off_table)} -- say one of {', '.join(COLORS)}")
    if colors and any(f" {w} " in norm for w in CORRECTION_WORDS):
        return Parsed(None, None, "heard a correction ('no', 'wait', ...) -- say just one colour")
    if len(colors) == 1:
        return Parsed(colors[0], None, "")
    if len(colors) > 1:
        return Parsed(None, None, f"heard {len(colors)} colours ({', '.join(colors)}) -- say one")
    if any(f" {w} " in norm for w in STOP_WORDS):
        return Parsed(None, "stop", "")
    if " xanh " in rest:
        return Parsed(None, None, "'xanh' alone is ambiguous -- say 'xanh dương' or 'xanh lá'")
    return Parsed(None, None, "no colour word in it")
