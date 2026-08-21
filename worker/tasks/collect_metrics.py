"""Stage 8 (Phase 3) - pull post performance from official insights APIs.

Snapshot schedule once this is live: 5m, 15m, 30m, 1h, 3h, 6h, 12h, 24h, 48h
after posting. One row per pull in `metric_snapshots` - the time series is what
Phase 4 learns from, so never overwrite, always append.

Only official insights APIs. Scraping the app UI is what gets accounts flagged.
"""

from __future__ import annotations

SNAPSHOT_SCHEDULE_S = (
    5 * 60,
    15 * 60,
    30 * 60,
    60 * 60,
    3 * 60 * 60,
    6 * 60 * 60,
    12 * 60 * 60,
    24 * 60 * 60,
    48 * 60 * 60,
)

PHASE_3_MESSAGE = "Metric collection is Phase 3 and needs approved platform apps first."


def run(post_id: int) -> None:
    raise NotImplementedError(PHASE_3_MESSAGE)
