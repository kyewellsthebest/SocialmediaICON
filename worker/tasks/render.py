"""Stage 5 - cut, reframe to 9:16, burn karaoke captions, write metadata.

The heaviest stage and the one that decides whether Phase 1 passes: a clip that
is framed wrong or captioned badly is unpostable no matter how good the moment
selection was.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.captions import write_ass
from core.config import settings
from core.db import session_scope
from core.ffmpeg_ops import render_clip
from core.llm import write_metadata
from core.models import Candidate as CandidateRow
from core.models import Clip, Source, Transcript
from core.selection import Candidate, Word, text_between, words_between
from core.storage import get_storage

log = logging.getLogger(__name__)


@dataclass
class RenderedClip:
    path: Path
    start_s: float
    end_s: float
    duration_s: float
    title: str
    caption: str
    hashtags: list[str]
    transcript: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "start_s": round(self.start_s, 2),
            "end_s": round(self.end_s, 2),
            "duration_s": round(self.duration_s, 2),
            "title": self.title,
            "caption": self.caption,
            "hashtags": self.hashtags,
            "transcript": self.transcript,
        }


def render_candidate(
    video_path: Path | str,
    words: list[Word],
    candidate: Candidate,
    out_path: Path | str,
    work_dir: Path | str,
    niche: str | None = None,
    with_metadata: bool = True,
) -> RenderedClip:
    niche = niche or settings.default_niche
    out_path = Path(out_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Caption timings are relative to the cut, not the source.
    clip_words = words_between(words, candidate.start_s, candidate.end_s)
    ass_path = write_ass(clip_words, work_dir / f"{out_path.stem}.ass")

    render_clip(video_path, out_path, candidate.start_s, candidate.end_s, ass_path=ass_path)

    clip_text = text_between(words, candidate.start_s, candidate.end_s)
    metadata: dict[str, Any] = {"title": "", "caption": "", "hashtags": []}
    if with_metadata:
        try:
            metadata = write_metadata(niche, clip_text)
        except Exception as exc:  # noqa: BLE001 - a rendered clip beats no clip
            log.warning("metadata generation failed: %s", exc)

    return RenderedClip(
        path=out_path,
        start_s=candidate.start_s,
        end_s=candidate.end_s,
        duration_s=candidate.duration_s,
        title=metadata["title"],
        caption=metadata["caption"],
        hashtags=metadata["hashtags"],
        transcript=clip_text,
    )


def run(candidate_id: int) -> int:
    """RQ entrypoint: render one selected candidate into a clip row."""
    from worker.tasks.common import local_source_path, work_dir_for

    with session_scope() as session:
        row = session.get(CandidateRow, candidate_id)
        if row is None:
            raise ValueError(f"no candidate {candidate_id}")
        source = session.get(Source, row.source_id)
        transcript = (
            session.query(Transcript).filter(Transcript.source_id == row.source_id).one()
        )
        words = list(transcript.words)
        niche = source.niche.name if source.niche else settings.default_niche
        source_id = source.id
        video_path = local_source_path(source)
        candidate = Candidate(
            start_s=row.start_s,
            end_s=row.end_s,
            hook_score=row.hook_score or 0.0,
            emotion=row.emotion or "none",
            payoff_score=row.payoff_score or 0.0,
            context_ok=row.context_ok,
            novelty=row.novelty or 0.0,
            predicted_score=row.predicted_score,
            rationale=row.rationale or {},
        )

    work_dir = work_dir_for(source_id)
    out_path = work_dir / f"clip-{candidate_id}.mp4"
    rendered = render_candidate(video_path, words, candidate, out_path, work_dir, niche)

    key = f"clips/{source_id}/{out_path.name}"
    get_storage().put_file(rendered.path, key)

    with session_scope() as session:
        clip = Clip(
            candidate_id=candidate_id,
            storage_key=key,
            title=rendered.title,
            hashtags=rendered.hashtags,
            duration_s=rendered.duration_s,
            status="queued",  # Phase 2: lands in the review queue
        )
        session.add(clip)
        session.get(CandidateRow, candidate_id).status = "rendered"
        session.flush()
        clip_id = clip.id

    log.info("rendered clip %s -> %s", clip_id, key)
    return clip_id
