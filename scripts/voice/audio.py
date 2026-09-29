"""Microphone in, speech out -- through PipeWire, with no extra Python packages.

WHY pw-record, NOT sounddevice / PyAudio
Both need PortAudio, which this env does not have, and both see a Bluetooth headset only
through PortAudio's ALSA shim. `pw-record` talks to PipeWire directly: a Bluetooth headset,
a USB mic and the 3.5 mm jack are all just nodes, picked by name, and PipeWire resamples
whatever the device delivers (8 or 16 kHz for a headset) to the 16 kHz Whisper wants.

WHY THE MIC STAYS OPEN FOR THE WHOLE SESSION
Opening a Bluetooth headset's microphone switches it from A2DP to the headset (HFP)
profile, which takes a second or two and drops the first words. Opening it once and
discarding what arrives while the arm moves (`flush()`) keeps the profile stable -- and is
also what makes "no new command while the arm is moving" true by construction.

WHY THE SPEAKER IS THE DEFAULT SINK
`say()` goes through speech-dispatcher (`spd-say`), the same path LeRobot's own log_say()
uses, and that always plays on PipeWire's DEFAULT sink. To hear it on the monitor rather
than in the headset, make the monitor the default once:  voice_test.py --set-output HDMI
"""
from __future__ import annotations

import collections
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

import numpy as np

RATE = 16000                    # what Whisper is trained on
CHUNK_S = 0.03                  # 30 ms analysis window
CHUNK = int(RATE * CHUNK_S)     # samples per window


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Node:
    id: int
    kind: str           # "sink" (speaker) or "source" (microphone)
    name: str           # node.name -- what pw-record --target and wpctl want
    description: str    # what a human recognises


def list_nodes() -> tuple[list[Node], dict[str, str]]:
    """Every audio sink and source, plus the current defaults {"sink": name, "source": name}."""
    if not shutil.which("pw-dump"):
        sys.exit("pw-dump not found -- this needs PipeWire (Ubuntu 22.10 and later ship it).")
    dump = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, check=True).stdout)
    nodes, defaults = [], {}
    for obj in dump:
        props = (obj.get("info") or {}).get("props") or {}
        cls = props.get("media.class")
        if cls in ("Audio/Sink", "Audio/Source"):
            nodes.append(Node(obj["id"], "sink" if cls == "Audio/Sink" else "source",
                              props.get("node.name", ""), props.get("node.description", "")))
        if (obj.get("props") or {}).get("metadata.name") == "default":
            for m in obj.get("metadata") or []:
                v = m.get("value")
                name = v.get("name") if isinstance(v, dict) else None
                if m.get("key") == "default.audio.sink" and name:
                    defaults["sink"] = name
                elif m.get("key") == "default.audio.source" and name:
                    defaults["source"] = name
    return nodes, defaults


def find_node(kind: str, query: str) -> Node:
    """The one sink/source whose name or description contains `query` (case-insensitive)."""
    nodes, _ = list_nodes()
    q = query.lower()
    hits = [n for n in nodes if n.kind == kind and (q in n.name.lower() or q in n.description.lower())]
    if len(hits) == 1:
        return hits[0]
    have = "\n".join(f"    {n.id:4d}  {n.description}   ({n.name})" for n in nodes if n.kind == kind)
    if not hits:
        sys.exit(f"No {kind} matches {query!r}. Available:\n{have or '    (none)'}")
    sys.exit(f"{len(hits)} {kind}s match {query!r} -- be more specific. Available:\n{have}")


def set_default(node: Node) -> None:
    subprocess.run(["wpctl", "set-default", str(node.id)], check=True)


# ---------------------------------------------------------------------------
# Speech out
# ---------------------------------------------------------------------------

def say(text: str, blocking: bool = True) -> None:
    """Speak on the default sink. Blocking by default, so the mic never hears it."""
    print(f"  [say] {text}", flush=True)
    if not shutil.which("spd-say"):
        return
    cmd = ["spd-say", "--language", "en", text]
    if blocking:
        # --wait returns when the audio has actually finished playing.
        subprocess.run(cmd + ["--wait"], timeout=15, check=False)
    else:
        subprocess.Popen(cmd)


# ---------------------------------------------------------------------------
# Microphone
# ---------------------------------------------------------------------------

@dataclass
class Utterance:
    audio: np.ndarray       # float32 mono at RATE, -1..1
    seconds: float
    peak_rms: float         # loudest 30 ms window, int16 units
    threshold: float        # the start threshold it had to beat


class Mic:
    """A PipeWire capture stream read on a background thread into a short ring buffer.

    target=None records the default source; otherwise a node.name from list_nodes().
    """

    def __init__(self, target: str | None = None, buffer_s: float = 30.0):
        self._init_buffer(buffer_s)
        cmd = ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16"]
        if target:
            cmd += ["--target", target]
        # stderr to a file, not a pipe: nobody reads it while the session runs, and a pipe
        # that fills up (64 KB of xrun warnings) blocks pw-record -- the mic would go dead.
        self._err = tempfile.TemporaryFile()
        self._proc = subprocess.Popen(cmd + ["-"], stdout=subprocess.PIPE, stderr=self._err)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        # Fail now, not on the first listen(), when the target does not exist.
        time.sleep(0.3)
        if self._proc.poll() is not None:
            self._err.seek(0)
            err = self._err.read().decode(errors="replace").strip()
            raise RuntimeError(f"pw-record exited at once: {err or 'no message'}  (cmd: {' '.join(cmd)})")

    def _init_buffer(self, buffer_s: float) -> None:
        self._buf: collections.deque[np.ndarray] = collections.deque(maxlen=int(buffer_s / CHUNK_S))
        self._cv = threading.Condition()
        self._alive = True

    def _push(self, chunk: np.ndarray) -> None:
        with self._cv:
            self._buf.append(chunk)
            self._cv.notify_all()

    def _ended(self) -> None:
        self._alive = False
        with self._cv:
            self._cv.notify_all()

    def _pump(self) -> None:
        nbytes = CHUNK * 2
        while self._alive:
            raw = self._proc.stdout.read(nbytes)
            if not raw or len(raw) < nbytes:
                break
            self._push(np.frombuffer(raw, dtype=np.int16).copy())
        self._ended()

    def _next(self, timeout: float) -> np.ndarray | None:
        with self._cv:
            if not self._buf:
                self._cv.wait(timeout)
            if not self._buf:
                if not self._alive:
                    raise RuntimeError("the microphone stream ended (device unplugged or disconnected?)")
                return None
            return self._buf.popleft()

    def flush(self) -> None:
        """Drop everything captured so far -- speech during a run is not a command."""
        with self._cv:
            self._buf.clear()

    def noise_floor(self, seconds: float = 0.5) -> float:
        """Median RMS over `seconds` of (hopefully) silence, int16 units."""
        rms, deadline = [], time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            c = self._next(timeout=0.5)
            if c is not None:
                rms.append(_rms(c))
        return float(np.median(rms)) if rms else 0.0

    def listen(self, wait_s: float | None = None, max_s: float = 6.0, end_silence_s: float = 0.7,
               start_factor: float = 3.0, min_threshold: float = 250.0,
               cancel: threading.Event | None = None) -> Utterance | None:
        """Block until one utterance has been spoken, and return it. None if nobody spoke.

        Energy gate, measured against this room right now: a window counts as speech when
        its RMS beats `start_factor` x the noise floor (and at least `min_threshold`).
        Speech starts after 3 such windows in a row (90 ms, so a click does not trigger
        it) and ends after `end_silence_s` below 2/3 of that threshold. 300 ms before the
        start are kept, or the first consonant -- the "r" of "red" -- is cut off.

        Returns None when nobody spoke within `wait_s`, or as soon as `cancel` is set.
        """
        self.flush()
        floor = self.noise_floor()
        on = max(floor * start_factor, min_threshold)
        off = on * 2 / 3
        pre = collections.deque(maxlen=int(0.3 / CHUNK_S))
        voiced, started, got, silent, peak = 0, False, [], 0.0, 0.0
        t0 = time.perf_counter()
        while True:
            if cancel is not None and cancel.is_set():
                return None
            c = self._next(timeout=0.5)
            if c is None:
                if wait_s is not None and not started and time.perf_counter() - t0 > wait_s:
                    return None
                continue
            r = _rms(c)
            if not started:
                pre.append(c)
                voiced = voiced + 1 if r > on else 0
                if voiced >= 3:
                    started, got = True, list(pre)
                elif wait_s is not None and time.perf_counter() - t0 > wait_s:
                    return None
                continue
            got.append(c)
            peak = max(peak, r)
            silent = silent + CHUNK_S if r < off else 0.0
            if silent >= end_silence_s or len(got) * CHUNK_S >= max_s:
                break
        audio = np.concatenate(got).astype(np.float32) / 32768.0
        return Utterance(audio, len(audio) / RATE, peak, on)

    def close(self) -> None:
        self._alive = False
        if getattr(self, "_proc", None) is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class PlaybackMic(Mic):
    """A Mic fed from an in-memory recording at real-time pace instead of from PipeWire.

    Used by the self-test: the same listen() -- noise floor, start and end detection --
    runs on known audio, so a failure points at the chain rather than at the room. Nothing
    reaches a speaker, so it disturbs no one and no other app's sound can leak in.
    """

    def __init__(self, audio: np.ndarray, buffer_s: float = 30.0):
        self._init_buffer(buffer_s)
        self._proc = None
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        n = len(pcm) // CHUNK

        def feed():
            t0 = time.perf_counter()
            for i in range(n):
                if not self._alive:
                    return
                self._push(pcm[i * CHUNK:(i + 1) * CHUNK])
                # Real time, like a microphone: listen() times its silences in wall-clock terms.
                delay = t0 + (i + 1) * CHUNK_S - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
            self._ended()

        self._thread = threading.Thread(target=feed, daemon=True)
        self._thread.start()


def _rms(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
