"""Convert mido messages to the semantic sources used by a score."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MidiState:
    values: dict[str, int] = field(default_factory=dict)

    def update(self, message: Any) -> dict[str, int]:
        changes: dict[str, int] = {}
        kind = message.type
        if kind == "control_change":
            changes[f"cc{message.control}"] = message.value
        elif kind == "note_on" and message.velocity > 0:
            changes = {"noteOn": message.note, "velocity": message.velocity,
                       f"note{message.note}": message.velocity}
        elif kind == "note_off" or (kind == "note_on" and message.velocity == 0):
            # A key's noteN source becomes zero on release, irrespective of
            # release velocity. Thus `note36, >, 0` only triggers on a press.
            changes = {"noteOff": message.note, f"note{message.note}": 0}
        elif kind == "pitchwheel":
            changes["pitchBend"] = message.pitch
        self.values.update(changes)
        return changes
