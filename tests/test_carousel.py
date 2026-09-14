"""The two-slide Instagram post, and when it goes out.

The rendering tests build real files with ffmpeg and measure the pixels,
because the two things that can go wrong here are both invisible to a unit
test of the code: a square that cropped the lift out of the frame, and a cover
that is just a frame of the video with nothing to swipe for.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import carousel
from core.config import settings
from core.models import Base, Reel

needs_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg is a system binary and is not installed here",
)


def a_video(path: Path, size: str = "1080x1920", seconds: int = 3) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


def dimensions(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    width, height = out.stdout.strip().split(",")[:2]
    return int(width), int(height)


@needs_ffmpeg
class TestTheSquareKeepsTheWholeFrame:
    """A 9:16 gym video cut to a square loses either the barbell or the
    lifter, and which one depends on where the crop lands. So the whole frame
    is fitted and the gap is filled rather than cut."""

    def test_it_comes_out_square(self, tmp_path):
        out = carousel.square(a_video(tmp_path / "tall.mp4"), tmp_path / "out")
        assert dimensions(out) == (carousel.SIDE, carousel.SIDE)

    def test_a_wide_video_is_squared_too(self, tmp_path):
        """Reddit serves landscape as well as portrait."""
        out = carousel.square(a_video(tmp_path / "wide.mp4", "1920x1080"),
                              tmp_path / "out")
        assert dimensions(out) == (carousel.SIDE, carousel.SIDE)

    def test_nothing_is_cropped_out_of_the_picture(self, tmp_path):
        """A portrait video in a square is *pillarboxed*, not letterboxed: a
        1080x1920 source fitted into 1080x1080 is 607 wide and full height, so
        the picture is a band down the middle and the fill is at the sides. A
        cropped version would be sharp from edge to edge."""
        from PIL import Image

        out = carousel.square(a_video(tmp_path / "tall.mp4"), tmp_path / "out")
        shot = tmp_path / "shot.png"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(out),
             "-frames:v", "1", str(shot)],
            check=True, capture_output=True,
        )
        frame = Image.open(shot).convert("L")

        # Edge energy, not colour: "blurred" means neighbouring pixels are
        # close together, and a colour-bar test pattern is vivid whether it is
        # blurred or not - which is what made the first version of this test
        # pass on a cropped render.
        def sharpness(x0, x1):
            """Edge energy down a vertical band, averaged."""
            total = count = 0
            for x in range(x0, x1, 4):
                column = [frame.getpixel((x, y)) for y in range(0, carousel.SIDE, 2)]
                total += sum(abs(b - a)
                             for a, b in zip(column, column[1:], strict=False))
                count += len(column)
            return total / max(count, 1)

        # 607 wide, centred: x 236-843 is the picture, outside it is fill.
        picture = sharpness(300, 780)
        fill = sharpness(20, 200)
        assert picture > fill * 2, (
            f"the sides should be blurred fill (picture {picture:.1f} vs "
            f"fill {fill:.1f}) - this looks like a crop")

    def test_the_sound_survives(self, tmp_path):
        out = carousel.square(a_video(tmp_path / "tall.mp4"), tmp_path / "out")
        streams = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=True,
        )
        assert "audio" in streams.stdout

    def test_a_silent_video_still_squares(self, tmp_path):
        silent = tmp_path / "silent.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "testsrc=size=720x1280:rate=24:duration=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent)],
            check=True, capture_output=True,
        )
        assert carousel.square(silent, tmp_path / "out").exists()


@needs_ffmpeg
class TestTheCover:
    def test_it_is_square_and_not_enormous(self, tmp_path):
        from PIL import Image

        frame = carousel.first_frame(a_video(tmp_path / "v.mp4"), tmp_path / "out")
        cover = carousel.cover(frame, tmp_path / "out")
        with Image.open(cover) as image:
            assert image.size == (carousel.SIDE, carousel.SIDE)
        assert cover.stat().st_size < 2_000_000, "a cover this heavy is a JPEG fault"

    def test_the_still_is_not_taken_from_the_very_first_frame(self):
        """Which is very often black, a fade, or a hand still reaching for the
        phone - none of which say anything about the video behind them."""
        assert settings.carousel_frame_at_s > 0

    def test_a_video_shorter_than_the_offset_still_gets_a_cover(self, tmp_path):
        """Reddit's floor is four seconds, but the offset is configurable and
        somebody will set it to ten."""
        short = a_video(tmp_path / "short.mp4", seconds=1)
        frame = carousel.first_frame(short, tmp_path / "out", at_s=30.0)
        assert frame.exists()

    def test_it_carries_the_mark_and_the_swipe_cue(self, tmp_path):
        """The cover's whole job is to be a reason to swipe. A bare frame of
        the video gives nobody one."""
        from PIL import Image

        frame = carousel.first_frame(a_video(tmp_path / "v.mp4"), tmp_path / "out")
        plain = Image.open(frame).convert("RGB")
        cover = Image.open(carousel.cover(frame, tmp_path / "out")).convert("RGB")

        # The badge sits top centre, the cue bottom centre. Both must have
        # changed the image relative to a plain letterbox of the same frame.
        bare = carousel._fit_on_blur(plain)
        from PIL import ImageChops

        diff = ImageChops.difference(bare, cover)
        top = diff.crop((300, 0, 780, 220)).convert("L")
        bottom = diff.crop((150, 880, 930, 1040)).convert("L")
        assert sum(top.getdata()) / (top.width * top.height) > 5, "no mark on the cover"
        assert sum(bottom.getdata()) / (bottom.width * bottom.height) > 5, "no swipe cue"

    def test_both_slides_come_back_in_order(self, tmp_path):
        first, second = carousel.build(a_video(tmp_path / "v.mp4"), tmp_path / "out")
        assert first.suffix == ".jpg" and second.suffix == ".mp4"
        assert dimensions(second) == (carousel.SIDE, carousel.SIDE)


@pytest.fixture
def database(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'c.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = maker()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    from worker.tasks import publish

    monkeypatch.setattr(publish, "session_scope", scope)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'c.db'}")
    # The scope, not the maker: a bare Session used as a context manager
    # closes without committing, so rows written through one vanish and the
    # test reads an empty table while looking like it set one up.
    return scope


def a_reel(session, pid, posted_minutes_ago=None, carousel_at=None, state="posted"):
    session.add(Reel(
        external_id=pid, permalink=f"https://reddit.com/{pid}", caption="lift",
        state=state, ups=100,
        posted_at=(datetime.now(UTC) - timedelta(minutes=posted_minutes_ago))
        if posted_minutes_ago is not None else None,
        carousel_at=carousel_at,
    ))


class TestHalfAnHourBehindTheReel:
    def test_one_posted_forty_minutes_ago_is_owed_a_carousel(self, database):
        from worker.tasks.publish import carousel_owed

        with database() as session:
            a_reel(session, "ready", posted_minutes_ago=40)
        assert len(carousel_owed()) == 1

    def test_one_posted_ten_minutes_ago_is_not(self, database):
        from worker.tasks.publish import carousel_owed

        with database() as session:
            a_reel(session, "toosoon", posted_minutes_ago=10)
        assert carousel_owed() == []

    def test_one_that_already_has_a_carousel_is_not_owed_another(self, database):
        from worker.tasks.publish import carousel_owed

        with database() as session:
            a_reel(session, "done", posted_minutes_ago=90,
                   carousel_at=datetime.now(UTC))
        assert carousel_owed() == []

    def test_a_reel_that_never_went_out_is_not_owed_one(self, database):
        """The carousel follows the reel. Posting it for something that was
        never posted would put the second half of a pair on its own."""
        from worker.tasks.publish import carousel_owed

        with database() as session:
            a_reel(session, "waiting", state="found")
        assert carousel_owed() == []

    def test_a_backlog_drains_oldest_first(self, database):
        """Newest-first would leave the oldest owed forever."""
        from worker.tasks.publish import carousel_owed

        with database() as session:
            a_reel(session, "old", posted_minutes_ago=300)
            a_reel(session, "newer", posted_minutes_ago=60)

        owed = carousel_owed()
        with database() as session:
            first = session.get(Reel, owed[0])
            assert first.external_id == "old"

    def test_the_delay_is_configurable(self, database, monkeypatch):
        from worker.tasks.publish import carousel_owed

        with database() as session:
            a_reel(session, "r", posted_minutes_ago=20)
        assert carousel_owed() == []
        monkeypatch.setattr(settings, "carousel_delay_minutes", 15)
        assert len(carousel_owed()) == 1


class TestWhenItWillNotGo:
    def test_it_is_off_when_the_switch_is_off(self, database, monkeypatch):
        from worker.tasks.publish import post_carousels_due

        monkeypatch.setattr(settings, "carousel_enabled", False)
        assert "CAROUSEL_ENABLED" in post_carousels_due()["skipped"]

    def test_autopost_still_gates_it(self, database, monkeypatch):
        from worker.tasks.publish import post_carousels_due

        monkeypatch.setattr(settings, "carousel_enabled", True)
        monkeypatch.setattr(settings, "autopost_enabled", False)
        assert "AUTOPOST_ENABLED" in post_carousels_due()["skipped"]

    def test_it_needs_instagram_specifically(self, database, monkeypatch):
        """A carousel is an Instagram object. Threads and Facebook having
        credentials does not make one possible."""
        from worker.tasks.publish import post_carousels_due

        monkeypatch.setattr(settings, "carousel_enabled", True)
        monkeypatch.setattr(settings, "autopost_enabled", True)
        monkeypatch.setattr(settings, "instagram_user_id", None)
        monkeypatch.setattr(settings, "publisher", "meta")
        assert "Instagram" in post_carousels_due()["skipped"]

    def test_a_failure_leaves_it_owed_rather_than_marking_it_done(
            self, database, monkeypatch):
        """Writing the timestamp on failure would mean a storage blip costs
        that reel its carousel permanently."""
        from worker.tasks import publish

        with database() as session:
            a_reel(session, "r", posted_minutes_ago=60)
        owed = publish.carousel_owed()
        publish._carousel_failed(owed[0], "storage was unreachable")

        later = datetime.now(UTC) + timedelta(hours=2)
        assert publish.carousel_owed(now=later) == [owed[0]], "it must still be owed"
        with database() as session:
            assert "storage" in session.get(Reel, owed[0]).carousel_note

    def test_it_is_not_retried_a_minute_later(self, database):
        """The heartbeat ticks every sixty seconds. Without a wait, one broken
        render becomes forty identical failures before anyone looks."""
        from worker.tasks import publish

        with database() as session:
            a_reel(session, "r", posted_minutes_ago=60)
        publish._carousel_failed(publish.carousel_owed()[0], "ffmpeg said no")

        assert publish.carousel_owed() == [], "it should be waiting, not retrying"

    def test_each_failure_waits_longer_than_the_last(self, database):
        from worker.tasks import publish

        with database() as session:
            a_reel(session, "r", posted_minutes_ago=60)
        reel_id = publish.carousel_owed()[0]

        publish._carousel_failed(reel_id, "no")
        # 15 minutes after one failure: due again.
        assert publish.carousel_owed(
            now=datetime.now(UTC) + timedelta(minutes=16)) == [reel_id]

        publish._carousel_failed(reel_id, "no")
        # After two, the wait is thirty, so sixteen minutes is not enough.
        assert publish.carousel_owed(
            now=datetime.now(UTC) + timedelta(minutes=16)) == []
        assert publish.carousel_owed(
            now=datetime.now(UTC) + timedelta(minutes=31)) == [reel_id]

    def test_it_gives_up_rather_than_trying_forever(self, database, monkeypatch):
        from worker.tasks import publish

        monkeypatch.setattr(settings, "carousel_max_attempts", 3)
        with database() as session:
            a_reel(session, "r", posted_minutes_ago=60)
        reel_id = publish.carousel_owed()[0]

        for _ in range(3):
            publish._carousel_failed(reel_id, "ffmpeg said no")

        far_future = datetime.now(UTC) + timedelta(days=7)
        assert publish.carousel_owed(now=far_future) == []
        with database() as session:
            assert "gave up after 3" in session.get(Reel, reel_id).carousel_note

    def test_the_note_says_which_attempt_it_is_on(self, database):
        from worker.tasks import publish

        with database() as session:
            a_reel(session, "r", posted_minutes_ago=60)
        reel_id = publish.carousel_owed()[0]
        publish._carousel_failed(reel_id, "ffmpeg said no")

        with database() as session:
            note = session.get(Reel, reel_id).carousel_note
        assert note.startswith("attempt 1 of ")
        assert "ffmpeg said no" in note

    def test_local_storage_is_refused_before_anything_is_rendered(
            self, database, monkeypatch):
        """Instagram fetches each slide from a URL, and local storage hands
        back a file:// one it cannot possibly read. Discovering that after
        rendering two videos, once a minute, is the expensive way to find out."""
        from worker.tasks import publish

        monkeypatch.setattr(settings, "carousel_enabled", True)
        monkeypatch.setattr(settings, "autopost_enabled", True)
        monkeypatch.setattr(settings, "instagram_user_id", "1784")
        monkeypatch.setattr(settings, "meta_access_token", "EAA")
        monkeypatch.setattr(settings, "publisher", "meta")
        monkeypatch.setattr(settings, "r2_bucket", None)

        assert "R2" in publish.post_carousels_due()["skipped"]


class TestTheCarouselApiShape:
    """Instagram's carousel flow is not a variation on the reel flow, and the
    two ways of getting it wrong both fail in ways that read as something
    else entirely."""

    def test_each_slide_is_marked_as_a_carousel_item(self):
        import inspect

        from core.publishers.meta import MetaPublisher
        source = inspect.getsource(MetaPublisher.publish_carousel)
        assert '"is_carousel_item": "true"' in source

    def test_the_video_slide_is_not_a_reel(self):
        """A REELS container cannot be a carousel item, and asking for one is
        refused in a way that reads like a bad URL."""
        import inspect

        from core.publishers.meta import MetaPublisher
        source = inspect.getsource(MetaPublisher.publish_carousel)
        # The body, not the docstring - which mentions REELS precisely to say
        # it must not be used.
        body = source.split('"""')[-1]
        assert '"media_type": "VIDEO"' in body
        assert "REELS" not in body

    def test_the_children_are_tied_together_before_publishing(self):
        import inspect

        from core.publishers.meta import MetaPublisher
        source = inspect.getsource(MetaPublisher.publish_carousel)
        assert '"media_type": "CAROUSEL"' in source
        assert '"children"' in source
        assert "media_publish" in source


class TestARateLimitIsAWaitNotAFailure:
    """Meta's code 4 / 1349210 means "stop asking for a while", not "this is
    broken". The two need completely different handling: a broken render fails
    the same way forever and should be given up on, while a rate limit clears
    by itself the moment the traffic stops.

    This matters because the retries were what spent the quota in the first
    place. Counting each refusal as an attempt would have abandoned every
    reel's second post over a limit that refills on its own.
    """

    @pytest.mark.parametrize("said", [
        "Application request limit reached (code 4/1349210)",
        "Application request limit reached (code 4)",
        "User request limit reached (code 17)",
        "Please retry your request later (code 2)",
    ])
    def test_metas_ways_of_saying_slow_down_are_recognised(self, said):
        from core.publishers.meta import is_rate_limited

        assert is_rate_limited(said)

    @pytest.mark.parametrize("said", [
        "Invalid parameter (code 100/2207026)",
        "could not build the slides: ffmpeg said no",
        "The access token is invalid (code 190)",
        None,
        "",
    ])
    def test_a_real_fault_is_not_mistaken_for_one(self, said):
        from core.publishers.meta import is_rate_limited

        assert not is_rate_limited(said)

    def test_it_does_not_spend_an_attempt(self, database):
        """Four rate limits would otherwise use up the whole give-up budget
        and abandon the post permanently."""
        from worker.tasks import publish

        with database() as session:
            a_reel(session, "r", posted_minutes_ago=60)
        reel_id = publish.carousel_owed()[0]

        for _ in range(6):
            publish._carousel_failed(
                reel_id, "Application request limit reached (code 4/1349210)")

        with database() as session:
            reel = session.get(Reel, reel_id)
        assert reel.carousel_attempts == 0, "a rate limit is not an attempt"
        assert "waiting" in reel.carousel_note

    def test_it_is_still_owed_once_the_limit_has_had_time_to_clear(self, database):
        from worker.tasks import publish

        with database() as session:
            a_reel(session, "r", posted_minutes_ago=60)
        reel_id = publish.carousel_owed()[0]
        publish._carousel_failed(
            reel_id, "Application request limit reached (code 4/1349210)")

        assert publish.carousel_owed() == [], "asking again is what spent it"
        later = datetime.now(UTC) + timedelta(
            minutes=settings.rate_limit_wait_minutes + 1)
        assert publish.carousel_owed(now=later) == [reel_id]

    def test_it_waits_far_longer_than_an_ordinary_failure(self):
        """A minute of backoff against a 24-hour window is no backoff."""
        assert settings.rate_limit_wait_minutes >= settings.carousel_retry_minutes * 2

    def test_a_real_fault_still_counts_and_still_gives_up(self, database):
        from worker.tasks import publish

        with database() as session:
            a_reel(session, "r", posted_minutes_ago=60)
        reel_id = publish.carousel_owed()[0]
        publish._carousel_failed(reel_id, "could not build the slides: ffmpeg said no")

        with database() as session:
            assert session.get(Reel, reel_id).carousel_attempts == 1
