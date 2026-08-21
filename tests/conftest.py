from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def make_words(count: int, start: float = 0.0, step: float = 0.5) -> list[dict]:
    """Evenly spaced words, one every `step` seconds."""
    return [
        {"w": f"w{i}", "start": round(start + i * step, 3), "end": round(start + (i + 1) * step, 3)}
        for i in range(count)
    ]
