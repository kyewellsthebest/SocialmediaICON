"""The crop has to follow the subject without twitching.

Both halves matter and they pull against each other, so every test here is
really asking the same question two ways: did it stay on the subject, and did
it hold still while doing it.
"""

from __future__ import annotations

import math
import random

import pytest

from core import reframe


def blob(centre: float, *, spread: float = 2.5, weight: float = 900.0) -> list[float]:
    """One patch of motion at ``centre`` (0..1), as a row of column energies."""
    x0 = centre * (reframe.PROBE_W - 1)
    return [
        weight * math.exp(-((x - x0) ** 2) / (2 * spread**2))
        for x in range(reframe.PROBE_W)
    ]


def noisy(centre: float, rng: random.Random, **kwargs) -> list[float]:
    return [v + rng.uniform(0, 60) for v in blob(centre, **kwargs)]


def follow(rows: list[list[float]]) -> list[float]:
    """The whole path pipeline minus the ffmpeg decode."""
    raw: list[float] = []
    last = 0.5
    for columns in rows:
        last = reframe._focus_centre(columns, last)
        raw.append(last)
    despiked = reframe._median_filter(raw, max(1, int(reframe.MEDIAN_S * reframe.PROBE_FPS)) | 1)
    window = max(1, int(reframe.SMOOTH_S * reframe.PROBE_FPS))
    smoothed = [
        sum(despiked[max(0, i - window + 1) : i + 1])
        / len(despiked[max(0, i - window + 1) : i + 1])
        for i in range(len(despiked))
    ]
    return reframe._follow(smoothed, step=1.0 / reframe.PROBE_FPS)


def travel(points: list[float]) -> float:
    return sum(abs(b - a) for a, b in zip(points, points[1:], strict=False))


#: A 9:16 crop out of 16:9 keeps 31.6% of the width, so the subject is in
#: frame while the crop centre is within half of that.
IN_FRAME = 0.158


def test_focus_centre_ignores_a_second_moving_thing():
    """A flashing alert box must not drag the crop off the person talking."""
    columns = [a + b for a, b in zip(blob(0.3), blob(0.9, weight=700), strict=False)]
    assert reframe._focus_centre(columns, 0.5) == pytest.approx(0.3, abs=0.03)


def test_focus_centre_holds_position_on_a_still_frame():
    assert reframe._focus_centre([0.0] * reframe.PROBE_W, 0.42) == 0.42


def test_median_filter_drops_a_spike_rather_than_averaging_it():
    values = [0.5] * 6
    values[3] = 0.95
    assert max(reframe._median_filter(values, 5)) == 0.5


def test_a_still_subject_does_not_move_the_crop_at_all():
    rng = random.Random(11)
    points = follow([noisy(0.5, rng) for _ in range(120)])
    assert travel(points) == 0.0


def test_fidgeting_in_a_chair_does_not_move_the_crop():
    """Small constant motion is exactly what a single-threshold crop chatters on."""
    rng = random.Random(12)
    rows = [noisy(0.5 + 0.02 * math.sin(i / 2), rng) for i in range(120)]
    assert travel(follow(rows)) == 0.0


def test_a_walking_subject_stays_in_frame():
    rng = random.Random(13)
    total = 160
    positions = [0.30 + 0.40 * (i / total) for i in range(total)]
    points = follow([noisy(p, rng) for p in positions])
    worst = max(abs(p - t) for p, t in zip(points, positions, strict=False))
    assert worst < IN_FRAME


def test_following_a_walk_costs_far_less_travel_than_the_walk_is_long():
    """The old path moved ten times further than the subject did. That was the jitter."""
    rng = random.Random(14)
    total = 160
    positions = [0.30 + 0.40 * (i / total) for i in range(total)]
    assert travel(follow([noisy(p, rng) for p in positions])) < 0.40 * 1.5


def test_the_crop_never_reverses_direction_while_following_a_one_way_walk():
    rng = random.Random(15)
    total = 160
    rows = [noisy(0.30 + 0.40 * (i / total), rng) for i in range(total)]
    points = follow(rows)
    speeds = [b - a for a, b in zip(points, points[1:], strict=False)]
    assert not [a for a, b in zip(speeds, speeds[1:], strict=False) if a * b < -1e-12]


def test_a_hard_cut_is_caught_up_with_and_not_overshot():
    rng = random.Random(16)
    total = 200
    rows = [noisy(0.25 if i < total // 2 else 0.75, rng) for i in range(total)]
    points = follow(rows)
    assert max(points) <= 0.76
    assert points[-1] == pytest.approx(0.75, abs=reframe.HOLD_ZONE + 0.005)


def test_the_crop_eases_in_rather_than_starting_at_full_speed():
    rng = random.Random(17)
    total = 200
    rows = [noisy(0.2 if i < 40 else 0.8, rng) for i in range(total)]
    points = follow(rows)
    step = 1.0 / reframe.PROBE_FPS
    speeds = [abs(b - a) / step for a, b in zip(points, points[1:], strict=False)]
    moving = [i for i, v in enumerate(speeds) if v > 1e-9]
    assert speeds[moving[0]] <= reframe.MAX_ACCEL_PER_S2 * step * 1.01
    assert max(speeds) <= reframe.MAX_PAN_PER_S * 1.01


def test_the_path_stays_inside_the_frame():
    rng = random.Random(18)
    rows = [noisy(0.98 if i % 2 else 0.02, rng) for i in range(200)]
    assert all(0.0 <= p <= 1.0 for p in follow(rows))


def test_crop_width_is_the_widest_portrait_slice_that_fits():
    path = reframe.Path_(points=[(0.0, 0.5)], source_w=1920, source_h=1080)
    assert path.crop_w == 606
    assert path.x_at(0.0) == pytest.approx((1920 - 606) / 2)


def test_x_at_clamps_a_crop_that_would_run_off_the_edge():
    path = reframe.Path_(points=[(0.0, 0.0), (10.0, 1.0)], source_w=1920, source_h=1080)
    assert path.x_at(0.0) == 0.0
    assert path.x_at(10.0) == pytest.approx(1920 - 606)
