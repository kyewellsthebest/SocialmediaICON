#!/usr/bin/env python3
"""Phase 1 CLI: one video, end to end, top N captioned 9:16 clips in ./out.

    python scripts/run_pipeline.py https://www.youtube.com/watch?v=...

No dashboard, no publishing, no Postgres required. The transcript is cached per
source so re-running while tuning the detect/rank prompts does not pay the
transcription bill twice.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings  # noqa: E402
from core.ffmpeg_ops import require_binaries  # noqa: E402
from core.selection import Candidate  # noqa: E402
from worker.tasks.detect_moments import detect  # noqa: E402
from worker.tasks.ingest import check_license, download_source  # noqa: E402
from worker.tasks.rank import rank  # noqa: E402
from worker.tasks.render import render_candidate  # noqa: E402

log = logging.getLogger("run_pipeline")


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "clip"


def preflight(args: argparse.Namespace) -> None:
    require_binaries()
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set (see .env.example)")
    if not args.transcript and not settings.transcription_key:
        provider = settings.transcribe_provider
        var = "ASSEMBLYAI_API_KEY" if provider == "assemblyai" else "DEEPGRAM_API_KEY"
        raise SystemExit(f"{var} is not set (TRANSCRIBE_PROVIDER={provider})")


def get_video(args: argparse.Namespace, work_dir: Path) -> tuple[Path, str]:
    """Returns (local video path, title)."""
    if args.file:
        path = Path(args.file).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"no such file: {path}")
        return path, path.stem

    cached = sorted(p for p in work_dir.glob("*.mp4") if not p.name.startswith("clip-"))
    if cached and not args.redownload:
        log.info("using cached download %s", cached[0].name)
        return cached[0], cached[0].stem

    log.info("downloading %s", args.url)
    download = download_source(args.url, work_dir)
    log.info("downloaded %s (%.0fs)", download.title, download.duration_s)
    return download.path, download.title


def get_transcript(args: argparse.Namespace, video_path: Path, work_dir: Path) -> dict:
    from worker.tasks.transcribe import transcribe_file

    cache = Path(args.transcript) if args.transcript else work_dir / "transcript.json"
    if cache.exists() and not args.retranscribe:
        log.info("using cached transcript %s", cache)
        return json.loads(cache.read_text())

    log.info("transcribing (provider=%s)", settings.transcribe_provider)
    result = transcribe_file(video_path, work_dir)
    cache.write_text(json.dumps(result))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("url", nargs="?", help="source video URL (yt-dlp compatible)")
    parser.add_argument("--file", help="use a local video file instead of downloading")
    parser.add_argument("--license", default="own", help="own|licensed|campaign|permitted|none")
    parser.add_argument("--niche", default=settings.default_niche)
    parser.add_argument("-n", "--top-n", type=int, default=settings.top_n_clips)
    parser.add_argument("--out", default="out", help="output directory (default: ./out)")
    parser.add_argument("--work", default=None, help="working directory (default: .work/<slug>)")
    parser.add_argument("--min-s", type=float, default=settings.min_clip_s)
    parser.add_argument("--max-s", type=float, default=settings.max_clip_s)
    parser.add_argument("--transcript", help="path to an existing transcript json")
    parser.add_argument("--retranscribe", action="store_true", help="ignore the cached transcript")
    parser.add_argument("--redownload", action="store_true", help="ignore the cached download")
    parser.add_argument("--no-metadata", action="store_true", help="skip title/hashtag generation")
    parser.add_argument("--keep-work", action="store_true", help="keep intermediate files")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.url and not args.file:
        parser.error("give a URL or --file")

    check_license(args.license)
    settings.min_clip_s = args.min_s
    settings.max_clip_s = args.max_s

    preflight(args)

    source_slug = slugify(Path(args.file).stem if args.file else args.url, limit=60)
    work_dir = Path(args.work) if args.work else Path(settings.work_dir) / source_slug
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    video_path, title = get_video(args, work_dir)

    transcript = get_transcript(args, video_path, work_dir)
    words = transcript["words"]
    if not words:
        raise SystemExit("transcript came back with no words - nothing to clip")
    log.info("transcript: %d words, %.1f minutes", len(words), float(words[-1]["end"]) / 60)

    candidates: list[Candidate] = detect(words, args.niche)
    if not candidates:
        raise SystemExit("no candidate moments found - check the detect prompt or the source")

    winners = rank(words, candidates, args.niche, args.top_n)
    log.info("rendering %d clips", len(winners))

    results = []
    for i, candidate in enumerate(winners, start=1):
        out_path = out_dir / f"{i:02d}-{slugify(title)}.mp4"
        rendered = render_candidate(
            video_path,
            words,
            candidate,
            out_path,
            work_dir,
            niche=args.niche,
            with_metadata=not args.no_metadata,
        )
        payload = rendered.to_dict()
        payload["predicted_score"] = candidate.predicted_score
        payload["rationale"] = candidate.rationale
        payload["one_line_reason"] = candidate.one_line_reason
        results.append(payload)

        sidecar = out_path.with_suffix(".txt")
        sidecar.write_text(
            f"{rendered.title}\n\n{rendered.caption}\n\n{' '.join(rendered.hashtags)}\n\n"
            f"source: {args.url or args.file}\n"
            f"segment: {rendered.start_s:.1f}s - {rendered.end_s:.1f}s "
            f"({rendered.duration_s:.0f}s)\n"
            f"why: {candidate.one_line_reason}\n"
        )
        log.info("wrote %s", out_path)

    (out_dir / "clips.json").write_text(json.dumps(results, indent=2))

    if not args.keep_work:
        for leftover in work_dir.glob("*.ass"):
            leftover.unlink(missing_ok=True)

    elapsed = time.time() - started
    print("\n" + "=" * 72)
    print(f"{len(results)} clips in {elapsed / 60:.1f} min  ->  {out_dir.resolve()}")
    print("=" * 72)
    for i, clip in enumerate(results, start=1):
        score = clip["predicted_score"]
        print(
            f"{i:>2}. {clip['start_s']:>7.1f}s -> {clip['end_s']:>7.1f}s "
            f"({clip['duration_s']:>4.0f}s)  score={score if score is None else round(score)}"
        )
        print(f"    {clip['title'] or '(no title)'}")
        print(f"    {' '.join(clip['hashtags'])}")
        print(f"    {Path(clip['path']).name}")
    print(
        "\nGo/no-go: watch all of them. If you would not post at least 3 of these "
        "without editing, fix the detect/rank prompts in core/llm.py before building "
        "anything else (spec section 7, Phase 1)."
    )
    if shutil.disk_usage(work_dir).free < 2 * 1024**3:
        print("\nwarning: under 2 GB free on the working disk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
