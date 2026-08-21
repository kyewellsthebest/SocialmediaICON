from __future__ import annotations

from core.ffmpeg_ops import (
    TARGET_H,
    TARGET_W,
    compute_crop,
    escape_filter_path,
    focus_x_ratio,
)


def test_landscape_source_is_scaled_up_then_cropped():
    plan = compute_crop(1920, 1080, focus_ratio=0.5)
    assert plan.scale_h == TARGET_H  # height drives the scale for 16:9
    assert plan.scale_w >= TARGET_W
    assert 0 <= plan.crop_x <= plan.scale_w - TARGET_W
    assert "crop=1080:1920" in plan.filter_str


def test_focus_shifts_the_crop_window():
    left = compute_crop(1920, 1080, focus_ratio=0.15)
    centre = compute_crop(1920, 1080, focus_ratio=0.5)
    right = compute_crop(1920, 1080, focus_ratio=0.85)
    assert left.crop_x < centre.crop_x < right.crop_x
    for plan in (left, centre, right):
        assert 0 <= plan.crop_x <= plan.scale_w - TARGET_W


def test_already_vertical_source_needs_no_horizontal_crop():
    plan = compute_crop(1080, 1920)
    assert (plan.scale_w, plan.scale_h) == (TARGET_W, TARGET_H)
    assert plan.crop_x == 0 and plan.crop_y == 0


def test_square_and_small_sources_still_cover_the_frame():
    for src_w, src_h in ((1080, 1080), (640, 360), (720, 1280)):
        plan = compute_crop(src_w, src_h)
        assert plan.scale_w >= TARGET_W and plan.scale_h >= TARGET_H
        assert plan.scale_w % 2 == 0 and plan.scale_h % 2 == 0


def _frames_with_activity_at(col: int, cols: int = 64, rows: int = 36, count: int = 4):
    frames = []
    for i in range(count):
        buf = bytearray([20] * (cols * rows))
        for r in range(rows):
            buf[r * cols + col] = 200 if i % 2 == 0 else 60
        frames.append(bytes(buf))
    return frames


def test_focus_follows_the_moving_region():
    left = focus_x_ratio(_frames_with_activity_at(8))
    right = focus_x_ratio(_frames_with_activity_at(56))
    assert left < 0.5 < right
    # blended back towards the centre so one bright edge cannot pin the crop
    assert 0.15 <= left and right <= 0.85


def test_focus_defaults_to_centre_on_flat_or_missing_input():
    assert focus_x_ratio([]) == 0.5
    assert focus_x_ratio([bytes([50] * 64 * 36)] * 3) == 0.5
    assert focus_x_ratio([b"too short"]) == 0.5


def test_escape_filter_path():
    assert escape_filter_path("/tmp/a b/c.ass") == "/tmp/a b/c.ass"
    assert escape_filter_path(r"C:\work\c.ass") == r"C\:/work/c.ass"
    assert escape_filter_path("/tmp/it's.ass") == r"/tmp/it\'s.ass"
