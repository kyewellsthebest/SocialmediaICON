"""Stage 4 - score candidates, dedupe again, keep the top N."""

from __future__ import annotations

import logging

from core.config import settings
from core.db import session_scope
from core.llm import rank_candidates
from core.models import Candidate as CandidateRow
from core.models import Source, Transcript
from core.selection import Candidate, Word, dedupe, select_top, text_between
from worker.queue import enqueue

log = logging.getLogger(__name__)

# Ranking prompts carry a transcript per candidate, so they are batched rather
# than sent as one giant request.
BATCH_SIZE = 10


def rank(
    words: list[Word],
    candidates: list[Candidate],
    niche: str | None = None,
    top_n: int | None = None,
) -> list[Candidate]:
    niche = niche or settings.default_niche
    scored: list[Candidate] = []

    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        texts = [text_between(words, c.start_s, c.end_s) for c in batch]
        try:
            scored.extend(rank_candidates(niche, batch, texts))
        except Exception as exc:  # noqa: BLE001 - fall back to detection scores
            log.warning("ranking batch at %d failed (%s); keeping detector scores", start, exc)
            scored.extend(batch)

    # Dropping candidates the model itself said are not standalone is cheaper
    # than rendering them and rejecting them by eye.
    usable = [c for c in scored if c.context_ok] or scored
    winners = select_top(dedupe(usable), top_n)
    for winner in winners:
        log.info(
            "selected %.1fs-%.1fs score=%.1f %s",
            winner.start_s,
            winner.end_s,
            winner.sort_score,
            winner.one_line_reason[:80],
        )
    return winners


def run(source_id: int) -> int:
    from worker.tasks.render import run as render_run

    with session_scope() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise ValueError(f"no source {source_id}")
        transcript = session.query(Transcript).filter(Transcript.source_id == source_id).one()
        words = list(transcript.words)
        niche = source.niche.name if source.niche else settings.default_niche
        rows = (
            session.query(CandidateRow)
            .filter(CandidateRow.source_id == source_id, CandidateRow.status == "new")
            .order_by(CandidateRow.start_s)
            .all()
        )
        row_ids = [row.id for row in rows]
        candidates = [
            Candidate(
                start_s=row.start_s,
                end_s=row.end_s,
                hook_score=row.hook_score or 0.0,
                emotion=row.emotion or "none",
                payoff_score=row.payoff_score or 0.0,
                context_ok=row.context_ok,
                novelty=row.novelty or 0.0,
                one_line_reason=(row.rationale or {}).get("one_line_reason", ""),
            )
            for row in rows
        ]

    winners = rank(words, candidates, niche)
    winner_keys = {(round(c.start_s, 2), round(c.end_s, 2)): c for c in winners}
    selected_ids: list[int] = []

    with session_scope() as session:
        for row_id, candidate in zip(row_ids, candidates, strict=True):
            row = session.get(CandidateRow, row_id)
            key = (round(candidate.start_s, 2), round(candidate.end_s, 2))
            winner = winner_keys.get(key)
            if winner is not None:
                row.predicted_score = winner.predicted_score
                row.rationale = {**(row.rationale or {}), **winner.rationale}
                row.status = "selected"
                selected_ids.append(row_id)
            else:
                row.predicted_score = candidate.predicted_score
                row.status = "ranked"
        session.get(Source, source_id).status = "rendering"

    for candidate_id in selected_ids:
        enqueue("render", render_run, candidate_id)
    return source_id
