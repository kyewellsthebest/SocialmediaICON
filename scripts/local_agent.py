#!/usr/bin/env python3
"""Download sources on this machine and hand them to the deployed pipeline.

    python scripts/local_agent.py --url https://your-app.up.railway.app --token YOUR_TOKEN

Why this exists: YouTube challenges datacenter IP ranges, and every cloud host
sits in one. Cookies, player-client rotation and cheap shared proxies all fail
against it sooner or later, because YouTube is the most scraped site there is
and it spends real money on this.

Your home connection is not a datacenter, and downloads from it work. So this
runs where you are, watches the deployment for sources waiting on a file,
fetches each one with yt-dlp, and posts it back. Everything else - transcribe,
detect, rank, render, publish - stays in the cloud and is unchanged.

Set INGEST_MODE=agent on the deployment first, or the worker will keep trying
to download and keep failing.

It does not sign in to anything. Downloads are anonymous, so there is no
account to lose - the worst case is YouTube briefly rate-limiting the
connection, which clears on its own. It paces itself to stay well inside what
ordinary viewing looks like, because that connection is shared with everyone
else in the building.

Leave it running in a terminal. Ctrl-C stops it; nothing is lost, a source it
did not reach stays queued for next time.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

log = logging.getLogger("agent")

POLL_S = 30

# Pacing. A home connection watching a few videos an hour is unremarkable;
# forty back-to-back downloads in ten minutes is not, and the thing that would
# notice is shared with everyone else in the house. These defaults keep the
# traffic inside what ordinary viewing looks like.
DEFAULT_MIN_GAP_S = 90
DEFAULT_MAX_PER_HOUR = 12


class Agent:
    def __init__(
        self,
        base_url: str,
        token: str,
        max_height: int,
        keep: bool,
        min_gap_s: float = DEFAULT_MIN_GAP_S,
        max_per_hour: int = DEFAULT_MAX_PER_HOUR,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.keep = keep
        self.max_height = max_height
        self.min_gap_s = min_gap_s
        self.max_per_hour = max_per_hour
        self.finished_at: list[float] = []
        self.client = httpx.Client(
            timeout=httpx.Timeout(900.0, connect=30.0),
            headers={"X-Dashboard-Token": token} if token else {},
        )
        self.running = True

    def stop(self, *_a) -> None:
        log.info("finishing the current source, then stopping")
        self.running = False

    # ------------------------------------------------------------------ api

    def waiting(self) -> list[dict]:
        """Sources registered but with no video yet."""
        response = self.client.get(
            f"{self.base}/api/sources", params={"status": "registered", "limit": 25}
        )
        response.raise_for_status()
        return [s for s in response.json() if s.get("kind") != "upload"]

    def send(self, source_id: int, path: Path) -> None:
        with path.open("rb") as fh:
            response = self.client.post(
                f"{self.base}/api/sources/{source_id}/file",
                files={"file": (path.name, fh, "video/mp4")},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")

    # ------------------------------------------------------------- download

    def fetch(self, url: str, dest_dir: Path) -> Path:
        import yt_dlp

        options = {
            "format": (f"bv*[height<={self.max_height}]+ba/b[height<={self.max_height}]/bv*+ba/b"),
            "merge_output_format": "mp4",
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))

        if not path.exists():
            matches = sorted(dest_dir.glob(f"{info['id']}.*"))
            if not matches:
                raise FileNotFoundError(f"yt-dlp reported success but produced no file for {url}")
            path = matches[0]
        return path

    # --------------------------------------------------------------- pacing

    def _recent(self, now: float) -> int:
        self.finished_at = [t for t in self.finished_at if now - t < 3600]
        return len(self.finished_at)

    def wait_for_slot(self, sleep=time.sleep) -> bool:
        """Hold until another download would look unremarkable.

        Returns False if the hourly budget is spent, in which case the caller
        should stop rather than sleep out the hour holding the queue.
        """
        now = time.monotonic()
        if self._recent(now) >= self.max_per_hour:
            log.info(
                "%d downloads in the last hour is the cap - pausing until it frees up",
                self.max_per_hour,
            )
            return False
        if self.finished_at:
            gap = now - self.finished_at[-1]
            if gap < self.min_gap_s:
                sleep(self.min_gap_s - gap)
        return True

    # ----------------------------------------------------------------- loop

    def handle(self, source: dict) -> None:
        source_id = source["id"]
        url = source["url"]
        log.info("source %s: downloading %s", source_id, url)

        tmp_dir = Path(tempfile.mkdtemp(prefix=f"agent-{source_id}-"))
        try:
            path = self.fetch(url, tmp_dir)
            size_mb = path.stat().st_size / 1_000_000
            log.info("source %s: got %.0f MB, uploading", source_id, size_mb)
            self.send(source_id, path)
            log.info("source %s: handed over, the pipeline has it", source_id)
            self.finished_at.append(time.monotonic())
        finally:
            if not self.keep:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def tick(self) -> int:
        try:
            pending = self.waiting()
        except httpx.HTTPError as exc:
            log.warning("could not reach the deployment: %s", exc)
            return 0

        done = 0
        for source in pending:
            if not self.running:
                break
            if not self.wait_for_slot():
                break
            try:
                self.handle(source)
                done += 1
            except Exception as exc:  # noqa: BLE001 - one bad source must not stop the agent
                log.error("source %s failed: %s", source["id"], exc)
        return done

    def loop(self) -> int:
        log.info("watching %s for sources to download", self.base)
        while self.running:
            handled = self.tick()
            if not self.running:
                break
            if handled == 0:
                time.sleep(POLL_S)
        log.info("stopped")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("CLIP_ENGINE_URL", ""),
        help="your deployment, e.g. https://your-app.up.railway.app",
    )
    parser.add_argument(
        "--token", default=os.environ.get("DASHBOARD_TOKEN", ""), help="DASHBOARD_TOKEN"
    )
    parser.add_argument("--max-height", type=int, default=1080)
    parser.add_argument(
        "--min-gap",
        type=float,
        default=DEFAULT_MIN_GAP_S,
        help="seconds to leave between downloads (default 90)",
    )
    parser.add_argument(
        "--max-per-hour",
        type=int,
        default=DEFAULT_MAX_PER_HOUR,
        help="most downloads in any rolling hour (default 12)",
    )
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--keep", action="store_true", help="leave downloaded files on disk")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.url:
        print("Pass --url (or set CLIP_ENGINE_URL) - the address of your deployment.")
        return 2

    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        print("yt-dlp is not installed here. Run:  pip install yt-dlp httpx")
        return 2

    agent = Agent(
        args.url,
        args.token,
        args.max_height,
        args.keep,
        min_gap_s=args.min_gap,
        max_per_hour=args.max_per_hour,
    )
    signal.signal(signal.SIGINT, agent.stop)
    signal.signal(signal.SIGTERM, agent.stop)

    if args.once:
        handled = agent.tick()
        print(f"{handled} source(s) handled.")
        return 0
    return agent.loop()


if __name__ == "__main__":
    raise SystemExit(main())
