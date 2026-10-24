"""Command-line player, terminal controls, and deterministic MIDI replay."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Thread
from typing import TextIO

from .bridge import BridgeError, DryRunBridge, GhciError, TidalBridge
from .engine import Notice, Performance
from .score import ScoreError, load_score


def _report(notice: Notice) -> None:
    location = f" block {notice.block + 1}" if notice.block is not None else ""
    print(f"[{notice.kind}{location}] {notice.message}", flush=True)
    if notice.code:
        print(notice.code, flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Play a MIDI-annotated TidalCycles score")
    parser.add_argument("score", nargs="?", type=Path, help="UTF-8 .tidal score")
    parser.add_argument("--boot", type=Path, default=Path(os.environ.get("TIDAL_BOOT_PATH", "BootTidal.hs")),
                        help="your existing BootTidal.hs (or TIDAL_BOOT_PATH)")
    parser.add_argument("--ghci", default="ghci", help="GHCi executable path")
    parser.add_argument("--timeout", type=float, default=30, help="GHCi command timeout in seconds")
    parser.add_argument("--midi-port", action="append", default=[], metavar="NAME",
                        help="exact input name; repeat for multiple ports (default: first available)")
    parser.add_argument("--list-midi", action="store_true", help="list MIDI input names and exit")
    parser.add_argument("--channel", type=int, choices=range(1, 17), metavar="1..16",
                        help="listen on this channel (default: all channels)")
    parser.add_argument("--no-default-pedal", action="store_true",
                        help="disable the initial CC64 = 127 next-block binding")
    parser.add_argument("--missing-values", choices=("empty", "defer", "zero"), default="empty",
                        help="unseen map values: empty as in paper, defer evaluation, or literal zero")
    parser.add_argument("--validate", action="store_true", help="validate score syntax without MIDI/GHCi")
    parser.add_argument("--dry-run", action="store_true", help="display resolved code without starting GHCi")
    parser.add_argument("--replay", type=Path, help="read MIDI messages from a JSONL file instead of hardware")
    parser.add_argument("--interactive", action="store_true", help="enable terminal commands alongside MIDI")
    parser.add_argument("--no-midi", action="store_true", help="use terminal commands without MIDI hardware")
    return parser


def _mido():
    import mido
    mido.set_backend("mido.backends.rtmidi")
    return mido


def _ports(mido) -> list[str]:
    try:
        return mido.get_input_names()
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"MIDI backend unavailable: {error}. "
                           "Connect a controller on a machine with MIDI support, "
                           "or use --dry-run --replay / --no-midi --interactive.") from error


def _replay(path: Path, performance: Performance, mido) -> None:
    # JSON uses mido's zero-based channel numbers. Replay is immediate, ordered,
    # and deterministic; it never sleeps according to live timing fields.
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                fields = json.loads(line)
                if not isinstance(fields, dict):
                    raise ValueError("expected a JSON object")
                message = mido.Message.from_dict(fields)
            except (ValueError, TypeError, KeyError, LookupError) as error:
                raise ValueError(f"{path}:{line_number}: Invalid MIDI event: {error}") from error
            performance.handle_midi(message)


def _read_console(events: Queue, stream: TextIO) -> None:
    for line in stream:
        events.put(("console", line.strip()))
    events.put(("console", "quit"))


def _console(command: str, performance: Performance, path: Path) -> bool:
    parts = command.split()
    if not parts:
        return True
    verb = parts[0].lower()
    if verb in ("quit", "q", "exit"):
        return False
    if verb in ("next", "n"):
        performance.next()
    elif verb in ("hush", "resetCycles", "resetcycles"):
        performance.command("resetCycles" if verb == "resetcycles" else verb)
    elif verb in ("block", "b") and len(parts) == 2:
        performance.evaluate(int(parts[1]) - 1)
    elif verb == "reload":
        performance.replace_score(load_score(path))
    elif verb == "status":
        print(f"Next block: {performance.next_index + 1}; "
              f"{len(performance.bindings)} active bindings", flush=True)
    else:
        print("Commands: next, block N, hush, resetCycles, reload, status, quit", flush=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.no_midi and not (args.interactive or args.replay or args.validate):
        parser.error("--no-midi needs --interactive or --replay")
    bridge = None
    try:
        if args.list_midi:
            names = _ports(_mido())
            print("\n".join(names) if names else "No MIDI inputs found.")
            return 0
        if args.score is None:
            parser.error("a .tidal score is required")
        score = load_score(args.score)
        print(f"Loaded {len(score.blocks)} blocks from {args.score}", flush=True)
        if args.validate:
            for block in score.blocks:
                print(f"  Block {block.index + 1}: line {block.line}, {len(block.markups)} annotations")
            return 0
        with ExitStack() as stack:
            mido = None
            events: Queue = Queue()
            if not args.no_midi or args.replay:
                mido = _mido()
            if not args.replay and not args.no_midi:
                names = _ports(mido)
                selected = args.midi_port or names[:1]
                if not selected:
                    raise RuntimeError("No MIDI inputs found. Use --list-midi after connecting a controller, "
                                       "or --no-midi --interactive for terminal control.")
                for name in dict.fromkeys(selected):
                    if name not in names:
                        raise ValueError(f"MIDI input {name!r} not found. Available inputs: {names}")
                    port = mido.open_input(name, callback=lambda message: events.put(("midi", message)))
                    stack.enter_context(port)
                    print(f"Listening on {name}", flush=True)
            if args.dry_run:
                bridge = DryRunBridge()
            else:
                bridge = TidalBridge(args.boot, command=(args.ghci, "-ignore-dot-ghci"),
                                    timeout=args.timeout, output=lambda line: print(f"[GHCi] {line}", flush=True))
            stack.callback(bridge.stop)
            performance = Performance(score, bridge, missing_values=args.missing_values,
                                      default_pedal=not args.no_default_pedal,
                                      channel=args.channel, notify=_report)
            performance.start()
            if args.replay:
                _replay(args.replay, performance, mido)
                return 0
            if args.interactive:
                print("Commands: next, block N, hush, resetCycles, reload, status, quit", flush=True)
                Thread(target=_read_console, args=(events, sys.stdin), daemon=True).start()
            print("Ready. CC64 = 127 advances the score; Ctrl+C stops and hushes."
                  if not args.no_default_pedal else "Ready. Ctrl+C stops and hushes.", flush=True)
            while True:
                try:
                    kind, value = events.get(timeout=0.25)
                except Empty:
                    if isinstance(bridge, TidalBridge) and bridge.process.poll() is not None:
                        raise BridgeError("GHCi exited while waiting for input")
                    continue
                try:
                    if kind == "midi":
                        performance.handle_midi(value)
                    elif not _console(value, performance, args.score):
                        break
                except (GhciError, ScoreError, ValueError, IndexError) as error:
                    # A bad edit/command must not tear down a live performance.
                    # Bridge timeouts/process failures remain fatal.
                    print(f"Error: {error}", file=sys.stderr, flush=True)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
