"""Where the people are, and when a face changed.

Every other visual signal is about the whole picture, and none of them know the
difference between a camera panning across an empty room and a man's face going
from calm to horrified. The second one is the entire product.

There is no way to synthesise a face a Haar cascade will accept, so these tests
move a real photograph of a person around a real H.264 video. That is a fair
test of the detector and a hard one - the photo is a mid-shot, so the face is
near the size floor throughout.
"""

from __future__ import annotations

import pytest
import synth_faces as clips

from core import faces


@pytest.fixture(scope="module")
def alone():
    return faces.watch(clips.one_person())


class TestFindingPeople:
    def test_a_person_on_screen_is_found(self, alone):
        assert alone.on_screen > 0.8

    def test_an_empty_room_is_not_a_person(self):
        """A moving shape is a moving shape."""
        assert faces.watch(clips.nobody()).on_screen == 0.0

    def test_two_people_are_two_people(self):
        assert faces.watch(clips.two_people()).most == 2

    def test_the_size_of_the_biggest_face_is_kept(self, alone):
        assert 0.0 < alone.biggest < 1.0

    def test_the_frame_size_is_above_the_cascade_floor(self):
        """Measured: the cascade finds a face down to about 22 pixels across.

        At 320x180 this exact photograph was never found once. The frame size
        is not a taste question - it decides which faces exist at all.
        """
        assert faces.HEIGHT * faces.MIN_FACE >= 22


class TestWhenSomethingHappensToAFace:
    def test_leaning_into_the_camera_is_noticed(self):
        found = faces.watch(clips.leans_in(at=20.0))
        assert any(19.0 <= t <= 23.0 for t, _ in found.close_ups)

    def test_a_person_sitting_still_is_not(self, alone):
        assert alone.close_ups == []

    def test_and_neither_is_an_empty_room(self):
        found = faces.watch(clips.nobody())
        assert found.close_ups == []
        assert found.reactions == []

    def test_the_close_up_says_how_much_of_the_frame_it_filled(self):
        found = faces.watch(clips.leans_in(at=20.0))
        assert found.close_ups[0][1] > 0.06


class TestTheCostOfLooking:
    def test_a_face_pass_is_affordable_on_a_timer(self):
        """Three streams, every twenty seconds, forever."""
        import time

        start = time.time()
        faces.watch(clips.one_person())
        assert time.time() - start < 8.0

    def test_nothing_to_look_at_says_so(self, tmp_path):
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        with pytest.raises(faces.FacesError):
            faces.watch(empty)

    def test_the_summary_is_json_shaped(self, alone):
        import json

        json.dumps(alone.as_dict())
        assert set(alone.as_dict()) >= {"on_screen", "biggest_face", "reactions"}


class TestFacesAreEvidence:
    def test_a_face_changing_can_nominate_a_moment(self):
        from core import moments

        assert "face_reaction" in moments.SENSED

    def test_and_outweighs_the_picture_moving(self):
        """A camera pan moves every pixel and means nothing."""
        from core import moments

        assert moments.WEIGHTS["face_reaction"] > moments.WEIGHTS["motion_surge"]
