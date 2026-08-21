"""Stage 3 - transcript -> candidate clip windows (the core AI step)."""

from __future__ import annotations

import logging

from core.config import settings
from core.db import session_scope
from core.llm import detect_moments
from core.models import Candidate as CandidateRow
from core.models import Source, Transcript
from core.selection import Candidate, Word, chunk_words, clamp_to_words, dedupe
from worker.queue import enqueue

log = logging.getLogger(__name__)


def detect(words: list[Word], niche: str | None = None) -> list[Candidate]:
    """Chunk the transcript, ask Claude per window, clamp and dedupe.

    A window that errors is logged and skipped: one bad response should not
    cost the whole source.
    """
    niche = niche or settings.default_niche
    windows = chunk_words(words)
    log.info("detecting moments across %d windows", len(windows))

    found: list[Candidate] = []
    for window in windows:
        try:
            raw = detect_moments(niche, window)
        except Exception as exc:  # noqa: BLE001 - one window must not kill the run
            log.warning("window %d failed: %s", window.index, exc)
            continue
        for candidate in raw:
            clamped = clamp_to_words(candidate, words)
            if clamped is not None:
                found.append(clamped)
        log.info("window %d -> %d candidates", window.index, len(raw))

    deduped = dedupe(found)
    log.info("%d candidates after dedupe (from %d raw)", len(deduped), len(found))
    return deduped


def run(source_id: int) -> int:
    from worker.tasks.rank import run as rank_run

    with session_scope() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise ValueError(f"no source {source_id}")
        transcript = session.query(Transcript).filter(Transcript.source_id == source_id).one()
        words = list(transcript.words)
        niche = source.niche.name if source.niche else settings.default_niche
        source.status = "detecting"

    candidates = detect(words, niche)

    with session_scope() as session:
        for candidate in candidates:
            session.add(
                CandidateRow(
                    source_id=source_id,
                    start_s=candidate.start_s,
                    end_s=candidate.end_s,
                    hook_score=candidate.hook_score,
                    emotion=candidate.emotion,
                    payoff_score=candidate.payoff_score,
                    context_ok=candidate.context_ok,
                    novelty=candidate.novelty,
                    rationale={"one_line_reason": candidate.one_line_reason},
                    status="new",
                )
            )
        session.get(Source, source_id).status = "ranking"

    enqueue("rank", rank_run, source_id)
    return source_id
