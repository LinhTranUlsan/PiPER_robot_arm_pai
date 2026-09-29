#!/usr/bin/env python3
"""Run the colour policy by voice: say a colour, the arm puts that cube in the jar.

    python scripts/deploy/voice_rollout.py <the usual lerobot-rollout flags> \\
        --duration=25 [--voice.mic=NAME] [--voice.turns=10] [--voice.score=true]

Takes the flags lerobot-rollout takes (README PART 3 has the full command). Leave out
--interactive and --task: the voice loop is the interactive part, and it sets the task.

ONE TURN
    listen  ->  "Say a color"; waits for one utterance on the microphone
    decide  ->  Whisper (+ your voice profile), then exactly one colour, or it asks again
    run     ->  "green cube"; the policy runs with "Put the green cube in the jar." for
                --duration seconds (size it from check_dataset.py's max episode length)
    return  ->  the arm goes back to the pose it was in when this script started
    repeat

Speech while the arm is moving is discarded, not queued: a new command is only taken
once the arm is home. Say "stop" while it is listening to end the session; Ctrl+C at any
moment stops it too.

WHY NOT lerobot-rollout --interactive
That reads typed /commands on stdin. This drives the same RolloutController -- same
strategy, same inference engine, same return-to-start move -- from the microphone instead,
so policy and hardware stay connected and warm between turns exactly as they do there.

VOICE FLAGS (ours; everything else goes to LeRobot)
    --voice.mic=NAME        microphone whose name contains NAME (default: the default source)
    --voice.model=ID        Whisper checkpoint (default openai/whisper-small)
    --voice.language=en     Whisper language, en or vi
    --voice.profile=FILE    your enrolled voice (voice_test.py --enroll); default
                            outputs/voice_profile.npz if it exists, 'none' = Whisper only
    --voice.turns=N         stop after N turns (default 0 = until "stop" / Ctrl+C)
    --voice.log=FILE        CSV of every turn (default outputs/voice_runs.csv)
    --voice.score=true      after each turn, type the outcome in this terminal -- for the
                            10-turn evaluation: ok / wrong colour / grasp / release failed
"""
from __future__ import annotations

import csv
import logging
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "voice"))
from asr import DEFAULT_MODEL, Whisper  # noqa: E402
from audio import Mic, find_node, list_nodes, say  # noqa: E402
from commands import COLORS, prompt_for  # noqa: E402
from voiceprint import DEFAULT_PROFILE, OTHER, VoiceProfile, decide, trim  # noqa: E402

logger = logging.getLogger("voice_rollout")

SCORES = {"o": "ok", "c": "wrong colour", "g": "grasp failed", "r": "release failed",
          "x": "other failure", "s": "skip (not counted)"}


def pop_voice_flags() -> dict[str, str]:
    """Take every --voice.* flag out of sys.argv before draccus sees it (it rejects unknowns)."""
    opts, rest = {}, [sys.argv[0]]
    args = iter(sys.argv[1:])
    for a in args:
        if a.startswith("--voice."):
            key, eq, val = a[len("--voice."):].partition("=")
            opts[key] = val if eq else next(args, "")
        else:
            rest.append(a)
    sys.argv = rest
    known = {"mic", "model", "language", "turns", "log", "score", "profile"}
    unknown = set(opts) - known
    if unknown:
        sys.exit(f"unknown voice flag(s): {', '.join('--voice.' + u for u in sorted(unknown))}; "
                 f"known: {', '.join('--voice.' + k for k in sorted(known))}")
    return opts


class VoiceSession:
    """The microphone front-end of a RolloutController, run on its own thread."""

    def __init__(self, controller, events, mic: Mic, asr: Whisper, turns: int, log_path: Path, score: bool,
                 profile: VoiceProfile | None = None):
        self.c, self.events, self.mic, self.asr, self.profile = controller, events, mic, asr, profile
        self.turns, self.score = turns, score
        self.quit = threading.Event()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        new = not log_path.exists() or log_path.stat().st_size == 0
        self._log_file = open(log_path, "a", newline="")
        self._log = csv.writer(self._log_file)
        if new:
            self._log.writerow(["time", "turn", "heard", "color", "prompt", "listen_s", "asr_ms",
                                "run_s", "ended", "returned", "score"])
        self.log_path = log_path
        self.tally: dict[str, int] = {}

    # -- helpers -------------------------------------------------------------------------

    def _wait(self, *names: str, timeout: float | None = None) -> str | None:
        """Block until one of the named controller events fires (or the session stops)."""
        wanted = [self.events[n] for n in names] + [self.events["stopped"]]
        deadline = None if timeout is None else time.perf_counter() + timeout
        while not self.quit.is_set():
            for n in (*names, "stopped"):
                if self.events[n].is_set():
                    return n
            if deadline is not None and time.perf_counter() > deadline:
                return None
            wanted[0].wait(0.1)
        return "stopped"

    def _clear(self, *names: str) -> None:
        for n in names:
            self.events[n].clear()

    def _ask_score(self, color: str) -> str:
        keys = "  ".join(f"{k}={v}" for k, v in SCORES.items())
        while True:
            try:
                a = input(f"\n  RESULT of '{color}'?  {keys}\n  > ").strip().lower()[:1]
            except EOFError:
                return ""
            if a in SCORES:
                return SCORES[a]

    # -- the loop ------------------------------------------------------------------------

    def run(self) -> None:
        try:
            self._loop()
        except Exception:
            logger.exception("voice loop failed")
        finally:
            self.quit.set()
            self.c.stop()

    def _loop(self) -> None:
        turn = 0
        say("Ready")
        while not self.quit.is_set() and not self.c.stopped:
            if self.turns and turn >= self.turns:
                say(f"{turn} turns done. Stopping.")
                return

            # ---- listen: only here, with the arm home and holding -------------------------
            say("Say a color")
            t_listen = time.perf_counter()
            u = None
            while u is None and not self.quit.is_set() and not self.c.stopped:
                u = self.mic.listen(wait_s=30, cancel=self.quit)
            if u is None:
                return
            listen_s = time.perf_counter() - t_listen
            t0 = time.perf_counter()
            text = self.asr.transcribe(u.audio)
            match = self.profile.match_features(self.asr.encode(trim(u.audio))) if self.profile else None
            asr_ms = (time.perf_counter() - t0) * 1000
            p, how = decide(text, match)
            print(f"\n  heard {text!r}  ({u.seconds:.1f}s, {asr_ms:.0f} ms)"
                  + (f"   profile: {match.color or '-'}  {match.detail}" if match else ""), flush=True)
            if p.command == "stop":
                say("Stopping")
                return
            if not p.color:
                print(f"  -> refused: {p.reason}", flush=True)
                self._log.writerow([time.strftime("%H:%M:%S"), "", text, "", "", f"{listen_s:.1f}",
                                    f"{asr_ms:.0f}", "", "refused: " + p.reason, "", ""])
                self._log_file.flush()
                say("Sorry, say one color")
                continue

            # ---- run one turn -------------------------------------------------------------
            turn += 1
            prompt = prompt_for(p.color)
            print(f"  -> TURN {turn}: {p.color.upper()}  [{how}]   task {prompt!r}", flush=True)
            say(f"{p.color} cube")
            self.c.set_task(prompt)
            if self.c.task != prompt:
                raise RuntimeError(f"the engine kept task {self.c.task!r}; refusing to run with it")
            self._clear("segment_started", "segment_ended", "failed")
            t_run = time.perf_counter()
            if not self.c.start():
                print("  controller refused to start -- stopping", flush=True)
                return
            ended = self._wait("segment_ended", "failed")
            run_s = time.perf_counter() - t_run
            if ended != "segment_ended":
                print(f"  turn ended by: {ended}", flush=True)
                self._record(turn, text, p.color, prompt, listen_s, asr_ms, run_s, ended, "", "")
                return

            # ---- back to the start pose ---------------------------------------------------
            self._clear("reset_done", "reset_failed", "reset_skipped")
            self.c.reset()
            back = self._wait("reset_done", "reset_failed", "reset_skipped", timeout=30)
            if back != "reset_done":
                print(f"  RETURN MOVE: {back or 'timed out'} -- the arm may NOT be at its start pose",
                      flush=True)
                say("The arm did not return. Check the robot.")
                self._record(turn, text, p.color, prompt, listen_s, asr_ms, run_s, "duration",
                             back or "timeout", "")
                return
            result = self._ask_score(p.color) if self.score else ""
            self._record(turn, text, p.color, prompt, listen_s, asr_ms, run_s, "duration", "yes", result)

    def _record(self, turn, text, color, prompt, listen_s, asr_ms, run_s, ended, returned, result):
        self._log.writerow([time.strftime("%H:%M:%S"), turn, text, color, prompt, f"{listen_s:.1f}",
                            f"{asr_ms:.0f}", f"{run_s:.1f}", ended, returned, result])
        self._log_file.flush()
        if result:
            self.tally[result] = self.tally.get(result, 0) + 1

    def close(self) -> None:
        self._log_file.close()
        if self.tally:
            counted = {k: v for k, v in self.tally.items() if not k.startswith("skip")}
            n = sum(counted.values())
            print("\n  === SCORE ===")
            for k, v in sorted(counted.items(), key=lambda kv: -kv[1]):
                print(f"    {k:15} {v:3d}   {100 * v / n:5.1f} %")
            print(f"    ({n} counted turns)")
        print(f"\n  every turn is in {self.log_path}")


def main() -> int:
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        return 0
    opts = pop_voice_flags()
    for bad in ("--interactive", "--task"):
        if any(a.startswith(bad) for a in sys.argv):
            sys.exit(f"drop {bad}: the voice loop is the interactive part, and it sets the task")
    # The task a run starts from is replaced before every turn; this is only what the engine
    # holds while the arm sits at home.
    sys.argv.append(f"--task={prompt_for(COLORS[0])}")

    # Before anything touches the robot: a missing microphone or a Whisper download is
    # better found with the arm still off.
    target = find_node("source", opts["mic"]).name if opts.get("mic") else None
    _, defaults = list_nodes()
    print(f"  microphone : {target or defaults.get('source', '?')}")
    print(f"  speaker    : {defaults.get('sink', '?')}  (the default -- voice_test.py --set-output to change)")
    asr = Whisper(opts.get("model") or DEFAULT_MODEL, opts.get("language") or "en")
    ppath = opts.get("profile") or str(DEFAULT_PROFILE)
    profile = None
    if ppath.lower() != "none":
        if Path(ppath).exists():
            profile = VoiceProfile.load(Path(ppath))
            print(f"  voice      : profile {ppath} (" + ", ".join(
                f"{'other' if c == OTHER else c} x{len(v)}" for c, v in profile.takes.items()) + ")")
        elif opts.get("profile"):
            sys.exit(f"--voice.profile={ppath} does not exist -- record it: voice_test.py --enroll")
        else:
            print(f"  voice      : Whisper only (no {ppath}; voice_test.py --enroll records one)")
    mic = Mic(target=target)

    from lerobot.configs import parser
    from lerobot.rollout import LinkedEvent, RolloutConfig, build_rollout_context, create_strategy
    from lerobot.rollout.controller import RolloutController, RolloutEvent
    from lerobot.utils.import_utils import register_third_party_plugins
    from lerobot.utils.process import ProcessSignalHandler
    from lerobot.utils.utils import init_logging
    from lerobot.utils.visualization_utils import init_visualization, shutdown_visualization

    names = {RolloutEvent.SEGMENT_STARTED: "segment_started", RolloutEvent.SEGMENT_ENDED: "segment_ended",
             RolloutEvent.RESET_DONE: "reset_done", RolloutEvent.RESET_FAILED: "reset_failed",
             RolloutEvent.RESET_SKIPPED: "reset_skipped", RolloutEvent.ENGINE_FAILED: "failed",
             RolloutEvent.STRATEGY_FAILED: "failed", RolloutEvent.STOPPED: "stopped"}
    events = {n: threading.Event() for n in set(names.values())}

    def run(cfg: RolloutConfig) -> None:
        init_logging()
        if cfg.duration <= 0:
            sys.exit("--duration=<seconds> is required: it is how long one turn runs. Size it from "
                     "check_dataset.py's max episode length.")
        if cfg.display_data:
            init_visualization(cfg.display_mode, session_name="voice_rollout", ip=cfg.display_ip,
                               port=cfg.display_port)
        signal_handler = ProcessSignalHandler(use_threads=True, display_pid=False)
        ctx = build_rollout_context(cfg, LinkedEvent(signal_handler.shutdown_event))
        strategy = create_strategy(cfg.strategy)

        def on_event(event, payload=None):
            name = names.get(event)
            if name:
                events[name].set()
            if event in (RolloutEvent.ENGINE_FAILED, RolloutEvent.STRATEGY_FAILED):
                print(f"\n  {event.value}:\n{controller.failure_traceback}", flush=True)

        session = None
        try:
            strategy.setup(ctx)
            controller = RolloutController(strategy, ctx, on_event=on_event)
            session = VoiceSession(controller, events, mic, asr, int(opts.get("turns") or 0),
                                   Path(opts.get("log") or "outputs/voice_runs.csv"),
                                   (opts.get("score") or "false").lower() == "true", profile)
            voice = threading.Thread(target=session.run, name="voice", daemon=True)
            voice.start()
            # The control loop runs on THIS thread, as in lerobot-rollout; the voice loop only
            # calls the controller's thread-safe methods.
            controller.serve()
            session.quit.set()
            voice.join(timeout=5)
        except KeyboardInterrupt:
            print("\n  interrupted", flush=True)
        finally:
            if session is not None:
                session.quit.set()
            strategy.teardown(ctx)
            if cfg.display_data:
                shutdown_visualization(cfg.display_mode)
            if session is not None:
                session.close()

    # parser.wrap() reads the config class off the annotation. `from __future__ import
    # annotations` turns that into the string "RolloutConfig", which it cannot resolve from
    # a function-local import -- so hand it the class itself.
    run.__annotations__["cfg"] = RolloutConfig
    run = parser.wrap()(run)

    register_third_party_plugins()
    try:
        run()
    finally:
        mic.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
