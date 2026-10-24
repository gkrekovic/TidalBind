"""Persistent MIDI bindings and sequential score evaluation."""

from dataclasses import dataclass
from decimal import Decimal
from threading import RLock
from typing import Callable, Protocol, Any

from .bridge import GhciError
from .midi import MidiState
from .score import Block, Markup, Score


class CodeSink(Protocol):
    def send_block(self, code: str) -> None: ...
    def send_immediate(self, code: str) -> None: ...


@dataclass(frozen=True)
class Notice:
    kind: str
    message: str
    block: int | None = None
    code: str | None = None


@dataclass(frozen=True)
class Binding:
    markup: Markup
    block: int | None


class Performance:
    """Own one performance. Public operations are serialized across MIDI/UI input.

    A binding is keyed by (function, canonical MIDI source); the last explicitly
    activated declaration wins. Reactive evaluations never steal bindings back.
    The pointer indexes the next block to execute and never wraps implicitly.
    """

    def __init__(self, score: Score, sink: CodeSink, *, missing_values: str = "empty",
                 default_pedal: bool = True, channel: int | None = None,
                 notify: Callable[[Notice], None] | None = None):
        if missing_values not in ("empty", "defer", "zero"):
            raise ValueError("missing_values must be empty, defer, or zero")
        if channel is not None and not 1 <= channel <= 16:
            raise ValueError("channel must be between 1 and 16")
        self.score, self.sink = score, sink
        self.missing_values, self.default_pedal, self.channel = missing_values, default_pedal, channel
        self.notify = notify or (lambda notice: None)
        self.state = MidiState()
        self.next_index = 0
        self.bindings: dict[tuple[str, str], Binding] = {}
        self._lock = RLock()
        self._install_default()

    def _install_default(self) -> None:
        if self.default_pedal:
            markup = Markup("triggerNextBlock", "cc64", 0, 0, 0, "=", Decimal(127))
            self.bindings[(markup.function, markup.source)] = Binding(markup, None)

    def start(self) -> None:
        """Activate leading declaration-only blocks, without playing music."""
        with self._lock:
            while self.next_index < len(self.score.blocks):
                block = self.score.blocks[self.next_index]
                if block.has_code:
                    break
                self._register(block)
                self.next_index += 1
            self._notice("ready", f"{len(self.score.blocks)} blocks; next is {self.next_index + 1}"
                         if self.next_index < len(self.score.blocks) else "End of score")

    def _notice(self, kind: str, message: str, block: int | None = None,
                code: str | None = None) -> None:
        self.notify(Notice(kind, message, block, code))

    def _register(self, block: Block) -> None:
        for markup in block.markups:
            key = (markup.function, markup.source)
            self.bindings[key] = Binding(markup, block.index)
            self._notice("binding", f"{markup.function} ← {markup.source}", block.index)

    def _evaluate(self, index: int, *, activate: bool,
                  evaluated: set[int] | None = None) -> bool:
        if not 0 <= index < len(self.score.blocks):
            raise IndexError(f"Block must be between 1 and {len(self.score.blocks)}")
        block = self.score.blocks[index]
        if activate:
            self._register(block)
        if evaluated is not None:
            if index in evaluated:
                return False
            evaluated.add(index)
        if not block.has_code:
            return False
        code, missing = block.render(self.state.values, self.missing_values)
        if missing:
            self._notice("missing", f"No MIDI value yet for {', '.join(missing)} "
                         f"(policy: {self.missing_values})", index)
            if self.missing_values == "defer":
                return False
        if code:
            self.sink.send_block(code)
            self._notice("execute", f"Block {index + 1}, line {block.line}", index, code)
            return True
        return False

    def next(self, *, _evaluated: set[int] | None = None) -> bool:
        with self._lock:
            if self.next_index >= len(self.score.blocks):
                self._notice("end", "End of score; use reload to restart")
                return False
            index = self.next_index
            # Consume only once the pipe accepts the code (or a deliberate defer).
            result = self._evaluate(index, activate=True, evaluated=_evaluated)
            self.next_index += 1
            return result

    def evaluate(self, index: int) -> bool:
        """Explicit activation of a zero-based block, leaving the pointer alone."""
        with self._lock:
            return self._evaluate(index, activate=True)

    def command(self, command: str) -> None:
        if command not in ("hush", "resetCycles"):
            raise ValueError("Only hush and resetCycles are supported")
        with self._lock:
            self.sink.send_immediate(command)
            self._notice("command", command)

    def handle_midi(self, message: Any) -> None:
        with self._lock:
            if self.channel is not None and getattr(message, "channel", None) != self.channel - 1:
                return
            changes = self.state.update(message)
            if not changes:
                return
            self._notice("midi", ", ".join(f"{key}={value}" for key, value in changes.items()))
            # Snapshot before executing any action: bindings installed by this
            # event become eligible on the next event, never recursively.
            snapshot = tuple(self.bindings.values())
            actions: list[tuple[str, int | None]] = []
            for binding in snapshot:
                markup = binding.markup
                if markup.source not in changes:
                    continue
                if markup.function == "map":
                    actions.append(("block", binding.block))
                elif markup.matches(changes[markup.source]):
                    actions.append(("block" if markup.function == "triggerBlock" else markup.function,
                                    binding.block if markup.function == "triggerBlock" else None))
            evaluated: set[int] = set()
            done: set[tuple[str, int | None]] = set()
            # Navigation first, reactive blocks next, transport commands last.
            # In particular a hush event must not immediately restart a pattern.
            priority = {"triggerNextBlock": 0, "block": 1, "resetCycles": 2, "hush": 3}
            actions.sort(key=lambda item: priority[item[0]])
            errors: list[GhciError] = []
            for action, index in actions:
                if (action, index) in done:
                    continue
                done.add((action, index))
                try:
                    if action == "triggerNextBlock":
                        self.next(_evaluated=evaluated)
                    elif action == "block" and index is not None:
                        self._evaluate(index, activate=False, evaluated=evaluated)
                    else:
                        self.command(action)
                except GhciError as error:
                    # A rejected pattern must not prevent a hush bound to the
                    # same event. Fatal pipe/process errors still end dispatch.
                    errors.append(error)
            if errors:
                raise errors[0]

    def replace_score(self, score: Score) -> None:
        """Atomic reload: reset pointer/bindings; keep received controller values."""
        with self._lock:
            self.score = score
            self.next_index = 0
            self.bindings.clear()
            self._install_default()
            self.start()
