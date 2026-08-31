"""The last check before a clip exists, and the only one that looks.

Everything upstream is arithmetic, and arithmetic cannot tell a man laughing at
his own joke about nothing from a man falling off a chair - they produce the
same envelope, the same motion surge and the same chat burst. This is the step
that can, and once posting stops going past a person it is the only thing
between a bad clip and an audience.

The model call itself is not tested here - a test that asserts what a model
says is a test of the model. What is tested is everything around it: that the
frames are real frames from the right places, that a refusal is honoured, that
an unwatchable candidate is refused rather than waved through, and that a
failure to look never takes the watcher down with it.
"""

from __future__ import annotations

import subprocess

import pytest

from core import verdict
from core.config import settings
from core.supervisor import Found, Held, Supervisor


def _held(raw):
    return Held(channel="x", found=Found(), raw=raw, cut_at=0.0, duration_s=30.0)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("clips") / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30",
         "-t", "40", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-y", str(path)],
        check=True, capture_output=True,
    )
    return path


class TestSamplingWhatItLooksAt:
    def test_it_gets_the_number_of_frames_it_asked_for(self, clip):
        assert len(verdict.sample_frames(clip, count=12)) == 12

    def test_they_are_spread_across_the_whole_clip(self, clip):
        """A dozen frames of the first two seconds is not watching it."""
        found = verdict.sample_frames(clip, count=12)
        assert found[0][0] < 1.0
        assert found[-1][0] > 30.0

    def test_they_are_evenly_spaced(self, clip):
        times = [t for t, _ in verdict.sample_frames(clip, count=12)]
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        assert max(gaps) - min(gaps) < 0.5

    def test_each_one_is_a_complete_picture(self, clip):
        """image2pipe writes them end to end with no length prefix."""
        for _, data in verdict.sample_frames(clip, count=8):
            assert data.startswith(b"\xff\xd8\xff"), "not a JPEG"
            assert data.endswith(b"\xff\xd9"), "a truncated JPEG"

    def test_a_short_clip_still_yields_frames(self, tmp_path):
        path = tmp_path / "short.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30",
             "-t", "3", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", "-y", str(path)],
            check=True, capture_output=True,
        )
        assert verdict.sample_frames(path, count=12)

    def test_nothing_to_look_at_says_so(self, tmp_path):
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        with pytest.raises(verdict.VerdictError):
            verdict.sample_frames(empty)


class TestFailingToLookIsNeverFatal:
    def test_a_file_it_cannot_read_comes_back_unwatched(self, tmp_path):
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        found = verdict.look(empty)
        assert found.watched is False
        assert found.problems

    def test_an_unwatched_verdict_is_not_an_approval(self, tmp_path):
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        assert verdict.look(empty).worth_it is False

    def test_no_key_does_not_raise(self, clip, monkeypatch):
        monkeypatch.setattr(settings, "anthropic_api_key", None)
        found = verdict.look(clip, count=2)
        assert found.watched is False
        assert found.problems


class TestTheGate:
    def _verdict(self, **kwargs):
        return verdict.Verdict(watched=True, **kwargs)

    def test_a_clear_yes_passes(self):
        assert Supervisor._acceptable(self._verdict(worth_it=True, confidence=0.9))

    def test_a_clear_no_does_not(self):
        assert not Supervisor._acceptable(self._verdict(worth_it=False, confidence=0.9))

    def test_an_unsure_yes_does_not(self):
        """Refusing a mediocre clip costs one clip; posting one costs the account."""
        assert not Supervisor._acceptable(self._verdict(worth_it=True, confidence=0.2))

    def test_the_bar_is_configurable(self, monkeypatch):
        monkeypatch.setattr(settings, "verdict_min_confidence", 0.95)
        assert not Supervisor._acceptable(self._verdict(worth_it=True, confidence=0.9))


class TestTheBudget:
    def test_looking_stops_when_the_days_money_is_gone(self, monkeypatch, tmp_path):
        """A refused candidate is not stored, so it cannot throttle itself."""
        monkeypatch.setattr(settings, "verdict_daily_usd", 2.50)
        sup = Supervisor()
        sup.spent_today()
        sup.spend["usd"] = 2.50
        found = sup.consider(_held(tmp_path / "nope.mp4"))
        assert found.watched is False
        assert "spent" in " ".join(found.problems)

    def test_a_look_is_priced_from_what_the_api_reported(self):
        """Not estimated from the request. An estimate is what let a budget of
        thirty looks a day survive a redesign meant to clip everything -
        nobody could see the bill, so nobody could see it was the wrong shape."""
        usage = type("U", (), {
            "input_tokens": 900, "output_tokens": 900,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1300,
        })()
        # haiku: 900 in @ $1, 900 out @ $5, 1300 cached @ $0.10 per million
        assert verdict.price_of("claude-haiku-4-5", usage) == pytest.approx(
            900 / 1e6 * 1.0 + 900 / 1e6 * 5.0 + 1300 / 1e6 * 0.10
        )

    def test_a_model_nobody_priced_is_assumed_expensive(self):
        """So an unknown model cannot quietly spend more than the budget says."""
        usage = type("U", (), {"input_tokens": 1_000_000, "output_tokens": 0,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0})()
        assert verdict.price_of("something-new", usage) == pytest.approx(
            verdict.DEFAULT_PRICE[0]
        )

    def test_a_cache_write_is_not_free(self):
        """Otherwise the first look of the day is reported as costing nothing."""
        usage = type("U", (), {"input_tokens": 0, "output_tokens": 0,
                               "cache_creation_input_tokens": 1_000_000,
                               "cache_read_input_tokens": 0})()
        assert verdict.price_of("claude-haiku-4-5", usage) == pytest.approx(1.25)

    def test_switching_looking_off_is_honoured(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "verdict_enabled", False)
        found = Supervisor().consider(_held(tmp_path / "nope.mp4"))
        assert found.watched is False
        assert "switched off" in " ".join(found.problems)


class TestWhatItIsToldAboutTheMoment:
    def test_the_evidence_is_put_into_words(self):
        said = verdict._describe({
            "heard": {"laughs": [1], "shouts": [1, 2], "drops": [], "speech_share": 0.62,
                      "music_share": 0.05},
            "seen": {"surges": [1], "cuts": [1, 2, 3]},
        })
        assert "laughter heard 1" in said
        assert "voice raised 2" in said
        assert "62% speech-like" in said

    def test_no_evidence_says_so_rather_than_inventing_some(self):
        assert "nothing" in verdict._describe(None).lower()
        assert "nothing" in verdict._describe({}).lower()

    def test_chat_is_quoted_not_summarised(self):
        said = verdict._describe_quotes(["KEKW", "OH MY GOD"])
        assert "KEKW" in said and "OH MY GOD" in said

    def test_the_prompt_says_the_evidence_is_not_proof(self):
        """The whole failure mode is trusting the numbers that got it here."""
        assert "not proof" in verdict.SYSTEM

    def test_the_schema_demands_a_reason(self):
        assert "why" in verdict.SCHEMA["required"]
        assert "worth_it" in verdict.SCHEMA["required"]
