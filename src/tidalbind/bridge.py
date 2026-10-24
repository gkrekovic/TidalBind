"""GHCi transport adapted from the supplied TidalMIDI.py prototype.

A reader thread drains combined output. A unique printed fence acknowledges
commands instead of timing boot steps with sleeps. No sound is started on boot.
"""

from pathlib import Path
import json
import os
from queue import Empty, Queue
import re
import subprocess
from threading import RLock, Thread
import time
from typing import Callable, Sequence
from uuid import uuid4

from .score import ScoreError, _scan


class BridgeError(RuntimeError):
    pass


class GhciError(BridgeError):
    pass


def _boot_steps(text: str) -> list[tuple[int, str]]:
    """Read boot commands with their original source line numbers."""
    try:
        # Drop comments lexically so multiline comments outside :{ :} do not
        # become incomplete GHCi commands. Quoted strings remain untouched.
        _, text, _ = _scan(text, "<boot>")
    except ScoreError as error:
        raise BridgeError(str(error)) from error
    commands, current, block_line = [], None, 0
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped == ":{":
            if current is not None:
                raise BridgeError("Nested :{ in boot file")
            current = []
            block_line = line_number
        elif stripped == ":}":
            if current is None:
                raise BridgeError("Unmatched :} in boot file")
            commands.append((block_line, ":{\n" + "\n".join(current) + "\n:}"))
            current = None
        elif current is not None:
            current.append(line)
        elif stripped and not stripped.startswith("--"):
            commands.append((line_number, stripped))
    if current is not None:
        raise BridgeError("Unclosed :{ in boot file")
    return commands


def boot_commands(text: str) -> list[str]:
    """Read the line commands and explicit multiline blocks in BootTidal.hs."""
    return [code for _, code in _boot_steps(text)]


def _without_leading_comments(code: str) -> str:
    remaining = code.lstrip()
    while remaining.startswith(("--", "{-")):
        if remaining.startswith("--"):
            remaining = remaining.partition("\n")[2].lstrip()
            continue
        depth, position = 1, 2
        while position < len(remaining) and depth:
            if remaining.startswith("{-", position):
                depth += 1
                position += 2
            elif remaining.startswith("-}", position):
                depth -= 1
                position += 2
            else:
                position += 1
        remaining = remaining[position:].lstrip()
    return remaining


_ERROR = re.compile(r"(?:\berror:|\*\*\* Exception|unknown command|Failed,|cannot find|"
                    r"Could not find module|not a valid command|syntax:|unrecognised (?:flag|command))",
                    re.IGNORECASE)


class TidalBridge:
    def __init__(self, boot_file_path: str | Path | None = None, *,
                 command: Sequence[str] = ("ghci", "-ignore-dot-ghci"),
                 timeout: float = 30,
                 output: Callable[[str], None] | None = None):
        if timeout <= 0:
            raise ValueError("GHCi timeout must be positive")
        commands = []
        if boot_file_path is not None:
            path = Path(boot_file_path).expanduser()
            commands = _boot_steps(path.read_text(encoding="utf-8-sig"))
        self.timeout, self.output = timeout, output or (lambda line: None)
        self._lock = RLock()
        self._lines: Queue[str | None] = Queue()
        self._booted = False
        self._closed = False
        self.process = subprocess.Popen(
            list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.output_thread = Thread(target=self._read_output, daemon=True, name="tidalbind-ghci")
        self.output_thread.start()
        try:
            self._exchange(':set prompt ""\n:set prompt-cont ""', context="starting GHCi")
            for line_number, command_text in commands:
                self._exchange(command_text, context=f"loading boot file {path}, line {line_number}")
            self._booted = boot_file_path is not None
        except BaseException:
            self.stop()
            raise

    def _read_output(self) -> None:
        try:
            for line in self.process.stdout:
                line = line.rstrip("\r\n")
                # Forward asynchronous Tidal diagnostics even while the player
                # is idle; protocol fences are consumed by _exchange only.
                if not re.search(r"TIDALBIND_DONE_[0-9a-f]{32}", line):
                    self.output(line)
                self._lines.put(line)
        finally:
            self._lines.put(None)

    def _exchange(self, code: str, *, context: str = "evaluating code") -> None:
        with self._lock:
            if self._closed or self.process.poll() is not None:
                raise BridgeError("GHCi is not running")
            marker = "TIDALBIND_DONE_" + uuid4().hex
            try:
                # In +m mode a let/definition needs a blank line to finish.
                # Without it, GHCi consumes the acknowledgement as more input
                # for that definition and the caller waits until timeout.
                self.process.stdin.write(code + "\n\nputStrLn " + json.dumps(marker) + "\n\n")
                self.process.stdin.flush()
            except KeyboardInterrupt:
                self._abort()
                raise
            except (OSError, ValueError) as error:
                raise BridgeError(f"Cannot write to GHCi: {error}") from error
            deadline = time.monotonic() + self.timeout
            errors, recent = [], []
            while True:
                try:
                    line = self._lines.get(timeout=max(0, deadline - time.monotonic()))
                except KeyboardInterrupt:
                    # An interrupted request has no trustworthy reply boundary.
                    # Do not try another command (including hush) on this pipe.
                    self._abort()
                    raise
                except Empty:
                    # There is no safe way to attribute subsequent output after
                    # a timed-out command. End this session rather than replay it.
                    self._abort()
                    excerpt = code if len(code) <= 500 else code[:500] + " …"
                    details = "\nRecent GHCi output:\n" + "\n".join(recent) if recent else ""
                    raise BridgeError(
                        f"GHCi did not acknowledge a command within {self.timeout:g}s "
                        f"while {context}.\nCommand:\n{excerpt}{details}"
                    ) from None
                if line is None:
                    raise BridgeError(f"GHCi exited unexpectedly (status {self.process.poll()})")
                if marker in line:
                    prefix = line.split(marker, 1)[0]
                    if prefix.strip():
                        self.output(prefix)
                    if errors:
                        raise GhciError("GHCi rejected code: " + "\n".join(errors))
                    return
                if _ERROR.search(line):
                    errors.append(line)
                if line.strip():
                    recent.append(line)
                    recent = recent[-8:]

    def send_immediate(self, code: str) -> None:
        code = code.strip()
        if "\n" in code:
            raise ValueError("send_immediate expects a single command")
        if code:
            self._exchange(code)

    def send_block(self, code: str) -> None:
        code = code.strip()
        if not code:
            return
        core = _without_leading_comments(code)
        if core.splitlines() and core.splitlines()[0].strip() == ":{":
            if core.splitlines()[-1].strip() != ":}":
                raise BridgeError("Unclosed :{ block")
            self._exchange(core)
        elif core.startswith(":"):
            if "\n" in core:
                raise BridgeError("Put each GHCi command in its own score block")
            self._exchange(core)
        else:
            self._exchange(":{\n" + code + "\n:}")

    def _abort(self) -> None:
        self._closed = True
        if self.process.poll() is None:
            if os.name == "nt":
                # ghcup/ghci shims can launch ghc.exe as a child. Terminating
                # just the shim can leave that interpreter holding OSC ports
                # and the inherited pipes after this session ends.
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=5, check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass  # Fall back to terminating the direct process below.
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def stop(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    if self._booted and self.process.poll() is None:
                        self.send_immediate("hush")
                except BridgeError:
                    pass
                try:
                    if self.process.poll() is None:
                        self.process.stdin.write(":quit\n")
                        self.process.stdin.flush()
                        self.process.wait(timeout=3)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    self._abort()
                self._closed = True
            if self.process.poll() is None:
                self._abort()
            self.output_thread.join(timeout=3)
            self.process.stdin.close()
            self.process.stdout.close()

    close = stop

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stop()


class DryRunBridge:
    """Record resolved code; never start GHCi or produce sound."""

    def __init__(self):
        self.commands: list[str] = []

    def send_block(self, code: str) -> None:
        self.commands.append(code)

    send_immediate = send_block

    def stop(self) -> None:
        pass
