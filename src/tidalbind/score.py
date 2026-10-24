"""Parse score blocks without interpreting the surrounding Haskell code."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
import re


class ScoreError(ValueError):
    """An invalid score or annotation, including its source location."""


_FUNCTIONS = {name.lower(): name for name in
              ("map", "triggerBlock", "triggerNextBlock", "hush", "resetCycles")}
_SPECIAL = re.compile(r"\{-\s*!")


def canonical_source(source: str) -> str:
    source = source.strip().lower()
    aliases = {"sustain": "cc64", "pitchband": "pitchBend", "pitchbend": "pitchBend",
               "noteon": "noteOn", "noteoff": "noteOff", "velocity": "velocity"}
    if source in aliases:
        return aliases[source]
    match = re.fullmatch(r"(cc|note)(\d+)", source)
    if match and 0 <= int(match[2]) <= 127:
        return f"{match[1]}{int(match[2])}"
    raise ValueError(f"Unknown MIDI source {source!r}")


def _number(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"Expected a number, got {value!r}") from None
    if not result.is_finite() or result.copy_abs() > Decimal("1e12"):
        raise ValueError("Numbers must be finite and between -1e12 and 1e12")
    return result


def format_number(value: Decimal | int) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


@dataclass(frozen=True)
class Markup:
    function: str
    source: str
    start: int
    end: int
    line: int
    operator: str | None = None
    threshold: Decimal | None = None
    scale: tuple[Decimal, Decimal, Decimal, Decimal] | None = None

    def matches(self, value: int) -> bool:
        if self.operator == "any":
            return True
        number = Decimal(value)
        return {"=": number == self.threshold, "<": number < self.threshold,
                ">": number > self.threshold, "<=": number <= self.threshold,
                ">=": number >= self.threshold}[self.operator]

    def resolve(self, value: int) -> str:
        if self.scale is None:
            return str(value)
        cmin, cmax, omin, omax = self.scale
        with localcontext() as context:
            context.prec = 16
            result = omin + (Decimal(value) - cmin) * (omax - omin) / (cmax - cmin)
        return format_number(result)


@dataclass(frozen=True)
class Block:
    index: int
    text: str
    line: int
    markups: tuple[Markup, ...]
    has_code: bool

    def render(self, values: dict[str, int], missing: str = "empty") -> tuple[str, tuple[str, ...]]:
        pieces, cursor, absent = [], 0, []
        for markup in self.markups:
            pieces.append(self.text[cursor:markup.start])
            if markup.function == "map":
                if markup.source in values:
                    pieces.append(markup.resolve(values[markup.source]))
                else:
                    absent.append(markup.source)
                    pieces.append("0" if missing == "zero" else "")
            cursor = markup.end
        pieces.append(self.text[cursor:])
        return "".join(pieces).strip(), tuple(dict.fromkeys(absent))


@dataclass(frozen=True)
class Score:
    blocks: tuple[Block, ...]
    name: str = "<score>"


def _parse_markup(body: str, start: int, end: int, line: int, name: str) -> Markup:
    try:
        fields = [part.strip() for part in body.split(",")]
        function = _FUNCTIONS.get(fields[0].lower())
        if function is None:
            raise ValueError(f"Unknown markup function {fields[0]!r}")
        if len(fields) < 2:
            raise ValueError("Markup needs a MIDI source")
        source = canonical_source(fields[1])
        if function == "map":
            if len(fields) not in (2, 6):
                raise ValueError("map expects source, or source,cMin,cMax,oMin,oMax")
            scale = tuple(_number(x) for x in fields[2:]) if len(fields) == 6 else None
            if scale and scale[0] == scale[1]:
                raise ValueError("map input range must have different endpoints")
            return Markup(function, source, start, end, line, scale=scale)
        if len(fields) not in (3, 4):
            raise ValueError("Trigger expects source,operator,value (or source,any)")
        operator = fields[2]
        if operator == "any":
            if len(fields) != 3:
                raise ValueError("any takes no comparison value")
            threshold = None
        else:
            if operator not in ("=", "<", ">", "<=", ">=") or len(fields) != 4:
                raise ValueError("Expected =, <, >, <=, >= with a value, or any without one")
            threshold = _number(fields[3])
        return Markup(function, source, start, end, line, operator, threshold)
    except ValueError as error:
        raise ScoreError(f"{name}:{line}: {error}") from None


def _scan(text: str, name: str) -> tuple[list[Markup], str, set[int]]:
    """Mask comments, find annotations (also inside strings), and safe block breaks.

    This is a small lexical scanner, not a Haskell parser. It preserves quoted
    strings, string gaps, nested ordinary block comments and explicit GHCi blocks.
    """
    masked = list(text)
    markups = []
    safe_starts = {0}
    state, depth, i, line = "normal", 0, 0, 1
    while i < len(text):
        char = text[i]
        if state in ("normal", "string") and (match := _SPECIAL.match(text, i)):
            end = text.find("-}", match.end())
            if end < 0:
                raise ScoreError(f"{name}:{line}: Unterminated TidalBind markup")
            end += 2
            markups.append(_parse_markup(text[match.end():end - 2], i, end, line, name))
            for position in range(i, end):
                if text[position] != "\n":
                    masked[position] = " "
            line += text[i:end].count("\n")
            i = end
            continue
        if state == "block":
            if text.startswith("{-", i):
                depth += 1
            if text.startswith("-}", i):
                depth -= 1
                masked[i:i + 2] = "  "
                i += 2
                if depth == 0:
                    state = "normal"
                continue
            if char != "\n":
                masked[i] = " "
        elif state == "line":
            if char == "\n":
                state = "normal"
            else:
                masked[i] = " "
        elif state == "string":
            if char == '"':
                state = "normal"
            elif char == "\\" and i + 1 < len(text) and text[i + 1] in ('"', "\\"):
                i += 2
                continue
        elif text.startswith("--", i):
            state = "line"
            masked[i] = " "
        elif text.startswith("{-", i):
            state, depth = "block", 1
            masked[i:i + 2] = "  "
            i += 2
            continue
        elif char == '"':
            state = "string"
        elif char == "'":
            # Character literals; apostrophes in Haskell identifiers remain intact.
            match = re.match(r"'(?:\\[^\n]|[^'\\\n])'", text[i:])
            if match:
                i += len(match[0])
                continue
        if char == "\n":
            line += 1
            if state == "normal":
                safe_starts.add(i + 1)
        i += 1
    if state == "block":
        raise ScoreError(f"{name}:{line}: Unterminated Haskell block comment")
    return markups, "".join(masked), safe_starts


def parse_score(text: str, name: str = "<score>") -> Score:
    text = text.replace("\r\n", "\n").replace("\r", "\n").removeprefix("\ufeff")
    markups, masked, safe_starts = _scan(text, name)
    segments, start, offset, explicit = [], 0, 0, False
    for line in text.splitlines(keepends=True):
        if offset in safe_starts:
            if line.strip() == ":{":
                if explicit:
                    raise ScoreError(f"{name}: Nested GHCi block")
                explicit = True
            elif line.strip() == ":}":
                if not explicit:
                    raise ScoreError(f"{name}: Unmatched :}}")
                explicit = False
            elif not line.strip() and not explicit:
                segments.append((start, offset))
                start = offset + len(line)
        offset += len(line)
    if explicit:
        raise ScoreError(f"{name}: Unclosed :{{ block")
    segments.append((start, len(text)))
    blocks = []
    for start, end in segments:
        active = [m for m in markups if start <= m.start < end]
        has_code = bool(re.sub(r"(?m)^\s*:[{}]\s*$", "", masked[start:end]).strip())
        if not has_code and not active:
            continue
        adjusted = tuple(Markup(m.function, m.source, m.start - start, m.end - start,
                                m.line, m.operator, m.threshold, m.scale) for m in active)
        blocks.append(Block(len(blocks), text[start:end].rstrip(), text[:start].count("\n") + 1,
                            adjusted, has_code))
    if not blocks:
        raise ScoreError(f"{name}: No executable code or TidalBind bindings found")
    return Score(tuple(blocks), name)


def load_score(path: str | Path) -> Score:
    path = Path(path)
    return parse_score(path.read_text(encoding="utf-8-sig"), str(path))
