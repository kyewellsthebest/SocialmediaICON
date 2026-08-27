"""The studio: archives, beds, overlay frames and the composite command.

The expensive half of a render - encoding - is not exercised here. What is
exercised is everything that decides *what* gets encoded, because that is where
a mistake is silent: a filter graph that drops the audio, a caption that runs
past the end of its shot, or a written line that has quietly been marked as
quoted from the record.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from core import archives, beds
from core.archives import Beat, Grade
from core.produce import Options, _ass_colour, build_command, write_captions

# --- the registry ----------------------------------------------------------


def test_every_archive_in_order_exists() -> None:
    assert set(archives.ORDER) == set(archives.ARCHIVES)
    assert len(archives.ORDER) == 6


def test_unknown_archive_names_the_valid_ones() -> None:
    with pytest.raises(KeyError) as exc:
        archives.get("watergate")
    assert "apollo" in str(exc.value)


@pytest.mark.parametrize("archive_id", archives.ORDER)
def test_timeline_is_contiguous_and_matches_duration(archive_id: str) -> None:
    archive = archives.get(archive_id)
    for voice_hook in (False, True):
        timeline = archive.timeline(voice_hook)
        assert timeline[0][0] == 0.0
        for (_, end, _), (next_start, _, _) in zip(timeline, timeline[1:], strict=False):
            assert end == pytest.approx(next_start)
        assert timeline[-1][1] == pytest.approx(archive.duration_s(voice_hook))


@pytest.mark.parametrize("archive_id", archives.ORDER)
def test_videos_land_in_short_form_length(archive_id: str) -> None:
    # Under 25s reads as a fragment; over 60s stops being short form on every
    # platform this posts to.
    duration = archives.get(archive_id).duration_s()
    assert 25.0 <= duration <= 60.0


@pytest.mark.parametrize("archive_id", archives.ORDER)
def test_only_tape_lines_are_ever_marked_verbatim(archive_id: str) -> None:
    """The one rule that would end the format if it slipped.

    A caption marked verbatim is presented to a viewer as a quotation from the
    record. Narration is written here, so narration must never carry the flag.
    """
    for beat in archives.get(archive_id).running_order():
        if beat.verbatim:
            assert beat.from_tape, f"{archive_id}: written line marked verbatim: {beat.text!r}"


@pytest.mark.parametrize("archive_id", archives.ORDER)
def test_archives_declare_their_provenance(archive_id: str) -> None:
    archive = archives.get(archive_id)
    quoted = any(b.verbatim for b in archive.running_order())
    lowered = archive.provenance.lower()
    assert lowered.startswith("verbatim") if quoted else "narration only" in lowered


def test_caption_words_stay_inside_their_shots() -> None:
    archive = archives.get("apollo")
    words = archives.caption_words(archive)
    assert words
    assert all(w["start"] < w["end"] for w in words)
    for first, second in zip(words, words[1:], strict=False):
        assert first["end"] <= second["start"] + 1e-6
    assert words[-1]["end"] <= archive.duration_s()


def test_voice_hook_changes_the_opening_only() -> None:
    archive = archives.get("apollo")
    cold = archive.running_order(False)
    voiced = archive.running_order(True)
    assert cold[0].text != voiced[0].text
    assert cold[1:] == voiced[1:]


def test_spread_words_weights_longer_words() -> None:
    words = archives.spread_words("a considerably longer", 0.0, 3.0)
    widths = [w.end - w.start for w in words]
    assert widths[0] < widths[1]
    assert sum(widths) == pytest.approx(3.0 * 0.82)


def test_spread_words_handles_nothing_to_say() -> None:
    assert archives.spread_words("", 0.0, 3.0) == []
    assert archives.spread_words("word", 3.0, 3.0) == []


def test_readiness_names_the_variable_that_fixes_it() -> None:
    ready = archives.readiness(archives.get("apollo"), has_tts=False, has_stock=False)
    assert not ready.ok
    assert ready.missing == ["OPENAI_API_KEY", "PEXELS_API_KEY"]
    assert archives.readiness(archives.get("apollo"), has_tts=True, has_stock=True).ok


def test_unfetchable_archive_says_so_even_when_keys_are_set() -> None:
    # STARGATE is 12,473 documents and no tape, so it is the only source that
    # can have every key set and still need a file from a human.
    ready = archives.readiness(archives.get("stargate"), has_tts=True, has_stock=True)
    assert ready.ok
    assert any("upload" in note for note in ready.notes)


# --- the grade -------------------------------------------------------------


def test_grade_at_zero_strength_is_a_no_op() -> None:
    chain = Grade(gray=1.0, contrast=2.0, brightness=0.5).ffmpeg_filter(0.0)
    assert "colorchannelmixer" not in chain
    assert "contrast=1.000" in chain
    assert "brightness=0.000" in chain


def test_grade_scales_with_strength() -> None:
    grade = Grade(gray=1.0, contrast=2.0, brightness=0.5)
    assert "contrast=1.500" in grade.ffmpeg_filter(0.5)
    assert "contrast=2.000" in grade.ffmpeg_filter(1.0)


def test_ass_colour_swaps_to_bgr() -> None:
    # ASS is &HAABBGGRR, so a pure red must come out with the red last.
    assert _ass_colour("#FF0000") == "&H000000FF"
    assert _ass_colour("#1FE0A8") == "&H00A8E01F"


# --- beds ------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(beds.RECIPES))
def test_beds_produce_playable_audio(kind: str, tmp_path: Path) -> None:
    path = beds.synth(kind, 0.5, tmp_path / f"{kind}.wav")
    with wave.open(str(path)) as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == beds.RATE
        assert handle.getnframes() == pytest.approx(int(0.5 * beds.RATE), rel=0.01)


def test_beds_are_deterministic(tmp_path: Path) -> None:
    a = beds.synth("radio", 0.3, tmp_path / "a.wav").read_bytes()
    b = beds.synth("radio", 0.3, tmp_path / "b.wav").read_bytes()
    assert a == b


def test_buzz_actually_pulses() -> None:
    # On for a beat, off for a beat: silence at the top of a cycle would mean
    # the envelope is inverted and the bed would be a continuous tone.
    assert beds._buzz(0.5) != 0.0
    assert beds._buzz(beds.BUZZ_ON_S + 0.4) == 0.0


# --- captions --------------------------------------------------------------


def test_captions_are_written_with_the_source_accent(tmp_path: Path) -> None:
    archive = archives.get("apollo")
    path = write_captions(archive, tmp_path / "c.ass", voice_hook=False)
    body = path.read_text(encoding="utf-8")
    assert _ass_colour(archive.accent) in body
    assert "Dialogue:" in body
    # The line is split across dialogue events by the line grouper, so look
    # for words rather than the whole sentence.
    assert "houston" in body.lower()
    assert "problem" in body.lower()


# --- the composite command -------------------------------------------------


def _command(**overrides: object) -> list[str]:
    archive = archives.get("apollo")
    kwargs: dict = {
        "archive": archive,
        "duration": 38.0,
        "fps": 24,
        "grade": 0.9,
        "stock": None,
        "frames_glob": "/tmp/f/frame-%05d.png",
        "static_png": Path("/tmp/static.png"),
        "ass_path": Path("/tmp/c.ass"),
        "bed_wav": Path("/tmp/bed.wav"),
        "tape": None,
        "narration": [],
        "dest": Path("/tmp/out.mp4"),
        "duck_windows": [],
        "crf": 20,
    }
    kwargs.update(overrides)
    return build_command(**kwargs)  # type: ignore[arg-type]


def _graph(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1]


def test_command_always_maps_both_streams() -> None:
    cmd = _command()
    assert cmd[cmd.index("-map") + 1] == "[v]"
    assert "[a]" in cmd
    assert cmd[-1] == "/tmp/out.mp4"


def test_without_stock_a_drawn_plate_is_generated() -> None:
    cmd = _command()
    assert "lavfi" in cmd
    assert "gradients=" in " ".join(cmd)


def test_with_stock_the_clip_is_looped_and_cropped() -> None:
    cmd = _command(stock=Path("/tmp/clip.mp4"))
    assert "-stream_loop" in cmd
    assert "crop=1080:1920" in _graph(cmd)


def test_the_plate_is_graded_gently_and_footage_hard() -> None:
    """The plate is already in palette; grading it again only removes light."""
    plate = _graph(_command())
    footage = _graph(_command(stock=Path("/tmp/clip.mp4")))
    assert "contrast=1.110" in plate  # 1 + (0.35 * 0.9) * (1.35 - 1)
    assert "contrast=1.315" in footage  # 1 + 0.9 * (1.35 - 1)


def test_layers_composite_in_order() -> None:
    graph = _graph(_command())
    assert graph.index("[tinted]") < graph.index("[withframes]") < graph.index("[withstatic]")
    assert "ass=" in graph


def test_narration_is_delayed_to_its_beat() -> None:
    cmd = _command(narration=[(Path("/tmp/n0.mp3"), 19.5)])
    assert "adelay=19500|19500" in _graph(cmd)


def test_tape_is_trimmed_and_ducked_not_cut() -> None:
    cmd = _command(
        tape=(Path("/tmp/tape.mp3"), 12.0),
        duck_windows=[(19.0, 25.5)],
    )
    graph = _graph(cmd)
    assert "atrim=start=12.000" in graph
    assert "enable='between(t,19.00,25.50)':volume=0.20" in graph


def test_mix_counts_every_audio_source() -> None:
    graph = _graph(
        _command(
            tape=(Path("/tmp/tape.mp3"), 0.0),
            narration=[(Path("/tmp/a.mp3"), 1.0), (Path("/tmp/b.mp3"), 9.0)],
        )
    )
    assert "amix=inputs=4" in graph  # bed + tape + two narration lines


def test_mix_is_just_the_bed_when_nothing_else_survives() -> None:
    assert "amix=inputs=1" in _graph(_command())


# --- options ---------------------------------------------------------------


def test_options_clamp_out_of_range_dials() -> None:
    assert Options("apollo", grade=4.0).resolved_grade() == 1.0
    assert Options("apollo", overlay=-2.0).resolved_overlay() == 0.0


def test_options_fall_back_to_settings() -> None:
    from core.config import settings

    assert Options("apollo").resolved_grade() == settings.studio_grade
    assert Options("apollo").resolved_fps() == settings.studio_fps


# --- overlay ---------------------------------------------------------------


@pytest.mark.parametrize("archive_id", archives.ORDER)
def test_every_source_can_be_drawn(archive_id: str, tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    from core import overlay

    archive = archives.get(archive_id)
    written = overlay.render_frames(archive, tmp_path / archive_id, fps=24, limit=3)
    assert written == 3
    frames = sorted((tmp_path / archive_id).glob("*.png"))
    assert [f.name for f in frames] == ["frame-00001.png", "frame-00002.png", "frame-00003.png"]
    assert all(f.stat().st_size > 0 for f in frames)

    static = overlay.render_static(archive, tmp_path / f"{archive_id}.png")
    assert static.stat().st_size > 0


def test_overlay_strength_never_makes_type_invisible() -> None:
    pytest.importorskip("PIL")
    from core.overlay import Painter

    faint = Painter(draw=None, accent="#FFFFFF", t=0.0, local=0.0,
                    beat=Beat("tape", 1.0), alpha=0.0)
    # The slider dials the instruments back, not the HUD out of existence.
    assert faint.a(1.0) == pytest.approx(0.5)
    assert faint.solid(1.0) == 1.0


def test_frame_count_follows_duration_and_fps(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    from core import overlay

    archive = archives.get("apollo")
    written = overlay.render_frames(archive, tmp_path / "n", fps=2)
    assert written == int(round(archive.duration_s() * 2))


# --- getting the recording -------------------------------------------------


def test_every_archive_with_audio_can_find_it_without_a_key() -> None:
    """Only STARGATE is documents; the rest have a recording somewhere public.

    None of this needs an API key - archive.org's search and metadata
    endpoints are open - so a source that cannot be fetched is a gap in the
    registry, not a billing question.
    """
    for archive_id in archives.ORDER:
        archive = archives.get(archive_id)
        if archive_id == "stargate":
            assert not archive.fetchable
            continue
        assert archive.fetchable, f"{archive_id} has neither an item nor a query"


def _cached(monkeypatch, tmp_path: Path, *names: str) -> None:
    """Point the tape cache at tmp_path and pre-create the files named."""
    from core import produce

    monkeypatch.setattr(produce, "tape_cache", lambda: tmp_path)
    for name in names:
        (tmp_path / name).write_bytes(b"audio")


def test_pinned_item_is_tried_before_the_search(monkeypatch, tmp_path: Path) -> None:
    from core import produce

    seen: list[str] = []

    def fake_item(item: str):
        seen.append(item)
        return (item, "tape.mp3")

    monkeypatch.setattr(produce, "_audio_from_item", fake_item)
    monkeypatch.setattr(
        produce, "search_archive_org", lambda q, **k: pytest.fail("search should not run")
    )
    _cached(monkeypatch, tmp_path, "my-item-tape.mp3")

    path = produce.fetch_archive_audio(archives.get("nixon"), override_item="my-item")
    assert seen == ["my-item"]
    assert path == tmp_path / "my-item-tape.mp3"


def test_search_skips_items_with_nothing_playable(monkeypatch, tmp_path: Path) -> None:
    """A hit with no audio in it is not a reason to stop looking."""
    from core import produce

    monkeypatch.setattr(produce, "search_archive_org", lambda q, **k: ["empty", "good"])
    monkeypatch.setattr(
        produce, "_audio_from_item", lambda item: (item, "t.mp3") if item == "good" else None
    )
    _cached(monkeypatch, tmp_path, "good-t.mp3")

    path = produce.fetch_archive_audio(archives.get("nixon"))
    assert path is not None
    assert "good" in path.name


def test_a_dead_search_is_not_a_crash(monkeypatch) -> None:
    from core import produce

    monkeypatch.setattr(produce, "search_archive_org", lambda q, **k: [])
    assert produce.fetch_archive_audio(archives.get("nixon")) is None


def test_video_containers_are_accepted_last(monkeypatch) -> None:
    """Some sources only exist publicly as video; ffmpeg reads their audio."""
    from core import produce

    order = produce.AUDIO_FORMAT_PREFERENCE
    assert ".mp3" in order
    assert order.index(".mp4") == len(order) - 1


# --- the working directory -------------------------------------------------


def test_workspace_creates_a_missing_work_dir(monkeypatch, tmp_path: Path) -> None:
    """A fresh container has no WORK_DIR - nothing in the image creates it.

    This is the failure that crashed the first real render: mkdtemp(dir=...)
    raises FileNotFoundError rather than creating its parent, so the render
    died with a bare "No such file or directory" naming a scratch path.
    """
    from core import produce

    missing = tmp_path / "does" / "not" / "exist"
    monkeypatch.setattr(produce.settings, "work_dir", missing)
    assert not missing.exists()

    made = produce.workspace()
    assert made == missing
    assert made.is_dir()


def test_workspace_nests_and_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    from core import produce

    monkeypatch.setattr(produce.settings, "work_dir", tmp_path / "w")
    first = produce.workspace("tape")
    second = produce.workspace("tape")
    assert first == second == tmp_path / "w" / "tape"
    assert first.is_dir()


def test_a_render_can_start_with_no_work_dir(monkeypatch, tmp_path: Path) -> None:
    """The scratch directory is made before mkdtemp is asked to use it."""
    import tempfile as tf

    from core import produce

    missing = tmp_path / "app" / ".work"
    monkeypatch.setattr(produce.settings, "work_dir", missing)

    # Exactly what produce() does before it touches anything else.
    root = Path(tf.mkdtemp(prefix="studio-", dir=str(produce.workspace())))
    assert root.is_dir()
    assert root.parent == missing
