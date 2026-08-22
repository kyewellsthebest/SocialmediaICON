#!/usr/bin/env python3
"""Fill the database with obviously-fake data so the dashboard has something to
draw before the real pipeline has run.

    python scripts/seed_demo.py          # insert
    python scripts/seed_demo.py --clear  # remove it again

Every row it creates is titled "[demo]" so you can tell it apart from real work.
Never run this against a database you care about.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db import session_scope  # noqa: E402
from core.models import (  # noqa: E402
    Account,
    Candidate,
    Clip,
    MetricSnapshot,
    Niche,
    Post,
    Source,
    TrackedSnapshot,
    TrackedVideo,
)
from core.scoring import (  # noqa: E402
    hot_segments,
    like_rate,
    peak_heat,
    score_video,
    views_per_hour,
)

DEMO = "[demo]"
NOW = datetime.now(UTC)
rng = random.Random(7)


def fake_heatmap(seed: int, duration: float) -> list[dict[str, float]]:
    """A plausible replay curve: a couple of humps plus noise."""
    local = random.Random(seed)
    peaks = [local.uniform(0.15, 0.85) for _ in range(local.randint(1, 3))]
    markers = []
    for i in range(100):
        position = i / 100
        value = 0.18 + local.uniform(0, 0.08)
        for peak in peaks:
            value += 0.75 * math.exp(-((position - peak) ** 2) / 0.0016)
        markers.append(
            {
                "start": round(position * duration, 2),
                "end": round((position + 0.01) * duration, 2),
                "value": round(min(1.0, value), 4),
            }
        )
    return markers


def clear() -> None:
    with session_scope() as session:
        for model, column in (
            (Post, None),
            (Clip, None),
            (Candidate, None),
            (Source, Source.title),
            (TrackedVideo, TrackedVideo.title),
            (Account, Account.handle),
        ):
            if column is None:
                continue
            session.query(model).filter(column.like(f"{DEMO}%")).delete(synchronize_session=False)
        # Orphans from the cascades above
        session.query(TrackedSnapshot).filter(
            ~TrackedSnapshot.tracked_video_id.in_(session.query(TrackedVideo.id))
        ).delete(synchronize_session=False)
    print("demo rows removed")


def seed() -> None:
    with session_scope() as session:
        niche = session.query(Niche).filter(Niche.name == "demo-niche").one_or_none()
        if niche is None:
            niche = Niche(
                name="demo-niche", config={"keywords": ["demo keyword"], "cadence_per_day": 10}
            )
            session.add(niche)
            session.flush()

        for platform, handle in (("youtube", "@demo"), ("tiktok", "@demo")):
            if not session.query(Account).filter(Account.handle == f"{DEMO} {handle}").first():
                session.add(
                    Account(niche_id=niche.id, platform=platform, handle=f"{DEMO} {handle}")
                )

        # --- trending videos -------------------------------------------------
        for i in range(8):
            external = f"demo{i:03d}"
            if session.query(TrackedVideo).filter(TrackedVideo.external_id == external).first():
                continue
            duration = rng.uniform(900, 4200)
            published = NOW - timedelta(hours=rng.uniform(6, 480))
            views = int(rng.uniform(20_000, 2_500_000))
            likes = int(views * rng.uniform(0.02, 0.09))
            heat = fake_heatmap(i, duration)
            velocity = views_per_hour(views, published)
            ratio = like_rate(likes, views)

            video = TrackedVideo(
                niche_id=niche.id,
                platform="youtube",
                external_id=external,
                url=f"https://www.youtube.com/watch?v={external}",
                title=f"{DEMO} source video {i + 1}",
                channel_title=f"{DEMO} channel {(i % 3) + 1}",
                published_at=published,
                duration_s=duration,
                views=views,
                likes=likes,
                comments=int(likes * rng.uniform(0.02, 0.12)),
                velocity_vph=velocity,
                like_rate=ratio,
                heatmap=heat,
                hot_segments=hot_segments(heat),
                score=score_video(velocity, ratio, published, peak_heat(heat)),
                status="new" if i % 4 else "clipped",
                last_checked_at=NOW,
            )
            session.add(video)
            session.flush()

            running = int(views * 0.82)
            for hours_back in range(12, -1, -2):
                running = int(running * rng.uniform(1.01, 1.06))
                session.add(
                    TrackedSnapshot(
                        tracked_video_id=video.id,
                        captured_at=NOW - timedelta(hours=hours_back),
                        views=min(running, views),
                        likes=int(min(running, views) * ratio),
                        comments=video.comments,
                    )
                )

        # --- pipeline: source -> candidate -> clip -> post -------------------
        for i in range(3):
            title = f"{DEMO} long-form source {i + 1}"
            if session.query(Source).filter(Source.title == title).first():
                continue
            source = Source(
                niche_id=niche.id,
                url=f"https://www.youtube.com/watch?v=demosrc{i}",
                kind="youtube",
                license="campaign",
                title=title,
                duration_s=rng.uniform(1800, 3600),
                status="done",
                storage_key=f"sources/demo/{i}.mp4",
            )
            session.add(source)
            session.flush()

            for j in range(2):
                start = rng.uniform(120, 1500)
                candidate = Candidate(
                    source_id=source.id,
                    start_s=start,
                    end_s=start + rng.uniform(20, 55),
                    hook_score=rng.uniform(6, 10),
                    payoff_score=rng.uniform(6, 10),
                    novelty=rng.uniform(4, 9),
                    emotion=rng.choice(["surprise", "humour", "curiosity", "excitement"]),
                    predicted_score=rng.uniform(55, 95),
                    rationale={"hook": "demo", "payoff": "demo", "risk": "none"},
                    status="rendered",
                )
                session.add(candidate)
                session.flush()

                posted = i == 0
                clip = Clip(
                    candidate_id=candidate.id,
                    storage_key=f"clips/demo/{i}-{j}.mp4",
                    title=f"{DEMO} clip {i + 1}.{j + 1}",
                    hashtags=["#demo", "#clipengine", "#shorts"],
                    duration_s=candidate.end_s - candidate.start_s,
                    status="posted" if posted else "queued",
                )
                session.add(clip)
                session.flush()

                if not posted:
                    continue

                posted_at = NOW - timedelta(hours=rng.uniform(6, 40))
                post = Post(
                    clip_id=clip.id,
                    platform=rng.choice(["youtube", "tiktok"]),
                    platform_post_id=f"demo-post-{i}{j}",
                    platform_url="https://example.com/demo",
                    posted_at=posted_at,
                    status="posted",
                )
                session.add(post)
                session.flush()

                # Views build fast then flatten - the shape you actually see.
                ceiling = rng.uniform(4_000, 90_000)
                for mark_h in (0.08, 0.25, 0.5, 1, 3, 6, 12, 24, 48):
                    captured = posted_at + timedelta(hours=mark_h)
                    if captured > NOW:
                        break
                    progress = 1 - math.exp(-mark_h / 7)
                    views = int(ceiling * progress * rng.uniform(0.95, 1.05))
                    session.add(
                        MetricSnapshot(
                            post_id=post.id,
                            captured_at=captured,
                            views=views,
                            likes=int(views * rng.uniform(0.04, 0.1)),
                            comments=int(views * rng.uniform(0.002, 0.01)),
                            avg_watch_s=rng.uniform(8, 26),
                            completion_rate=rng.uniform(0.3, 0.8),
                        )
                    )

    print("demo data inserted - everything is prefixed [demo]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clear", action="store_true", help="remove demo rows instead")
    args = parser.parse_args()
    clear() if args.clear else seed()
