from __future__ import annotations

from datetime import UTC, datetime

from worker.tasks.collect_metrics import SNAPSHOT_SCHEDULE_S

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def test_schedule_is_ordered_and_covers_48_hours():
    assert list(SNAPSHOT_SCHEDULE_S) == sorted(SNAPSHOT_SCHEDULE_S)
    assert SNAPSHOT_SCHEDULE_S[0] == 5 * 60
    assert SNAPSHOT_SCHEDULE_S[-1] == 48 * 3600


def test_checkpoints_passed_matches_elapsed_time():
    """The rule due_posts() applies: a post is due when more checkpoints have
    passed than it has snapshots."""

    def passed(hours: float) -> int:
        elapsed = hours * 3600
        return sum(1 for mark in SNAPSHOT_SCHEDULE_S if elapsed >= mark)

    assert passed(0.01) == 0
    assert passed(0.2) == 1      # 5m
    assert passed(1.1) == 4      # 5m, 15m, 30m, 1h
    assert passed(25) == 8
    assert passed(72) == 9       # everything, and no more
