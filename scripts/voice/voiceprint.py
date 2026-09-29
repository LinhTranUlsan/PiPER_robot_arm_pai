"""Your own voice as the reference: say each colour a few times, then match against that.

    python scripts/voice/voice_test.py --enroll          # writes outputs/voice_profile.npz

WHY
Whisper transcribes toward the English it was trained on, so an accent it does not expect
turns "yellow" into "hello" and "green" into "grin" -- and then no colour word is found. A
profile does not care how the word is pronounced, only that it sounds like YOUR earlier
takes of it. Say it in English or Vietnamese ("vàng", "xanh lá"), but say it the same way
you enrolled it.

HOW
Each take is stored as Whisper's encoder states (50 frames/s). A new utterance is aligned
to every take with dynamic time warping, so speaking faster or slower still lines up, and
the closest colour wins -- but only if it is close in absolute terms (`threshold`, set from
how much your own takes of one colour differ) and clearly closer than both the other
colour and the enrolled NON-commands (`margin`). Distance alone cannot separate a colour
from a similar-sounding word said by the same voice ("mellow", "jello"); the non-command
takes are what can. Otherwise the profile says "don't know", and decide() falls back to
Whisper.

decide() never lets the two disagree silently: profile says yellow and Whisper reads
"green" -> refused. The profile can rescue a word Whisper could not spell; it can never
overrule one Whisper read clearly.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from audio import RATE
from commands import COLORS, CORRECTION_WORDS, STOP_WORDS, Parsed, _normalise, parse_color

DEFAULT_PROFILE = Path("outputs/voice_profile.npz")
OTHER = "_other"     # enrolled NON-commands: hello, okay, your name, a cough ...
MARGIN = 1.3         # the runner-up (another colour, or OTHER) must be 30 % further away
# The threshold is this many times the MEAN distance between your own takes of one colour.
# Measured on synthetic speakers: real takes of a colour reach ~1.5-1.75x, the nearest
# non-command ~1.55-1.95x -- the ranges touch, which is why OTHER exists. 1.5 refuses the
# odd genuine take (it asks again) rather than accept a word that was not a command.
THRESHOLD_X = 1.5


def trim(audio: np.ndarray, pad_s: float = 0.08) -> np.ndarray:
    """Cut the silence listen() keeps around a word, so DTW compares speech with speech."""
    hop = int(0.02 * RATE)
    n = len(audio) // hop
    if n < 3:
        return audio
    rms = np.sqrt((audio[: n * hop].reshape(n, hop) ** 2).mean(1))
    on = np.where(rms > max(rms.max() * 0.1, 1e-4))[0]
    if not len(on):
        return audio
    pad = int(pad_s / 0.02)
    a, b = max(on[0] - pad, 0), min(on[-1] + pad + 1, n)
    return audio[a * hop: b * hop]


def dtw(a: np.ndarray, b: np.ndarray) -> float:
    """Mean cosine distance along the best alignment of a [n, d] onto b [m, d].

    Steps (i-1, j), (i-1, j-1), (i-1, j-2): a row depends only on the row before, so each
    row is one vectorised numpy step -- fast enough in pure numpy for words of a second or
    two. It lets b run up to twice as fast as a, or stall; plenty for one person saying one
    word twice.
    """
    a = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-8)
    b = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-8)
    cost = 1.0 - a @ b.T
    n, m = cost.shape
    inf = np.inf
    prev = np.full(m, inf)
    prev[0] = cost[0, 0]
    for i in range(1, n):
        shift1 = np.concatenate(([inf], prev[:-1]))
        shift2 = np.concatenate(([inf, inf], prev[:-2]))
        prev = cost[i] + np.minimum(prev, np.minimum(shift1, shift2))
    return float(prev[-1] / n)


@dataclass
class Match:
    color: str | None       # None when too far from everything, or too close to call
    distance: float
    margin: float           # runner-up distance / best distance
    detail: str


class VoiceProfile:
    def __init__(self, takes: dict[str, list[np.ndarray]], threshold: float):
        self.takes, self.threshold = takes, threshold

    # -- building ------------------------------------------------------------------------

    @classmethod
    def build(cls, takes: dict[str, list[np.ndarray]]) -> "VoiceProfile":
        """Set the threshold from the takes themselves: how far apart ONE colour's takes are."""
        within = []
        for color, feats in takes.items():
            if color == OTHER:
                continue
            for i, f in enumerate(feats):
                others = [dtw(f, g) for j, g in enumerate(feats) if j != i]
                if others:
                    within.append(min(others))
        threshold = THRESHOLD_X * float(np.mean(within)) if within else 0.2
        return cls(takes, threshold)

    def check(self) -> list[str]:
        """Leave-one-out on the enrolment itself: every take must be recognised as its colour.

        With one take held out the rest are fewer, so a consistent take lands a little past
        the threshold routinely. Only a take that matches the WRONG class, or sits well past
        (x1.3), is a real inconsistency worth re-recording.
        """
        problems = []
        for color, feats in self.takes.items():
            for i, f in enumerate(feats):
                rest = {c: [g for j, g in enumerate(v) if not (c == color and j == i)]
                        for c, v in self.takes.items()}
                m = VoiceProfile(rest, self.threshold * 1.3).match_features(f)
                if m.color != (None if color == OTHER else color):
                    problems.append(f"{color} take {i + 1}: matched {m.color or 'nothing'} ({m.detail})")
        return problems

    # -- files ---------------------------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        arrays = {f"{c}__{i}": f.astype(np.float16) for c, fs in self.takes.items() for i, f in enumerate(fs)}
        np.savez_compressed(path, __threshold=np.array(self.threshold), **arrays)

    @classmethod
    def load(cls, path: Path) -> "VoiceProfile":
        z = np.load(path)
        takes: dict[str, list[np.ndarray]] = {}
        for k in sorted(z.files):
            if k == "__threshold":
                continue
            color, _, _ = k.partition("__")
            takes.setdefault(color, []).append(z[k].astype(np.float32))
        missing = [c for c in COLORS if c not in takes]
        extra = [c for c in takes if c not in COLORS and c != OTHER]
        if missing or extra:
            raise ValueError(f"{path} was enrolled for {sorted(takes)}, but the table has {list(COLORS)}"
                             " -- enrol again: voice_test.py --enroll")
        return cls(takes, float(z["__threshold"]))

    # -- matching ------------------------------------------------------------------------

    def match_features(self, f: np.ndarray) -> Match:
        best = {c: min(dtw(f, g) for g in fs) for c, fs in self.takes.items() if fs}
        order = sorted(best, key=best.get)
        d1 = best[order[0]]
        d2 = best[order[1]] if len(order) > 1 else np.inf
        margin = d2 / max(d1, 1e-6)
        detail = ("  ".join(f"{'other' if c == OTHER else c} {best[c]:.3f}" for c in order)
                  + f"  (threshold {self.threshold:.3f})")
        if order[0] == OTHER:
            return Match(None, d1, margin, "closest to a non-command: " + detail)
        if d1 > self.threshold:
            return Match(None, d1, margin, "too far from every take: " + detail)
        if margin < MARGIN:
            return Match(None, d1, margin, "too close to call: " + detail)
        return Match(order[0], d1, margin, detail)


def decide(text: str, match: Match | None) -> tuple[Parsed, str]:
    """Whisper's reading plus the profile's match -> one decision, and how it was reached."""
    p = parse_color(text)
    if match is None:                       # no profile: Whisper alone, as before
        return p, "whisper"
    if p.command == "stop":
        return p, "whisper"
    norm = _normalise(text)
    if any(f" {w} " in norm for w in CORRECTION_WORDS + STOP_WORDS):
        return p if p.color is None else Parsed(None, None, "heard a correction"), "whisper"
    if p.color and match.color and p.color != match.color:
        return Parsed(None, None, f"Whisper read {p.color} but it sounds like your {match.color} -- say it again"), "conflict"
    if p.color:
        return p, "whisper + profile" if match.color == p.color else "whisper"
    # The profile may only fill in where Whisper found NO colour word at all. A refusal for a
    # reason -- two colours, a colour not on the table -- stands: Whisper heard something
    # definite, and the profile matching it to the nearest take would be a guess.
    if p.reason not in ("no colour word in it", "heard nothing"):
        return p, "whisper"
    if match.color:
        return Parsed(match.color, None, ""), "profile"
    return Parsed(None, None, f"{p.reason}; profile: {match.detail}"), "neither"
