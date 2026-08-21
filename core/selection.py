"""Pure helpers shared by detect_moments / rank / render.

Deliberately free of network and DB calls: this is the logic most likely to be
wrong and it is the logic that is cheapest to unit test.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from core.config import settings

Word = dict[str, Any]  # {"w": str, "start": float, "end": float}


@dataclass
class Window:
    """A slice of transcript handed to the model in one request."""

    index: int
    start_s: float
    end_s: float
    words: list[Word]


@dataclass
class Candidate:
    start_s: float
    end_s: float
    hook_score: float = 0.0
    emotion: str = "none"
    payoff_score: float = 0.0
    context_ok: bool = True
    novelty: float = 0.0
    one_line_reason: str = ""
    predicted_score: float | None = None
    rationale: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def sort_score(self) -> float:
        """Ranking key: the model's predicted score once ranked, else a
        provisional blend of the detection scores."""
        if self.predicted_score is not None:
            return self.predicted_score
        return 0.45 * self.hook_score + 0.35 * self.payoff_score + 0.20 * self.novelty

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def chunk_words(
    words: list[Word], window_s: float | None = None, overlap_s: float = 20.0
) -> list[Window]:
    """Split a word list into overlapping time windows.

    The overlap exists so a moment straddling a window boundary is still seen
    whole by at least one request; duplicates are removed later by `dedupe`.
    """
    if not words:
        return []
    window_s = window_s or settings.window_minutes * 60
    if overlap_s >= window_s:
        raise ValueError("overlap_s must be smaller than window_s")

    total_end = float(words[-1]["end"])
    windows: list[Window] = []
    start = float(words[0]["start"])
    index = 0
    while start < total_end:
        end = start + window_s
        chunk = [w for w in words if float(w["start"]) >= start and float(w["start"]) < end]
        if chunk:
            windows.append(
                Window(
                    index=index,
                    start_s=float(chunk[0]["start"]),
                    end_s=float(chunk[-1]["end"]),
                    words=chunk,
                )
            )
            index += 1
        start = end - overlap_s
    return windows


def format_words(words: list[Word], words_per_line: int = 12) -> str:
    """Render words as timestamped lines for the prompt.

    Per-word timestamps would triple the prompt size for no benefit: candidate
    boundaries are snapped back onto real word boundaries by `clamp_to_words`
    anyway, so line-level granularity is enough for the model to point at.
    """
    lines: list[str] = []
    for i in range(0, len(words), words_per_line):
        group = words[i : i + words_per_line]
        text = " ".join(str(w["w"]).strip() for w in group if str(w["w"]).strip())
        if text:
            lines.append(f"[{float(group[0]['start']):.1f}] {text}")
    return "\n".join(lines)


def clamp_to_words(
    candidate: Candidate,
    words: list[Word],
    min_s: float | None = None,
    max_s: float | None = None,
) -> Candidate | None:
    """Snap a model-proposed window onto real word boundaries.

    Returns None when nothing usable is left (empty window, or a window that
    cannot be brought inside the min/max duration bounds).
    """
    if not words:
        return None
    min_s = settings.min_clip_s if min_s is None else min_s
    max_s = settings.max_clip_s if max_s is None else max_s

    start, end = float(candidate.start_s), float(candidate.end_s)
    if end <= start:
        return None

    inside = [w for w in words if float(w["end"]) > start and float(w["start"]) < end]
    if not inside:
        return None

    new_start = float(inside[0]["start"])
    new_end = float(inside[-1]["end"])

    if new_end - new_start > max_s:
        # Too long: keep the opening, since the hook is what earns the view.
        cut = [w for w in inside if float(w["end"]) <= new_start + max_s]
        if cut:
            new_end = float(cut[-1]["end"])
        else:
            new_end = new_start + max_s

    if new_end - new_start < min_s:
        # Too short: extend forwards with following words, then backwards.
        after = [w for w in words if float(w["start"]) >= new_end]
        for w in after:
            new_end = float(w["end"])
            if new_end - new_start >= min_s:
                break
        if new_end - new_start < min_s:
            before = [w for w in words if float(w["end"]) <= new_start]
            for w in reversed(before):
                new_start = float(w["start"])
                if new_end - new_start >= min_s:
                    break
        if new_end - new_start < min_s:
            return None

    clamped = Candidate(**{**candidate.to_dict(), "start_s": new_start, "end_s": new_end})
    return clamped


def overlap_ratio(a: Candidate, b: Candidate) -> float:
    """Intersection over the shorter of the two windows."""
    overlap = min(a.end_s, b.end_s) - max(a.start_s, b.start_s)
    if overlap <= 0:
        return 0.0
    shortest = min(a.duration_s, b.duration_s)
    return overlap / shortest if shortest > 0 else 0.0


def dedupe(candidates: list[Candidate], max_overlap: float = 0.5) -> list[Candidate]:
    """Greedy non-max suppression over time windows, best score first."""
    kept: list[Candidate] = []
    for cand in sorted(candidates, key=lambda c: c.sort_score, reverse=True):
        if any(overlap_ratio(cand, k) > max_overlap for k in kept):
            continue
        kept.append(cand)
    return sorted(kept, key=lambda c: c.start_s)


def select_top(candidates: list[Candidate], n: int | None = None) -> list[Candidate]:
    n = settings.top_n_clips if n is None else n
    ranked = sorted(candidates, key=lambda c: c.sort_score, reverse=True)[:n]
    return sorted(ranked, key=lambda c: c.start_s)


def text_between(words: list[Word], start_s: float, end_s: float) -> str:
    """Plain transcript text for a time range."""
    return " ".join(
        str(w["w"]).strip()
        for w in words
        if float(w["end"]) > start_s and float(w["start"]) < end_s and str(w["w"]).strip()
    )


def words_between(words: list[Word], start_s: float, end_s: float) -> list[Word]:
    """Words inside a time range, re-based so the first word starts at 0."""
    inside = [w for w in words if float(w["end"]) > start_s and float(w["start"]) < end_s]
    return [
        {
            "w": w["w"],
            "start": max(0.0, float(w["start"]) - start_s),
            "end": max(0.0, min(float(w["end"]), end_s) - start_s),
        }
        for w in inside
    ]
