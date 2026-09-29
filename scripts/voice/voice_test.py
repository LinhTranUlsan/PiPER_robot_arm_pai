#!/usr/bin/env python3
"""Test the voice chain on its own -- no robot, no policy, no CAN.

    python scripts/voice/voice_test.py --list                  # speakers and microphones
    python scripts/voice/voice_test.py --headset T14           # headset -> 16 kHz mic mode, default mic
    python scripts/voice/voice_test.py --set-output HDMI       # speak on the monitor
    python scripts/voice/voice_test.py --set-input  <name>     # listen on the headset
    python scripts/voice/voice_test.py --say "Red cube"        # one sentence out loud
    python scripts/voice/voice_test.py --selftest              # full chain, NO microphone
    python scripts/voice/voice_test.py --enroll                # learn YOUR voice (profile)
    python scripts/voice/voice_test.py                         # live: speak into the mic
    python scripts/voice/voice_test.py --rounds 10 --csv voice_log.csv

--selftest synthesises a fixed list of phrases IN MEMORY and runs them through the same
speech detection, Whisper and colour parser the rollout uses. It is silent and touches no
audio device, so it works before the headset is paired. It says nothing about the
microphone or the speaker -- --say and live mode test those.

Live mode is the rehearsal for the rollout: it asks for a colour, listens, and says back
what it understood, exactly as voice_rollout.py will -- only no arm moves.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from asr import DEFAULT_MODEL, Whisper  # noqa: E402
from audio import (RATE, Mic, PlaybackMic, bluez_mic_format, find_node, list_nodes, say,  # noqa: E402
                   set_default, set_headset_mode)
from commands import COLORS, parse_color, prompt_for  # noqa: E402
from voiceprint import DEFAULT_PROFILE, OTHER, VoiceProfile, decide, trim  # noqa: E402

# What is spoken, and the colour parse_color() must return. None = it must REFUSE.
#
# The refusals are the part that matters for safety: a phrase with no colour that gets
# accepted starts the arm when nobody asked it to. Those must be 0 wrong. The colour
# phrases are only a measure -- the synthetic voice says "red" worse than a person does.
SELFTEST = [
    ("Put the yellow cube in the jar", "yellow"),
    ("Put the green cube in the jar", "green"),
    ("Yellow", "yellow"), ("Green", "green"),
    ("Yellow cube please", "yellow"), ("Please pick the green one", "green"),
    ("Yellow, no, green", None), ("Put the red cube in the jar", None), ("Blue", None),
    ("Hello there", None), ("Thank you", None), ("Hello", None),
    ("What time is it", None), ("Can you hear me", None), ("Okay", None), ("Nice job", None),
]


def load_profile(path: str) -> VoiceProfile | None:
    """The enrolled profile, or None (Whisper alone) when there is none or it was switched off."""
    if path.lower() == "none":
        return None
    if not Path(path).exists():
        print(f"  no voice profile at {path} -- Whisper only. Record one:  --enroll")
        return None
    prof = VoiceProfile.load(Path(path))
    print(f"  voice profile {path}: " + ", ".join(f"{'other' if c == OTHER else c} x{len(v)}"
                                                for c, v in prof.takes.items()))
    return prof


def cmd_list() -> int:
    nodes, defaults = list_nodes()
    for kind, title in (("sink", "SPEAKERS (sinks)"), ("source", "MICROPHONES (sources)")):
        print(f"\n{title}")
        rows = [n for n in nodes if n.kind == kind]
        if not rows:
            print("    (none)")
        for n in rows:
            mark = "*" if defaults.get(kind) == n.name else " "
            print(f"  {mark} {n.id:4d}  {n.description}\n           {n.name}")
    print("\n  * = default. say() always plays on the default speaker; the rollout listens on")
    print("    the default microphone unless --mic is given.")
    for name, (codec, rate) in bluez_mic_format().items():
        warn = "   <-- 8 kHz: run --headset <name> for 16 kHz" if rate and rate < 16000 else ""
        print(f"\n  Bluetooth mic {name}: codec {codec}, {rate} Hz{warn}")
    src = defaults.get("source", "")
    if src.startswith("alsa_output") or ".monitor" in src:
        print("\n  NOTE: the default microphone is a speaker MONITOR, i.e. no real mic is selected.")
        print("        Connect the headset, then:  --set-input <part of its name>")
    if not any("bluez" in n.name for n in nodes):
        print("\n  No Bluetooth audio device. Pair and connect the headset first (see README PART 3).")
    return 0


TTS_MODEL = "facebook/mms-tts-eng"   # MMS-TTS, CC-BY-NC 4.0 -- fine for a lab self-test


def synthesize(phrases: list[str]) -> list[np.ndarray]:
    """Each phrase as 16 kHz float audio, generated in memory -- nothing is played."""
    import torch
    from transformers import AutoTokenizer, VitsModel

    tok = AutoTokenizer.from_pretrained(TTS_MODEL)
    model = VitsModel.from_pretrained(TTS_MODEL).eval()
    if model.config.sampling_rate != RATE:
        sys.exit(f"{TTS_MODEL} speaks at {model.config.sampling_rate} Hz, expected {RATE}")
    out = []
    for i, text in enumerate(phrases):
        torch.manual_seed(i)            # VITS samples its prosody; seed it so reruns match
        with torch.inference_mode():
            wav = model(**tok(text, return_tensors="pt")).waveform[0].numpy()
        out.append(0.5 * wav / max(np.abs(wav).max(), 1e-6))
    return out


def cmd_selftest(args) -> int:
    print(f"Self-test: synthesising {len(SELFTEST)} phrases with {TTS_MODEL} (in memory, silent)\n")
    speech = synthesize([p for p, _ in SELFTEST])
    asr = Whisper(args.model, args.language)
    rng = np.random.default_rng(0)
    hit = pos = false_accept = vad_miss = 0
    for (phrase, want), wav in zip(SELFTEST, speech):
        # 1 s of room noise, the phrase, 1.5 s of room noise: listen() measures the noise
        # floor on the first half second exactly as it does on a real microphone.
        pad = lambda s: rng.normal(0, 0.003, int(s * RATE)).astype(np.float32)  # noqa: E731
        mic = PlaybackMic(np.concatenate([pad(1.0), wav + pad(len(wav) / RATE), pad(1.5)]))
        try:
            u = mic.listen(wait_s=5)
        finally:
            mic.close()
        if u is None:
            vad_miss += 1
            print(f"  [FAIL] {phrase!r}: speech detection never triggered")
            continue
        t0 = time.perf_counter()
        text = asr.transcribe(u.audio)
        dt = time.perf_counter() - t0
        got = parse_color(text)
        if want is None:
            tag = "ok  " if got.color is None else "FAIL"
            false_accept += got.color is not None
        else:
            pos += 1
            hit += got.color == want
            tag = "ok  " if got.color == want else ("miss" if got.color is None else "FAIL")
            false_accept += got.color not in (None, want)     # a WRONG colour is the dangerous miss
        print(f"  [{tag}] said {phrase!r:32} heard {text!r:34} -> "
              f"{got.color or '(refused: ' + got.reason + ')'}   ASR {dt*1000:.0f} ms")
    print(f"\n  colour phrases understood : {hit}/{pos}   (synthetic voice -- measure yours in live mode)")
    print(f"  wrong colour / false start: {false_accept}   (must be 0)")
    print(f"  speech detection misses  : {vad_miss}   (must be 0)")
    print("\n  'miss' = refused and would ask again: safe. 'FAIL' = would move the arm wrongly.")
    print("  This does NOT test the microphone or the speaker:  --say \"hello\"  and live mode do.")
    return 0 if (false_accept == 0 and vad_miss == 0) else 1


OTHER_HINTS = ["hello", "okay", "thank you", "your own name", "robot", "wait a moment",
               "any sentence", "a cough or a laugh"]


def cmd_enroll(args) -> int:
    """Record your takes of each colour, and of things that are NOT commands."""
    target = find_node("source", args.mic).name if args.mic else None
    asr = Whisper(args.model, args.language)
    mic = Mic(target=target)
    takes: dict[str, list] = {}
    print("\nSay each word the way you will say it to the robot -- English or Vietnamese,")
    print("any pronunciation; it only has to sound like itself each time. Wait for the prompt.\n")
    plan = [(c, f"Say {c}") for c in COLORS for _ in range(args.takes)]
    plan += [(OTHER, "Say something that is not a colour") for _ in range(args.others)]
    try:
        i = 0
        while i < len(plan):
            label, spoken = plan[i]
            n = sum(1 for c, _ in plan[:i] if c == label) + 1
            total = args.takes if label != OTHER else args.others
            hint = f" -- e.g. '{OTHER_HINTS[(n - 1) % len(OTHER_HINTS)]}'" if label == OTHER else ""
            print(f"  [{label if label != OTHER else 'not a command'} {n}/{total}]{hint}", flush=True)
            say(spoken)
            u = mic.listen(wait_s=args.wait)
            if u is None:
                print("     nothing heard -- again")
                continue
            if u.seconds >= 5.5:
                print("     too long (over 5 s) -- say just the word, again")
                continue
            takes.setdefault(label, []).append(asr.encode(trim(u.audio)))
            print(f"     ok: {u.seconds:.1f}s, Whisper heard {asr.transcribe(u.audio)!r}")
            i += 1
    except KeyboardInterrupt:
        print("\n  interrupted -- nothing saved")
        return 1
    finally:
        mic.close()
    prof = VoiceProfile.build(takes)
    problems = prof.check()
    print(f"\n  threshold {prof.threshold:.3f}")
    if problems:
        print("  Some takes are not recognised by the others -- say the words more consistently:")
        for p in problems:
            print(f"     {p}")
        print("  Saved anyway; run --enroll again if live mode keeps refusing.")
    else:
        print("  every take is recognised as itself by the others -- consistent")
    path = Path(args.profile)
    prof.save(path)
    print(f"  saved {path}. Live mode and voice_rollout.py use it from now on.")
    say("Voice profile saved")
    return 0


def cmd_live(args) -> int:
    target = find_node("source", args.mic).name if args.mic else None
    nodes, defaults = list_nodes()
    print(f"microphone : {target or defaults.get('source', '?')}   (--mic to change)")
    print(f"speaker    : {defaults.get('sink', '?')}   (--set-output to change)\n")
    asr = Whisper(args.model, args.language)
    prof = load_profile(args.profile)
    mic = Mic(target=target)
    writer = None
    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        f = open(args.csv, "a", newline="")
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(["time", "round", "heard", "color", "reason", "decided_by", "profile", "audio_s",
                             "asr_ms", "peak_rms", "threshold"])
    print(f"Say one of: {', '.join(COLORS)}  (or 'stop'). Ctrl+C to quit.\n")
    n = 0
    try:
        while args.rounds == 0 or n < args.rounds:
            n += 1
            say("Say a color")
            u = mic.listen(wait_s=args.wait)
            if u is None:
                print(f"  round {n}: nobody spoke within {args.wait:.0f}s")
                continue
            t0 = time.perf_counter()
            text = asr.transcribe(u.audio)
            match = prof.match_features(asr.encode(trim(u.audio))) if prof else None
            asr_ms = (time.perf_counter() - t0) * 1000
            p, how = decide(text, match)
            print(f"  round {n}: heard {text!r}  ({u.seconds:.1f}s, peak {u.peak_rms:.0f} vs threshold "
                  f"{u.threshold:.0f}, {asr_ms:.0f} ms)")
            if match:
                print(f"     profile: {match.color or '-'}   {match.detail}")
            if writer:
                writer.writerow([time.strftime("%H:%M:%S"), n, text, p.color or "", p.reason, how,
                                 match.detail if match else "", f"{u.seconds:.2f}", f"{asr_ms:.0f}",
                                 f"{u.peak_rms:.0f}", f"{u.threshold:.0f}"])
                f.flush()
            if p.command == "stop":
                say("Stopping")
                break
            if p.color:
                print(f"     -> {p.color.upper()}   [{how}]   prompt: {prompt_for(p.color)!r}")
                say(f"{p.color} cube")
            else:
                print(f"     -> refused: {p.reason}")
                say("Sorry, say one color")
    except KeyboardInterrupt:
        pass
    finally:
        mic.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="list speakers and microphones")
    g.add_argument("--set-output", metavar="NAME", help="make the speaker matching NAME the default")
    g.add_argument("--set-input", metavar="NAME", help="make the microphone matching NAME the default")
    g.add_argument("--headset", metavar="NAME", help="put the Bluetooth headset matching NAME in its 16 kHz "
                                                   "mic mode and make it the default microphone")
    g.add_argument("--say", metavar="TEXT", help="speak TEXT on the default speaker")
    g.add_argument("--selftest", action="store_true", help="whole chain on synthetic speech, silent, no mic")
    g.add_argument("--enroll", action="store_true", help="record your voice profile (takes per colour)")
    ap.add_argument("--mic", metavar="NAME", help="live mode: listen on this microphone, not the default")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Whisper checkpoint (default {DEFAULT_MODEL})")
    ap.add_argument("--language", default="en", help="Whisper language: en (default) or vi")
    ap.add_argument("--rounds", type=int, default=0, help="live mode: stop after N rounds (0 = until Ctrl+C)")
    ap.add_argument("--wait", type=float, default=15.0, help="live mode: seconds to wait for speech")
    ap.add_argument("--csv", help="live mode: append every round to this CSV")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE),
                    help=f"voice profile to write (--enroll) or use (live); 'none' = Whisper only "
                         f"(default {DEFAULT_PROFILE})")
    ap.add_argument("--takes", type=int, default=5, help="--enroll: takes per colour (default 5)")
    ap.add_argument("--others", type=int, default=6, help="--enroll: non-command takes (default 6)")
    args = ap.parse_args()

    if args.list:
        return cmd_list()
    if args.set_output:
        n = find_node("sink", args.set_output)
        set_default(n)
        print(f"default speaker -> {n.description}")
        say("This is the robot speaker")
        return 0
    if args.headset:
        profile, codec, rate = set_headset_mode(args.headset)
        print(f"headset -> {profile}   codec {codec}, {rate} Hz, now the default microphone")
        if rate and rate < 16000:
            print(f"  WARNING: {rate} Hz is phone quality -- Whisper will mishear. This headset has no "
                  "16 kHz (mSBC) mode here; a wired or USB mic will recognise far better.")
            return 1
        return 0
    if args.set_input:
        n = find_node("source", args.set_input)
        set_default(n)
        print(f"default microphone -> {n.description}")
        return 0
    if args.say:
        say(args.say)
        return 0
    if args.selftest:
        return cmd_selftest(args)
    if args.enroll:
        return cmd_enroll(args)
    return cmd_live(args)


if __name__ == "__main__":
    sys.exit(main())
