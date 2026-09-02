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

import httpx2 as httpx

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


class TestItDoesNotSendParametersTheModelRejects:
    """Every verdict failed for days, and the queue ranked in the teens
    because of it.

    look() sent `thinking: {"type": "adaptive"}` and `output_config.effort` on
    every call. Both arrived with the 4.6 generation; the default verdict model
    is claude-haiku-4-5, which is a 4.5 model and takes neither. A model that
    rejects a parameter rejects the whole call, so every look 400'd before it
    began. Every clip came back UNWATCHED with an API error stored on it - and
    because an unwatched clip also scored zero on the verdict axis, a third of
    the ranking went missing at the same time. One unsupported parameter, two
    symptoms that looked nothing like each other.
    """

    def _reply(self):
        import json

        body = json.dumps({
            "happening": "he drinks it and gags", "kind": "gross",
            "worth_it": True, "confidence": 0.8, "why": "a real reaction",
            "setting": "a kitchen",
        })
        return type("R", (), {
            "content": [type("B", (), {"type": "text", "text": body})()],
            "model": "stub",
            "usage": type("U", (), {"input_tokens": 10, "output_tokens": 5})(),
        })()

    def _client(self, *, strict=True):
        """Stands in for Haiku: 400s on anything from a later generation."""
        import anthropic

        seen: list[dict] = []
        reply = self._reply()

        class Client:
            class messages:  # noqa: N801
                @staticmethod
                def create(**request):
                    seen.append(request)
                    late = "thinking" in request or "effort" in (
                        request.get("output_config") or {})
                    if strict and late:
                        raise anthropic.BadRequestError(
                            "adaptive thinking is not supported on this model",
                            response=httpx.Response(
                                400, request=httpx.Request("POST", "http://x")),
                            body=None,
                        )
                    return reply

        return Client(), seen

    def test_a_45_model_is_not_asked_to_think_adaptively(self, clip, monkeypatch):
        monkeypatch.setattr(settings, "verdict_model", "claude-haiku-4-5")
        client, seen = self._client()
        monkeypatch.setattr("core.llm.get_client", lambda: client)
        found = verdict.look(clip, count=2)
        assert found.watched is True, found.problems
        assert "thinking" not in seen[0]
        assert "effort" not in (seen[0].get("output_config") or {})

    def test_a_46_model_still_is(self, clip, monkeypatch):
        monkeypatch.setattr(settings, "verdict_model", "claude-opus-5")
        monkeypatch.setattr(settings, "verdict_effort", "medium")
        client, seen = self._client(strict=False)
        monkeypatch.setattr("core.llm.get_client", lambda: client)
        verdict.look(clip, count=2)
        assert seen[0]["thinking"] == {"type": "adaptive"}
        assert seen[0]["output_config"]["effort"] == "medium"

    def test_a_model_that_refuses_anyway_is_asked_again_plainly(self, clip, monkeypatch):
        """The guard above is a list of model names and lists go stale. A
        rejected parameter must never again cost every verdict the bot forms."""
        monkeypatch.setattr(settings, "verdict_model", "claude-opus-5")
        client, seen = self._client(strict=True)
        monkeypatch.setattr("core.llm.get_client", lambda: client)
        found = verdict.look(clip, count=2)
        assert found.watched is True, found.problems
        assert len(seen) == 2, "it should have tried twice"
        assert "thinking" in seen[0] and "thinking" not in seen[1]

    def test_which_models_take_the_late_parameters(self):
        from core import llm

        assert llm.thinks_adaptively("claude-opus-5")
        assert llm.thinks_adaptively("claude-sonnet-5")
        assert llm.thinks_adaptively("claude-fable-5")
        assert not llm.thinks_adaptively("claude-haiku-4-5")
        assert not llm.thinks_adaptively("claude-sonnet-4-5")
        assert not llm.thinks_adaptively("")


class TestActivityIsNotAMoment:
    """Three clips of a wrestling match reached the page scored 41, 43 and 44,
    each approved by the model as a physical event - which they were, for
    their whole duration. None of them contained a takedown, a reaction or an
    ending. "Something is happening" was the criterion, and continuous
    activity satisfies it forever.

    So approving a clip now means having pointed at the second it turns.
    """

    def _payload(self, **over):
        base = {
            "happening": "two people wrestling in a paddling pool",
            "kind": "impressive", "worth_it": True, "confidence": 0.8,
            "why": "physical event with a crowd", "setting": "outdoor event",
            "moment_s": 12.0,
        }
        return base | over

    def test_a_clip_with_a_named_moment_is_approved(self):
        assert verdict._worth_it(self._payload()) is True

    def test_approval_without_a_moment_is_not_an_approval(self):
        """The exact shape of the failure: yes to worth_it, nothing to point
        at. Energetic footage of an event already underway."""
        assert verdict._worth_it(self._payload(moment_s=None)) is False

    def test_a_missing_field_is_not_a_moment_either(self):
        payload = self._payload()
        del payload["moment_s"]
        assert verdict._worth_it(payload) is False

    def test_a_refusal_stays_a_refusal_however_precise_it_is(self):
        assert verdict._worth_it(self._payload(worth_it=False)) is False

    def test_the_second_zero_still_counts_as_a_moment(self):
        """0.0 is a real answer and must not be read as "no moment"."""
        assert verdict._worth_it(self._payload(moment_s=0.0)) is True

    def test_the_schema_makes_the_model_answer(self):
        assert "moment_s" in verdict.SCHEMA["required"]
        assert "moment_s" in verdict.SCHEMA["properties"]

    def test_the_prompt_names_the_failure_it_is_guarding_against(self):
        """A prompt that only says "be hard to please" was already there and
        approved all three. It has to name the mistake."""
        said = verdict.SYSTEM.lower()
        assert "activity is not a moment" in said
        assert "before and an after" in said
