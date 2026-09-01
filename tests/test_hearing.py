"""Can it tell a laugh from a backing track?

This exists because the previous audio signal was "which second was loudest",
and that cut a clip of a betting screen with music over it. Loudness cannot
separate a room reacting from a fader being pushed - only the shape of the
envelope can, and a claim about shape has to be tested against sounds whose
shape is known.

So every case here is synthesised from a definition (see synth_audio) and the
detector is asked to name it. Synthetic audio is not a real stream and this
file does not pretend otherwise; what it proves is that the thing being
measured is the thing the module claims to measure.
"""

from __future__ import annotations

import pytest
import synth_audio as synth

from core import hearing


@pytest.fixture(scope="module")
def sound(tmp_path_factory):
    where = tmp_path_factory.mktemp("sounds")
    return lambda name, samples: synth.write(name, samples, out=where)


def heard(sound, name, samples) -> hearing.Hearing:
    return hearing.listen(sound(name, samples))


class TestLaughter:
    """A laugh is a voiced pulse train at four to seven a second."""

    def _with_a_laugh(self, **kwargs):
        return synth.join(
            synth.speech(20), synth.laughter(4, **kwargs), synth.speech(6, seed=7)
        )

    @pytest.mark.parametrize("rate_hz", [3.8, 4.7, 5.5, 7.0])
    def test_it_is_found_across_the_whole_syllable_range(self, sound, rate_hz):
        found = heard(sound, f"laugh{rate_hz}", self._with_a_laugh(rate_hz=rate_hz))
        assert [a for a, _, _ in found.laughs if 18.0 <= a <= 25.0], (
            f"a laugh at {rate_hz} Hz was not heard"
        )

    def test_a_quiet_laugh_is_found_as_readily_as_a_loud_one(self, sound):
        """Depth is a ratio, so a laugh across the room is still a laugh."""
        found = heard(sound, "quietlaugh", self._with_a_laugh(level=0.10))
        assert found.laughs

    def test_it_is_found_through_music_playing_over_it(self, sound):
        under = synth.music(4, seed=8)
        over = synth.laughter(4, level=0.42)
        samples = synth.join(
            synth.music(20), [a + b for a, b in zip(under, over, strict=True)],
            synth.music(6, seed=9),
        )
        found = heard(sound, "laughovermusic", samples)
        assert [a for a, _, _ in found.laughs if 18.0 <= a <= 26.0]

    def test_it_is_placed_where_the_laugh_actually_is(self, sound):
        found = heard(sound, "placed", self._with_a_laugh())
        start, end, _ = found.laughs[0]
        assert start == pytest.approx(20.0, abs=1.5)
        assert end == pytest.approx(24.0, abs=1.5)

    def test_it_says_how_sure_it_is(self, sound):
        found = heard(sound, "sure", self._with_a_laugh())
        assert 0.0 < found.laughs[0][2] <= 1.0


class TestThingsThatAreNotLaughter:
    """Each of these has been a false positive at some point in this file's life."""

    def test_talking_is_not(self, sound):
        """Speech modulates at the same rate. Depth and regularity are the difference."""
        assert heard(sound, "talk", synth.speech(30)).laughs == []

    def test_talking_quickly_is_not(self, sound):
        assert heard(sound, "fasttalk", synth.speech(30, seed=31)).laughs == []

    def test_a_backing_track_is_not(self, sound):
        """128 BPM puts sixteenths at 8.5 Hz, right inside the laughter band."""
        assert heard(sound, "music", synth.music(30)).laughs == []

    def test_faster_music_is_not(self, sound):
        assert heard(sound, "fastmusic", synth.music(30, bpm=160)).laughs == []

    def test_the_start_of_a_track_is_not(self, sound):
        """The first window has no history, so it used to call itself unprecedented."""
        found = heard(sound, "trackstart", synth.music(30, bpm=90))
        assert found.laughs == [], "a stream that opens on music scored 0.64 at t=0"

    def test_a_cut_between_tracks_is_not(self, sound):
        samples = synth.join(synth.music(20, bpm=90), synth.music(10, bpm=90, seed=21))
        assert heard(sound, "trackcut", samples).laughs == []

    def test_a_burst_of_noise_is_not(self, sound):
        samples = synth.join(synth.speech(20), synth.room(4, level=0.25), synth.speech(6))
        assert heard(sound, "noise", samples).laughs == []

    def test_silence_is_not(self, sound):
        assert heard(sound, "silence", synth.room(30)).laughs == []


class TestRaisedVoices:
    def test_a_shout_is_heard(self, sound):
        samples = synth.join(
            synth.speech(20), synth.speech(3, level=0.85, seed=11, bright=3.0),
            synth.speech(7, seed=12),
        )
        found = heard(sound, "shout", samples)
        assert found.shouts
        assert any(19.0 <= t <= 24.0 for t, _ in found.shouts)

    def test_music_getting_louder_is_not_a_shout(self, sound):
        """This is the exact mistake the old loudness signal made."""
        samples = synth.join(synth.music(20), [v * 3.2 for v in synth.music(10, seed=5)])
        assert heard(sound, "louder", samples).shouts == []

    def test_a_steady_mix_produces_no_shouts_at_all(self, sound):
        assert heard(sound, "steady", synth.music(30)).shouts == []

    def test_ordinary_talking_produces_no_shouts(self, sound):
        """Loosening the brightness test to catch a real shout let five
        through on plain speech. Both ends have to hold at once."""
        assert heard(sound, "plaintalk", synth.speech(30)).shouts == []


class TestTellingAVoiceFromABreath:
    """The measurement everything about breath rests on.

    Verified against signals whose nature is not in doubt, because there is no
    recording of anybody breathing available here and an opinion is not a test.
    """

    def _voicing(self, sound, name, filt, seconds=12):
        import subprocess

        path = sound(name + "_seed", synth.room(0.5)).parent / f"{name}.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", filt, "-t", str(seconds),
             "-ar", "16000", "-ac", "1", "-y", str(path)],
            check=True, capture_output=True,
        )
        found = hearing.listen(path)
        return sorted(found.voicing)[len(found.voicing) // 2]

    def test_white_noise_is_not_a_voice(self, sound):
        assert self._voicing(sound, "white", "anoisesrc=c=white:a=0.3:r=16000") < 0.25

    def test_noise_in_the_band_breath_occupies_is_not_a_voice(self, sound):
        found = self._voicing(
            sound, "breathband",
            "anoisesrc=c=white:a=0.3:r=16000,highpass=f=900,lowpass=f=6000",
        )
        assert found < 0.25

    def test_a_pure_tone_is(self, sound):
        assert self._voicing(sound, "tone", "sine=frequency=200:sample_rate=16000") > 0.6

    def test_a_stack_of_harmonics_is(self, sound):
        found = self._voicing(
            sound, "harm",
            "aevalsrc='0.3*(sin(2*PI*150*t)+0.5*sin(2*PI*300*t)+0.3*sin(2*PI*450*t))':s=16000",
        )
        assert found > 0.6

    def test_the_two_do_not_overlap(self, sound):
        """A threshold is only meaningful if there is a gap to put it in."""
        noise = self._voicing(sound, "white2", "anoisesrc=c=white:a=0.3:r=16000")
        tone = self._voicing(sound, "tone2", "sine=frequency=200:sample_rate=16000")
        assert tone - noise > 0.4

    def test_the_threshold_sits_between_them(self):
        assert 0.25 < 1.0 - hearing.BREATHY < 0.7


class TestTheRoomGoingQuiet:
    def test_dead_air_after_a_loud_room_is_noticed(self, sound):
        samples = synth.join(synth.speech(20, level=0.4), synth.room(4), synth.speech(6, seed=13))
        found = heard(sound, "deadair", samples)
        assert found.drops
        assert any(19.0 <= a <= 25.0 for a, _ in found.drops)

    def test_continuous_talking_has_no_drop(self, sound):
        assert heard(sound, "nodrop", synth.speech(30)).drops == []


class TestSpeechOrMusic:
    def test_talking_reads_as_speech_not_music(self, sound):
        found = heard(sound, "isspeech", synth.speech(30))
        assert found.speech_share > found.music_share

    def test_a_mix_reads_as_music_not_speech(self, sound):
        found = heard(sound, "ismusic", synth.music(30))
        assert found.music_share > 0.5
        assert found.speech_share < 0.1


class TestTheShapeOfTheAnswer:
    def test_it_is_fast_enough_to_run_on_a_timer(self, sound):
        """Three streams, every twenty seconds, forever."""
        import time

        path = sound("timing", synth.speech(30))
        start = time.time()
        hearing.listen(path)
        assert time.time() - start < 3.0

    def test_audio_too_short_to_read_says_so(self, sound):
        with pytest.raises(hearing.HearingError):
            hearing.listen(sound("tiny", synth.room(0.5)))

    def test_the_summary_is_json_shaped(self, sound):
        import json

        found = heard(sound, "shape", synth.join(synth.speech(20), synth.laughter(4)))
        json.dumps(found.as_dict())
        assert set(found.as_dict()) >= {"laughs", "shouts", "drops", "speech_share"}


class TestTheEarWorksOnRealAudio:
    """Every fixture in this file is mono. Every real stream is stereo. The ear
    passed every test here and had never once worked in production.

    The band splitter merges nine taps - eight bands and the untouched signal -
    back into one stream and reads nine channels off it. That is nine only if
    each tap is mono. On a stereo source the merge makes eighteen, the nine
    requested cannot be reconciled with them, and ffmpeg fails the whole graph
    with "Error reinitializing filters". Not partially: it writes no audio and
    listen() raises.

    What it cost, measured on 27 minutes of real 1080p video: 168 windows
    scored, the ear failed on all 168, laughter and voice therefore scored zero
    on every one, and not a single window had two families of evidence
    agreeing - so almost everything faced the lone-signal bar and 141 of them
    were rejected as "one signal only".
    """

    def _talking(self):
        import synth_audio as sound

        return sound.join(sound.room(2.0), sound.speech(6.0), sound.room(2.0))

    def test_stereo_is_heard_at_all(self):
        import synth_audio as sound

        path = sound.write_stereo("talking", self._talking())
        found = hearing.listen(path)
        assert found.duration_s > 5.0, "a stereo source read as no audio at all"

    def test_it_hears_the_same_thing_in_stereo_as_in_mono(self):
        """Downmixing must not change what the ear reports, or every threshold
        tuned on the mono fixtures becomes wrong the moment this is fixed."""
        import synth_audio as sound

        samples = self._talking()
        one = hearing.listen(sound.write("talking-mono", samples))
        two = hearing.listen(sound.write_stereo("talking", samples))
        assert abs(one.speech_share - two.speech_share) < 0.05
        assert abs(len(one.shouts) - len(two.shouts)) <= 1

    def test_a_stereo_laugh_is_still_a_laugh(self):
        """The shape the other laughter tests use - speech first, so the laugh
        has a baseline to stand out from rather than silence."""
        import synth_audio as sound

        samples = sound.join(sound.speech(20), sound.laughter(4))
        assert hearing.listen(sound.write_stereo("laughing", samples)).laughs
