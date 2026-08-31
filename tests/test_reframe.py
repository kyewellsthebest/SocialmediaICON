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


class TestTheCropMovesEveryFrameRatherThanTenTimesASecond:
    """The path was always smooth. The delivery of it was a staircase.

    sendcmd fired ten times a second, so on a 60fps clip the crop held
    perfectly still for six frames and then jumped - measured on real 1080p60,
    up to 34.6 pixels in a single frame, with the crop frozen on 87% of
    frames. That snap is what "jittery" looks like, and no amount of
    smoothing the path could have fixed it.
    """

    def _pan(self) -> reframe.Path_:
        """A subject walking steadily across frame, at 1920x1080."""
        rows = [blob(0.2 + 0.6 * i / 200) for i in range(200)]
        fine = self._fine(rows)
        crop = reframe.Path_(points=[], source_w=1920, source_h=1080).crop_w
        half = crop / 2 / 1920
        step = 1.0 / reframe.PATH_FPS
        walked = reframe._follow(fine, step=step, bounds=(half, 1.0 - half))
        return reframe.Path_(
            points=[(i * step, v) for i, v in enumerate(walked)],
            source_w=1920, source_h=1080,
        )

    def _fine(self, rows):
        raw, last = [], 0.5
        for columns in rows:
            last = reframe._focus_centre(columns, last)
            raw.append(last)
        despiked = reframe._median_filter(
            raw, max(1, int(reframe.MEDIAN_S * reframe.PROBE_FPS)) | 1
        )
        w = max(1, int(reframe.SMOOTH_S * reframe.PROBE_FPS))
        smoothed = [
            sum(despiked[max(0, i - w + 1): i + 1]) / len(despiked[max(0, i - w + 1): i + 1])
            for i in range(len(despiked))
        ]
        probe_step = 1.0 / reframe.PROBE_FPS
        coarse = [(i * probe_step, v) for i, v in enumerate(smoothed)]
        end = coarse[-1][0]
        step = 1.0 / reframe.PATH_FPS
        return [reframe._sample(coarse, i * step) for i in range(int(end * reframe.PATH_FPS) + 1)]

    def _frames(self, path, sendcmd_hz, out_fps=60.0):
        """Crop x on each output frame. A command at k/hz holds until the
        next one, which is the staircase ffmpeg actually applies."""
        end = path.points[-1][0]
        return [
            path.x_at(int((i / out_fps) * sendcmd_hz + 1e-9) / sendcmd_hz)
            for i in range(int(end * out_fps))
        ]

    def _jerk(self, xs):
        d1 = [b - a for a, b in zip(xs, xs[1:], strict=False)]
        d2 = [abs(b - a) for a, b in zip(d1, d1[1:], strict=False)]
        return sum(d2) / len(d2), max(d2)

    def test_the_commands_are_written_at_frame_rate(self):
        assert reframe.PATH_FPS >= 60.0
        path = self._pan()
        assert len(path.points) > 60 * 10, "the path itself has to be dense too"

    def test_no_single_frame_jumps_more_than_a_pixel(self):
        """34.6 pixels in one frame was the old worst case."""
        _, worst = self._jerk(self._frames(self._pan(), reframe.PATH_FPS))
        assert worst < 1.0, f"worst single-frame jump is {worst:.2f}px"

    def test_it_is_at_least_ten_times_smoother_than_it_was(self):
        path = self._pan()
        was, _ = self._jerk(self._frames(path, 10.0))
        now, _ = self._jerk(self._frames(path, reframe.PATH_FPS))
        assert now * 10 < was, f"only {was / max(now, 1e-9):.1f}x smoother"

    def test_the_acceleration_cap_actually_binds_at_frame_rate(self):
        """Following at the measurement rate and interpolating afterwards let
        the crop teleport between capped positions. Following at frame rate is
        what makes the cap mean anything."""
        path = self._pan()
        allowed = reframe.MAX_ACCEL_PER_S2 / reframe.PATH_FPS / reframe.PATH_FPS * 1920
        xs = [v * 1920 for _, v in path.points]
        _, worst = self._jerk(xs)
        assert worst <= allowed * 1.5, f"{worst:.3f}px against a cap of {allowed:.3f}px"

    def test_the_crop_decelerates_into_the_frame_edge(self):
        """A centre outside the legal range used to be clipped after the fact,
        which bypassed every limit and stopped the crop dead."""
        step = 1.0 / reframe.PATH_FPS
        hard_left = [0.0] * int(reframe.PATH_FPS * 4)
        walked = reframe._follow(hard_left, step=step, bounds=(0.3, 0.7))
        assert min(walked) >= 0.3 - 1e-9, "the follower left the legal range"
        d1 = [b - a for a, b in zip(walked, walked[1:], strict=False)]
        d2 = [abs(b - a) for a, b in zip(d1, d1[1:], strict=False)]
        cap = reframe.MAX_ACCEL_PER_S2 / reframe.PATH_FPS / reframe.PATH_FPS
        assert max(d2) <= cap * 1.5, "it hit the wall instead of easing into it"

    def test_interpolation_between_knots_is_smooth_in_velocity_too(self):
        """Linear interpolation is continuous in position and not in speed,
        and a corner in the speed is as visible as a corner in the position."""
        points = [(0.0, 0.2), (1.0, 0.8), (2.0, 0.2)]
        xs = [reframe._sample(points, i / 240.0) for i in range(480)]
        d1 = [b - a for a, b in zip(xs, xs[1:], strict=False)]
        d2 = [abs(b - a) for a, b in zip(d1, d1[1:], strict=False)]
        # At the turn the speed reverses; smoothstep brings it to zero first.
        assert abs(d1[239]) < abs(d1[120]) / 5, "it turned without slowing down"
        assert max(d2) < 0.002


class TestADeskStreamIsStackedNotFollowed:
    """A screen-share breaks the whole idea of following the action, because
    the action is in two places: the thing being talked about is on the screen
    and the person talking is in a box in the corner. Following the motion
    oscillates between them and settles between them, which shows neither - a
    strip of desktop with half a face at the edge."""

    def test_a_webcam_in_the_corner_is_found(self):
        import synth_faces as people

        cam = reframe.find_webcam(people.screen_share())
        assert cam is not None
        assert cam.x + cam.w / 2 > 0.6, "it is on the right"
        assert cam.y + cam.h / 2 < 0.4, "and near the top"
        assert cam.seen > 0.5, "and it stays there"

    def test_a_person_on_camera_is_not_a_webcam_overlay(self):
        """The ordinary case: somebody filmed, filling the frame. Stacking
        that would put their forehead over their chin."""
        import synth_faces as people

        assert reframe.find_webcam(people.one_person()) is None

    def test_an_empty_room_is_not_one_either(self):
        import synth_faces as people

        assert reframe.find_webcam(people.nobody()) is None

    def test_the_output_is_still_portrait(self, tmp_path):
        import synth_faces as people

        from core.ffmpeg_ops import probe

        out = tmp_path / "stacked.mp4"
        reframe.to_portrait(people.screen_share(), out, work_dir=tmp_path)
        found = probe(out)
        assert (found.width, found.height) == (reframe.OUT_W, reframe.OUT_H)

    def test_the_webcam_gets_the_top_third(self):
        cam = reframe.Webcam(x=0.75, y=0.06, w=0.05, h=0.09, seen=1.0)
        chain = reframe.stacked_filter(cam, 1920, 1080)
        top = int(reframe.OUT_H * reframe.CAM_SHARE) // 2 * 2
        assert f"scale={reframe.OUT_W}:{top}" in chain
        assert f"scale={reframe.OUT_W}:{reframe.OUT_H - top}" in chain
        assert "vstack=inputs=2" in chain

    def test_the_crop_is_the_overlay_not_the_face(self):
        """The detector returns eyes to chin. Cropping to that fills the top
        third with a nose; a webcam box is head and shoulders."""
        cam = reframe.Webcam(x=0.75, y=0.06, w=0.05, h=0.09, seen=1.0)
        chain = reframe.stacked_filter(cam, 1920, 1080)
        crop = chain.split("[cam]crop=")[1].split(",")[0]
        w, h = (int(v) for v in crop.split(":")[:2])
        assert w > 0.05 * 1920 * 2, "the box has to be wider than the face"
        assert h > 0.09 * 1080 * 1.5

    def test_the_screen_half_is_the_middle_of_the_screen(self):
        cam = reframe.Webcam(x=0.88, y=0.04, w=0.05, h=0.09, seen=1.0)
        chain = reframe.stacked_filter(cam, 1920, 1080)
        crop = chain.split("[scr]crop=")[1].split(",")[0]
        w, h, x, y = (int(v) for v in crop.split(":"))
        assert abs((x + w / 2) - 1920 / 2) < 4, "horizontally centred"
        assert abs((y + h / 2) - 1080 / 2) < 4, "and vertically"

    def test_every_crop_fits_inside_the_frame(self):
        """A crop that runs off the edge is an ffmpeg error, not a bad shot."""
        for x, y in ((0.0, 0.0), (0.95, 0.0), (0.0, 0.92), (0.95, 0.92)):
            cam = reframe.Webcam(x=x, y=y, w=0.06, h=0.09, seen=1.0)
            chain = reframe.stacked_filter(cam, 1920, 1080)
            for part in ("[cam]crop=", "[scr]crop="):
                w, h, cx, cy = (
                    int(v) for v in chain.split(part)[1].split(",")[0].split(":"))
                assert cx >= 0 and cy >= 0
                assert cx + w <= 1920 and cy + h <= 1080, f"{part} at {x},{y}"

    def test_forcing_follow_ignores_the_webcam(self, tmp_path):
        import synth_faces as people

        from core.ffmpeg_ops import probe

        out = tmp_path / "followed.mp4"
        reframe.to_portrait(people.screen_share(), out, work_dir=tmp_path,
                            layout="follow")
        assert probe(out).width == reframe.OUT_W


class TestTheWebcamStripDoesNotDragTheGameIn:
    """The top strip is 1080x640, much wider than any webcam box, so filling
    it by growing the crop sideways takes whatever is beside the person. On a
    game stream that is the game: rendered that way, Ninja arrived in the top
    third with a Fortnite character standing next to him."""

    def _chain(self):
        cam = reframe.Webcam(x=0.05, y=0.53, w=0.07, h=0.15, seen=1.0)
        return reframe.stacked_filter(cam, 1920, 1080)

    def test_the_camera_is_scaled_to_fit_not_to_fill(self):
        assert "force_original_aspect_ratio=decrease" in self._chain()

    def test_what_is_left_at_the_sides_is_the_camera_blurred(self):
        """Not black bars, and not anything from outside the overlay."""
        chain = self._chain()
        assert "gblur" in chain
        assert "force_original_aspect_ratio=increase" in chain
        assert chain.count("[cam]crop=") == 1, "both layers come from one crop"

    def test_the_sharp_camera_is_centred_in_the_strip(self):
        assert "overlay=(W-w)/2:(H-h)/2" in self._chain()

    def test_the_crop_is_the_overlay_and_not_an_aspect_ratio(self):
        """Nothing has to be satisfied by the crop any more - the blur fills
        the strip - so it can be the box the person is in and nothing else."""
        cam = reframe.Webcam(x=0.05, y=0.53, w=0.07, h=0.15, seen=1.0)
        chain = reframe.stacked_filter(cam, 1920, 1080)
        w, h = (int(v) for v in chain.split("[cam]crop=")[1].split(",")[0].split(":")[:2])
        # The face is 134x162 px; the overlay is that grown, not stretched to 27:16.
        assert w / h < 1.5, f"{w}x{h} has been stretched to fill a wide strip"


class TestItSaysHowItFramedTheClip:
    """Which of the two layouts happened is the first question anyone asks
    when a clip looks wrong, and it cannot be worked out from the finished
    file: the decision comes from a face detection on a source that is deleted
    once the portrait version exists."""

    def test_a_desk_stream_reports_stacked_and_where_the_camera_was(self, tmp_path):
        import synth_faces as people

        report: dict = {}
        reframe.to_portrait(people.screen_share(), tmp_path / "a.mp4",
                            work_dir=tmp_path, report=report)
        assert report["layout"] == "stacked"
        assert report["webcam"]["seen"] > 0.5
        assert 0.0 <= report["webcam"]["x"] <= 1.0

    def test_everything_else_reports_followed_and_how_far_it_moved(self, tmp_path):
        import synth_faces as people

        report: dict = {}
        reframe.to_portrait(people.one_person(), tmp_path / "b.mp4",
                            work_dir=tmp_path, report=report)
        assert report["layout"] == "followed"
        assert report["travel"] >= 0.0

    def test_asking_for_no_report_is_not_a_crash(self, tmp_path):
        import synth_faces as people

        reframe.to_portrait(people.one_person(), tmp_path / "c.mp4", work_dir=tmp_path)
