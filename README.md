# TidalBind

A Python application for the **score-embedded integral mapping** described in
*TidalBind: Towards Interactive Real-Time Control and Declarative Mapping in Live
Coding*, by Gordan Kreković and Antonio Pošćić (SMC 2026).

Load a `.tidal` score, step through its blocks with a MIDI pedal, retrigger earlier
blocks with pads, and substitute MIDI control values directly into Tidal code.
Bindings persist as the sequence advances. Resolved code is sent to a running
GHCi subprocess, which boots Tidal using your existing `BootTidal.hs`.

This implements the paper's headless workflow. The terminal displays incoming MIDI,
binding changes, evaluated blocks, substituted values, and GHCi output. Editor
plugins, projected editor highlighting, and control-bus integration are future work.

## Install

Python 3.10 or newer is required. From this directory:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, activate with `.venv\Scripts\activate` instead. The installed
`tidalbind` command and `python -m tidalbind` are equivalent. The MIDI backend is
Mido with python-rtmidi. On systems without a compatible wheel, building
python-rtmidi also requires the platform's compiler and MIDI development libraries
(e.g. `libasound2-dev` and `libjack-jackd2-dev` on Debian/Ubuntu).

For sound, independently install **GHC/GHCi, the TidalCycles Haskell package,
SuperCollider, and SuperDirt**, following the
[TidalCycles setup guide](https://tidalcycles.org/docs/getting-started/).
Start SuperDirt as usual, and use the same working `BootTidal.hs` you use in your
editor. TidalBind does not install or start SuperCollider and does not require a
new boot script. It starts its own GHCi session with `-ignore-dot-ghci` and reads
that boot file explicitly.

## Try it without hardware or sound

```sh
tidalbind examples/performance.tidal --validate
tidalbind examples/performance.tidal --dry-run --replay examples/replay.jsonl --missing-values defer
```

Replay reads ordinary Mido message dictionaries, one JSON object per line. Events
run in file order without timing delays. A single line, for example:

```json
{"type":"note_on","channel":0,"note":60,"velocity":100}
```

For manual operation without a MIDI device:

```sh
tidalbind examples/performance.tidal --dry-run --no-midi --interactive --missing-values defer
```

Type `next`, `block 2`, `hush`, `resetCycles`, `status`, `reload`, or `quit`, followed
by Enter. `block N` uses one-based block numbers and does not move the sequence
pointer. `reload` reparses the score before changing state; a parse error leaves
the current score active. A successful reload resets bindings and the pointer,
retains received MIDI values, and arms leading configuration blocks again.

## Play a score

```sh
tidalbind --list-midi
tidalbind my-piece.tidal --boot /path/to/BootTidal.hs --midi-port "Your MIDI input" --interactive
```

Use exact input names. Repeat `--midi-port` to listen to several inputs. By default,
the first available input is opened. `--channel 2` selects MIDI channel 2; the
CLI uses channels 1–16, while replay dictionaries use Mido's channels 0–15.
Ports and channels share one state cache unless filtered. `TIDAL_BOOT_PATH` can
supply the default boot path; otherwise it is `BootTidal.hs` in the working directory.
Use `--ghci /path/to/ghci` for a particular executable and `--timeout 60` for a
slower boot. Run `tidalbind --help` for all options.

**CC64 = 127 advances by one block**, matching the supplied TidalMIDI prototype.
Releases do not advance. Each matching MIDI message is an event: repeated 127
messages advance repeatedly; there is no implicit debounce or edge detection.
Use `--no-default-pedal` to disable this initial binding, or redefine it in the score.
At the end, navigation stops without wrapping or crashing; existing bindings keep
working. Ctrl+C or `quit` hushes and closes the GHCi session and MIDI ports.

## Score format and markup

Separate code blocks by one or more blank lines. Continuation lines belong to the
same block. Blank lines inside multiline comments, string gaps, or explicit GHCi
`:{` / `:}` blocks do not split a block. Comment-only sections are ignored.
UTF-8, a UTF-8 BOM, and Windows line endings are supported.

Tags use `{-! function, source, ... -}`; surrounding whitespace is optional.
Functions and source identifiers are case insensitive. Ordinary Haskell comments
are preserved, and tags mentioned inside ordinary comments are ignored. Tags
inside quoted patterns are resolved, as in the paper's `note` example.

### Triggers

```haskell
-- These leading declarations are armed automatically, without playing sound.
{-! triggerNextBlock, sustain, =, 127 -}
{-! hush, cc20, any -}
{-! resetCycles, note49, >, 0 -}

-- Advance here to arm the pad binding and evaluate its code.
{-! triggerBlock, note36, >, 0 -}
d1 $ sound "bd sd"

-- Later navigation does not remove the earlier pad binding.
d2 $ sound "hh*8"
```

| Function | On a matching event |
| --- | --- |
| `triggerBlock` | Evaluate the containing block; leave the sequence pointer alone. |
| `triggerNextBlock` | Evaluate the next block and advance the sequence pointer. |
| `hush` | Send `hush` to GHCi. |
| `resetCycles` | Send `resetCycles` to GHCi. |

Use `=`, `<`, `>`, `<=`, or `>=` followed by a numeric comparison value.
`any` matches every event for that source and takes no value. `triggerBlock`
belongs at the top of its code block; other triggers normally occupy standalone
configuration blocks. Only **leading declaration-only blocks** are armed on
startup. Later declarations become active when navigation reaches them or you
explicitly activate their block.

The confirmed replacement rule is **last explicitly activated declaration wins
for the same function and canonical source**. Thus a new `triggerBlock, note36`
replaces the earlier pad binding, while a different source or function stays
active. `sustain` and `cc64` are the same source. Likewise, a later `map, cc74`
assigns future CC74-driven evaluations to its new block. Reactive evaluations do
not reclaim bindings from newer blocks. Explicit `block N` activation can reclaim
them. Restarting/reloading clears bindings.

### Inline mapping

```haskell
d1 $ sound "bd sd" # lpf {-! map, cc74, 0, 127, 100, 9000 -}

d2 $ note "{-! map, noteOn -}" # s "superpiano"
   # gain {-! map, velocity, 0, 127, 0, 1 -}
```

`{-! map, source -}` inserts the raw value. The six-field form uses linear scaling:

```text
output = oMin + (value - cMin) * (oMax - oMin) / (cMax - cMin)
```

Reversed ranges work. Values extrapolate outside the input range; they are not
clamped or rounded to integers. Output uses plain decimal notation, with up to
16 significant digits for scaled calculations. An input range with equal
endpoints, invalid sources, nonfinite numbers, and malformed tags are rejected
with a filename and line number before a session starts.

Substitution is textual. Surrounding characters, quotes, and numeric prefixes
remain exactly as written. Prefer a complete numeric placeholder rather than the
paper's `1.{-! map, ... -}` workaround: a fractional mapped result after that prefix
would produce an invalid number.

| MIDI source | Value and event |
| --- | --- |
| `cc0` … `cc127` | Controller value, 0–127. `sustain` aliases `cc64`. |
| `noteOn` | Pitch of a pressed key, 0–127; positive-velocity note-on only. |
| `noteOff` | Pitch of a released key, 0–127. Includes note-on with velocity zero. |
| `note0` … `note127` | Velocity on pressing that key; zero on release, regardless of release velocity. |
| `velocity` | Velocity of the last positive-velocity note-on, 1–127. |
| `pitchBand` | Raw signed pitch bend, −8192…8191; `pitchBend` is an alias. |

A message updates all its values **before** evaluating code. Each affected block
is evaluated at most once per message, even when note, velocity, and a trigger all
refer to it. Bindings are snapshotted before dispatch, so new bindings cannot
recursively fire from the event that installs them. Navigation runs first, then
block evaluations, then `resetCycles`, then `hush`. This makes a hush effective even
when its event also updates a pattern. Hush does not remove bindings; subsequent
controller events can play patterns again.

### Values not yet received

The default `--missing-values empty` reproduces the paper: an unseen source
resolves to an empty string and emits a warning. This can produce invalid Tidal
code. Two optional policies help initialize controllers:

- `--missing-values defer`: arm the bindings but defer sending a block until all
  its map sources have values. The sequence pointer still advances. A subsequent
  mapped MIDI event retries the block.
- `--missing-values zero`: substitute the literal `0` for an unseen source. This
  is an output fallback, not the scaled value of input zero.

There are no implicit controller values. `--validate` checks score structure and
markup syntax; GHCi checks Haskell/Tidal syntax during actual evaluation.

## Development and validation

For the pinned, hash-checked development dependencies:

```sh
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest -q
```

On Unix, `scripts/setup.sh` creates a `.venv`, installs those dependencies, validates
and replays the example, and runs the tests. Its shell setup assumes an existing
Python installation. To regenerate the lock after intentional dependency changes:
`uv pip compile pyproject.toml --extra dev --universal --python-version 3.10 --generate-hashes -o requirements-dev.lock`.

Tests cover score parsing, all comparisons and MIDI sources, scaling, binding
replacement, missing-value policies, navigation, replay, and subprocess lifecycle,
including errors, timeouts, and cleanup. The real GHCi test runs when `ghci` is on
PATH or `TIDALBIND_TEST_GHCI` names an executable; otherwise it is explicitly skipped.
CI installs GHCi and runs the tests on Python 3.10, 3.12, and 3.13.

To exercise the actual system pipe without Tidal, MIDI hardware, or SuperDirt:

```sh
tidalbind examples/pipe-smoke.tidal --boot examples/BootPipeSmoke.hs --replay examples/pipe-smoke.jsonl
```

It must print `TidalBind pipe ready`, `4`, and `Transport check finished` from GHCi.
`BootPipeSmoke.hs` is exclusively a transport fixture; use your real `BootTidal.hs`
for music. MIDI replay and plain Haskell transport tests do not validate audible
SuperDirt output. A final hardware check should verify pedal navigation, note/pad
retriggering, a CC sweep, and hush with your actual boot file and audio system.

## Implementation

- `score.py`: block and markup parsing, validation, and textual rendering.
- `midi.py`: semantic MIDI values and state.
- `engine.py`: persistent bindings, replacement, sequence pointer, and dispatch.
- `bridge.py`: pipe-based GHCi connection adapted from the supplied `TidalMIDI.py`.
- `cli.py`: MIDI port selection, terminal controls, logging, and JSONL replay.

The bridge retains the prototype's `ghci -ignore-dot-ghci` process, combined-output
reader thread, and explicit multiline boot handling. Unique acknowledgements
replace boot sleeps; startup errors are surfaced, no demonstration kick is played,
and shutdown waits for or terminates the process. Each session owns its process;
there is no partially initialized singleton. Boot files should use ordinary GHCi
line commands and `:{` / `:}` for multiline definitions, as in the supplied script.
GHCi failures leave the navigation pointer in place; fatal pipe failures/timeouts
end the session. A successful acknowledgement establishes evaluation, not audible
playback, and asynchronous Tidal diagnostics are forwarded to the terminal.

Continuous control re-evaluates code, following the paper's implementation rather
than using control buses. Expensive Haskell evaluations can delay queued MIDI
events. This is a performance tool with visible diagnostics, not a hard real-time
scheduler. A score and its boot file are executable Haskell and must be trusted.
