"""TidalBind: declarative MIDI interaction embedded in TidalCycles scores."""

from .engine import Performance
from .score import Score, ScoreError, load_score, parse_score

__all__ = ["Performance", "Score", "ScoreError", "load_score", "parse_score"]
__version__ = "0.1.0"
