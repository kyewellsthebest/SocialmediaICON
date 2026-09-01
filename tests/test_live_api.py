"""Start has to actually start something, and status has to cross processes.

Both bugs this file exists for shipped and produced "Internal Server Error"
on the one button that matters:

  enqueue() takes the queue name first, and was called with one argument.
  The API read the supervisor from a module global in the *web* process while
  it lives in the *worker* one, so status could never be seen at all.

Neither was visible to a test that only imported the modules.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import livestate
from core.config import settings


@pytest.fixture(autouse=True)
def clean():
    livestate._fallback.clear()
    yield
    livestate._fallback.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _auth() -> dict[str, str]:
    return {"X-Dashboard-Token": settings.dashboard_token or ""}


class TestStarting:
    def test_start_enqueues_with_the_queue_name_first(self, client, monkeypatch):
        """The signature is enqueue(queue, func) - one argument is a TypeError."""
        seen = {}

        def fake(queue, func, *args, **kwargs):
            seen["queue"], seen["func"], seen["kwargs"] = queue, func, kwargs
            return type("Job", (), {"id": "abc"})()

        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        monkeypatch.setattr("api.routes.live.enqueue", fake)

        body = client.post("/api/live/start", headers=_auth()).json()
        assert body["ok"] is True
        assert seen["queue"] == "live", "the first argument is the queue, not the path"
        assert seen["func"] == "worker.tasks.live_watch.run"

    def test_the_job_timeout_outlives_a_long_watch(self, client, monkeypatch):
        """The default hour would kill the watcher mid-stream."""
        seen = {}
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        monkeypatch.setattr(
            "api.routes.live.enqueue",
            lambda q, f, **kw: seen.update(kw) or type("Job", (), {"id": "x"})(),
        )
        client.post("/api/live/start", headers=_auth())
        assert seen.get("job_timeout", 0) >= 6 * 3600

    def test_the_live_queue_exists(self):
        from worker.queue import QUEUE_NAMES

        assert "live" in QUEUE_NAMES, "enqueue rejects a queue name it does not know"

    def test_starting_without_redis_says_so_rather_than_500ing(self, client, monkeypatch):
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", None)
        response = client.post("/api/live/start", headers=_auth())
        assert response.status_code == 503
        assert "REDIS_URL" in response.json()["detail"]

    def test_starting_while_disabled_is_refused_clearly(self, client, monkeypatch):
        monkeypatch.setattr(settings, "live_enabled", False)
        assert client.post("/api/live/start", headers=_auth()).status_code == 400

    def test_starting_twice_does_not_queue_a_second_watcher(self, client, monkeypatch):
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        monkeypatch.setattr(settings, "redis_url", None, raising=False)
        # With no redis client the fallback carries the snapshot.
        livestate.publish({"running": True, "streams": []})
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        calls = []
        monkeypatch.setattr("api.routes.live.enqueue", lambda *a, **k: calls.append(1))
        body = client.post("/api/live/start", headers=_auth()).json()
        assert body.get("already_running") is True
        assert not calls


class TestStatusCrossesProcesses:
    def test_a_snapshot_published_elsewhere_is_what_the_page_shows(self, client):
        livestate.publish({"running": True, "slots": 3, "streams": [{"channel": "x"}]})
        body = client.get("/api/live", headers=_auth()).json()
        assert body["running"] is True
        assert body["streams"][0]["channel"] == "x"

    def test_a_stale_snapshot_is_not_reported_as_live(self, client):
        """A worker that died must not leave three streams on screen forever."""
        livestate.publish({"running": True, "streams": [{"channel": "x"}]})
        livestate._fallback["status"]["published_at"] = time.time() - 600
        body = client.get("/api/live", headers=_auth()).json()
        assert body["running"] is False
        assert body["streams"] == []

    def test_nothing_published_reads_as_idle_with_a_hint(self, client, monkeypatch):
        monkeypatch.setattr(settings, "live_enabled", False)
        body = client.get("/api/live", headers=_auth()).json()
        assert body["running"] is False
        assert "LIVE_ENABLED" in body["hint"]

    def test_the_idle_shape_matches_the_running_shape(self, client, monkeypatch):
        """The page reads the same keys either way; a missing one is a crash."""
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", None)
        body = client.get("/api/live", headers=_auth()).json()
        for key in ("running", "slots", "posting_enabled", "caps", "streams", "errors"):
            assert key in body


class TestStopping:
    def test_stop_is_recorded_where_the_other_process_will_see_it(self, client):
        assert livestate.stop_requested() is False
        client.post("/api/live/stop", headers=_auth())
        assert livestate.stop_requested() is True

    def test_clear_forgets_both_flags(self):
        livestate.publish({"running": True})
        livestate.request_stop()
        livestate.clear()
        assert livestate.read() is None
        assert livestate.stop_requested() is False


class TestNoSilentFailures:
    """Pressing Start and seeing nothing happen is the worst outcome.

    The first live attempt did exactly that: the job queued, the worker
    refused it because LIVE_ENABLED was not set on that service, and the
    refusal was returned as a dict nobody reads. From the dashboard it was
    indistinguishable from a dead button.
    """

    def test_queueing_claims_the_state_so_the_page_can_say_so(self, client, monkeypatch):
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        monkeypatch.setattr(
            "api.routes.live.enqueue", lambda *a, **k: type("Job", (), {"id": "x"})()
        )
        client.post("/api/live/start", headers=_auth())

        body = client.get("/api/live", headers=_auth()).json()
        assert body["queued"] is True
        assert body["running"] is False
        assert "worker" in body["hint"], "the hint has to name what to check"

    def test_a_queue_that_silently_did_nothing_is_an_error(self, client, monkeypatch):
        """enqueue() returns None when Redis is missing; that is not success."""
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        monkeypatch.setattr("api.routes.live.enqueue", lambda *a, **k: None)
        assert client.post("/api/live/start", headers=_auth()).status_code == 503

    def test_the_worker_refusing_reaches_the_page(self, monkeypatch):
        from worker.tasks import live_watch

        monkeypatch.setattr(settings, "live_enabled", False)
        result = live_watch.run()
        assert result["ok"] is False

        found = livestate.read()
        assert found is not None, "a refusal that publishes nothing is invisible"
        assert "LIVE_ENABLED" in found["hint"]
        assert "worker service" in found["hint"]

    def test_the_worker_publishes_before_it_does_slow_work(self, monkeypatch):
        """Three playback URLs and three buffers take most of a minute."""
        from worker.tasks import live_watch

        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(live_watch, "_current", None)
        seen: list[dict] = []
        monkeypatch.setattr(livestate, "publish", lambda s: seen.append(s))
        monkeypatch.setattr(livestate, "stop_requested", lambda: True)
        monkeypatch.setattr("core.supervisor.Supervisor.tick", lambda self, **k: [])
        monkeypatch.setattr("core.supervisor.Supervisor.poll_roster", lambda self, **k: {})

        live_watch.run(max_seconds=0)
        assert seen, "nothing was published at all"
        assert seen[0]["running"] is True
        assert "Attaching" in seen[0]["hint"]

    def test_a_crash_inside_the_loop_reaches_the_page(self, monkeypatch):
        from worker.tasks import live_watch

        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(live_watch, "_current", None)
        monkeypatch.setattr(
            "core.supervisor.Supervisor.poll_roster",
            lambda self, **k: (_ for _ in ()).throw(RuntimeError("kick said no")),
        )
        monkeypatch.setattr(
            "core.supervisor.Supervisor.tick",
            lambda self, **k: (_ for _ in ()).throw(RuntimeError("kick said no")),
        )
        result = live_watch.run(max_seconds=0)
        assert result["ok"] is False
        found = livestate.read()
        assert found and "kick said no" in found["hint"]


class TestTheDebugEndpoint:
    """Four different faults look identical from the dashboard."""

    def test_no_redis_is_named_outright(self, client, monkeypatch):
        monkeypatch.setattr(settings, "redis_url", None)
        body = client.get("/api/live/debug", headers=_auth()).json()
        assert "No Redis" in body["verdict"]
        assert body["web"]["has_redis"] is False

    def test_it_never_500s_when_the_queue_is_unreachable(self, client, monkeypatch):
        monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
        response = client.get("/api/live/debug", headers=_auth())
        assert response.status_code == 200, "a diagnostic that crashes diagnoses nothing"
        assert "verdict" in response.json()

    def test_it_reports_the_web_side_settings(self, client, monkeypatch):
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", None)
        body = client.get("/api/live/debug", headers=_auth()).json()
        assert body["web"]["live_enabled"] is True
        assert body["web"]["slots"] == settings.live_slots


class TestOneStreamAtATime:
    """The Live page is three summaries; each stream has its own page."""

    def _snapshot(self):
        livestate.publish({
            "running": True, "slots": 3, "streams": [
                {"channel": "n3on", "name": "n3on", "viewers": 33890,
                 "avatar": "https://files.kick.com/a.webp",
                 "audio": {"ok": True, "loudness_db": [-30, -20, -10], "has_spectrogram": True},
                 "chat": {"per_minute": 84.6}},
                {"channel": "deenthegreat", "name": "DeenTheGreat", "viewers": 17464,
                 "audio": {"ok": False, "why": "the buffer is still filling"}, "chat": {}},
            ],
        })

    def test_a_watched_stream_returns_everything_about_itself(self, client):
        self._snapshot()
        body = client.get("/api/live/streams/n3on", headers=_auth()).json()
        assert body["viewers"] == 33890
        assert body["avatar"].endswith(".webp")
        assert body["audio"]["ok"] is True

    def test_the_lookup_is_case_insensitive(self, client):
        self._snapshot()
        assert client.get("/api/live/streams/DeenTheGreat", headers=_auth()).status_code == 200

    def test_a_stream_not_being_watched_is_a_404(self, client):
        self._snapshot()
        response = client.get("/api/live/streams/nobody", headers=_auth())
        assert response.status_code == 404
        assert "not being watched" in response.json()["detail"]

    def test_the_spectrogram_travels_from_the_worker(self, client):
        """It is drawn on the worker's disk and served by the web process."""
        livestate.put_image("spectrogram:n3on", b"\x89PNG\r\n\x1a\nfake")
        response = client.get("/api/live/streams/n3on/spectrogram", headers=_auth())
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG")

    def test_a_stale_picture_of_a_live_stream_is_not_cached(self, client):
        livestate.put_image("spectrogram:n3on", b"\x89PNGx")
        response = client.get("/api/live/streams/n3on/spectrogram", headers=_auth())
        assert response.headers.get("cache-control") == "no-store"

    def test_no_spectrogram_yet_says_so(self, client):
        response = client.get("/api/live/streams/nothing/spectrogram", headers=_auth())
        assert response.status_code == 404
        assert "twenty seconds" in response.json()["detail"]


class TestTheClipHasToBeReachable:
    """The first real catch was recorded perfectly and could not be played.

    Clips are cut on the worker and watched in a browser talking to the web
    service - different containers, different disks. A path is a note about a
    file, not a way to hand one over.
    """

    def test_a_clip_held_in_redis_round_trips(self):
        livestate.put_image("clip:deen.mp4", b"\x00\x00\x00\x18ftypmp42", ttl_s=60)
        assert livestate.get_image("clip:deen.mp4", max_age_s=60) is not None

    def test_an_expired_clip_says_so_rather_than_pretending_it_never_existed(
        self, monkeypatch
    ):
        from fastapi import HTTPException

        from api.routes import live as route

        monkeypatch.setattr("core.livestate.get_image", lambda *a, **k: None)

        class Row:
            storage_key = "redis:gone.mp4"

        class DB:
            def get(self, *_a):
                return Row()

        with pytest.raises(HTTPException) as caught:
            route.video(1, db=DB())
        assert caught.value.status_code == 410
        assert "expired" in caught.value.detail

    def test_a_local_path_explains_the_two_container_problem(self, monkeypatch):
        from fastapi import HTTPException

        from api.routes import live as route

        monkeypatch.setattr(settings, "r2_bucket", None, raising=False)

        class Row:
            storage_key = "/nowhere/on/this/disk.mp4"

        class DB:
            def get(self, *_a):
                return Row()

        with pytest.raises(HTTPException) as caught:
            route.video(1, db=DB())
        assert "worker" in caught.value.detail
        assert "R2" in caught.value.detail

    def test_playability_covers_all_three_homes(self, monkeypatch):
        from api.routes.live import _playable

        class Row:
            storage_key = ""

        assert _playable(Row()) is False

        Row.storage_key = "redis:missing.mp4"
        monkeypatch.setattr("core.livestate.get_image", lambda *a, **k: None)
        assert _playable(Row()) is False

        monkeypatch.setattr("core.livestate.get_image", lambda *a, **k: b"x")
        assert _playable(Row()) is True


class TestItKeepsWatchingByItself:
    """Pressing Start after every deploy is not a watcher.

    Running is a fact about now; wanted is an intention that has to outlive a
    deploy, a crash and an OOM kill. Only pressing Stop is a decision to stop.
    """

    def test_watching_is_the_default_before_anything_is_pressed(self):
        assert livestate.wanted() is True

    def test_start_records_the_intent(self, client, monkeypatch):
        livestate.want(False)
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        monkeypatch.setattr(
            "api.routes.live.enqueue", lambda *a, **k: type("Job", (), {"id": "x"})()
        )
        client.post("/api/live/start", headers=_auth())
        assert livestate.wanted() is True

    def test_stop_records_it_too_so_it_stays_stopped(self, client):
        livestate.want(True)
        client.post("/api/live/stop", headers=_auth())
        assert livestate.wanted() is False

    def test_a_run_that_ends_on_its_own_queues_the_next_one(self, monkeypatch):
        from worker.tasks import live_watch

        livestate.want(True)
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        queued = []
        monkeypatch.setattr("worker.queue.enqueue", lambda *a, **k: queued.append(a))
        assert live_watch.relaunch("after a crash") is True
        assert queued and queued[0][0] == "live"

    def test_a_run_that_was_stopped_on_purpose_does_not(self, monkeypatch):
        from worker.tasks import live_watch

        livestate.want(False)
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        queued = []
        monkeypatch.setattr("worker.queue.enqueue", lambda *a, **k: queued.append(a))
        assert live_watch.relaunch("after a crash") is False
        assert not queued

    def test_a_booting_worker_resumes_the_watch(self, monkeypatch):
        from worker.tasks import live_watch

        livestate.want(True)
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        queued = []
        monkeypatch.setattr("worker.queue.enqueue", lambda *a, **k: queued.append(a))
        assert live_watch.ensure_running()["ok"] is True
        assert queued

    def test_a_booting_worker_respects_stop(self, monkeypatch):
        from worker.tasks import live_watch

        livestate.want(False)
        monkeypatch.setattr(settings, "live_enabled", True)
        found = live_watch.ensure_running()
        assert found["ok"] is False
        assert "Stop" in found["reason"]

    def test_it_does_not_start_a_second_watcher_over_a_live_one(self, monkeypatch):
        from worker.tasks import live_watch

        livestate.want(True)
        # The lease, not the snapshot: the snapshot is a reading and is allowed
        # to go stale for a minute and a half while a pass runs, so a booting
        # worker that consulted it would refuse to restart a watcher that had
        # actually died - and would also refuse for ninety seconds after one
        # that really is running published.
        livestate.take_lease("the-live-one")
        monkeypatch.setattr(settings, "live_enabled", True)
        assert live_watch.ensure_running()["reason"] == "already running"

    def test_a_stale_snapshot_does_not_stop_a_boot_resuming(self, monkeypatch):
        """A watcher killed mid-pass leaves its last reading behind. That is a
        reading, not a heartbeat, and treating it as one is how a crash goes
        unnoticed until the snapshot expires."""
        from worker.tasks import live_watch

        livestate.want(True)
        livestate.publish({"running": True, "streams": []})
        monkeypatch.setattr(settings, "live_enabled", True)
        queued = []
        monkeypatch.setattr(live_watch, "relaunch", lambda why: queued.append(why) or True)
        assert live_watch.ensure_running()["ok"] is True
        assert queued

    def test_the_page_can_tell_restarting_from_stopped(self, client, monkeypatch):
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", None)
        livestate.want(True)
        body = client.get("/api/live", headers=_auth()).json()
        assert "wanted" in body


class TestWhyThereIsNoVideo:
    """Three different problems shared one message, and only one is actionable."""

    def _row(self, key):
        return type("Row", (), {"storage_key": key})()

    def test_a_pre_fix_clip_says_the_file_only_lived_on_the_worker(self, monkeypatch):
        from api.routes.live import _video_state

        monkeypatch.setattr(settings, "r2_bucket", None, raising=False)
        ok, note = _video_state(self._row("/app/.work/catches/deen.mp4"))
        assert ok is False
        assert "only ever existed on the worker" in note
        assert "Newer clips are fine" in note

    def test_an_expired_review_clip_says_so_and_points_at_r2(self, monkeypatch):
        from api.routes.live import _video_state

        monkeypatch.setattr("core.livestate.get_image", lambda *a, **k: None)
        ok, note = _video_state(self._row("redis:deen.mp4"))
        assert ok is False
        assert "expired" in note and "R2" in note

    def test_a_clip_still_held_for_review_plays(self, monkeypatch):
        from api.routes.live import _video_state

        monkeypatch.setattr("core.livestate.get_image", lambda *a, **k: b"mp4")
        assert _video_state(self._row("redis:deen.mp4")) == (True, "")

    def test_an_r2_key_plays_when_r2_is_configured(self, monkeypatch):
        from api.routes.live import _video_state

        for key, value in dict(
            r2_account_id="a", r2_access_key_id="b",
            r2_secret_access_key="c", r2_bucket="d",
        ).items():
            monkeypatch.setattr(settings, key, value, raising=False)
        assert _video_state(self._row("catches/deen.mp4")) == (True, "")

    def test_an_r2_key_without_r2_configured_says_which_way_round(self, monkeypatch):
        from api.routes.live import _video_state

        monkeypatch.setattr(settings, "r2_bucket", None, raising=False)
        ok, note = _video_state(self._row("catches/deen.mp4"))
        assert ok is False
        assert "not configured on this service" in note

    def test_nothing_stored_is_its_own_case(self):
        from api.routes.live import _video_state

        assert _video_state(self._row(""))[0] is False


class TestItSaysWhyItIsNotRunning:
    """The screenshot this exists for: "RESTARTING" over an empty page.

    Once the snapshot expires - thirty seconds - "restarting" was the only
    thing the page could say, and it said it forever. It says it for a job
    queued behind no worker, for a worker missing LIVE_ENABLED, and for a run
    that died without relaunching, and all three need different fixes.
    """

    def _no_snapshot(self, monkeypatch, verdict, queue=None):
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        monkeypatch.setattr(
            "api.routes.live._queue_view",
            lambda **k: {"verdict": verdict, "queue": queue},
        )

    def test_the_queue_verdict_reaches_the_page(self, client, monkeypatch):
        self._no_snapshot(monkeypatch, "No worker is listening on the 'live' queue.")
        body = client.get("/api/live", headers=_auth()).json()
        assert "No worker is listening" in body["diagnosis"]
        assert body["hint"] == body["diagnosis"]

    def test_a_note_the_worker_left_outlives_its_snapshot(self, client, monkeypatch):
        self._no_snapshot(monkeypatch, "Nothing queued, nothing running.")
        livestate.note("LIVE_ENABLED is not set on the worker service.")
        body = client.get("/api/live", headers=_auth()).json()
        assert "LIVE_ENABLED is not set on the worker" in body["hint"]
        assert body["noted_at"] > 0

    def test_a_run_that_starts_clears_the_old_explanation(self):
        livestate.note("something went wrong")
        livestate.clear_note()
        assert livestate.last_note() is None

    def test_the_shape_is_still_the_one_the_page_reads(self, client, monkeypatch):
        self._no_snapshot(monkeypatch, "anything")
        body = client.get("/api/live", headers=_auth()).json()
        for key in ("running", "slots", "posting_enabled", "caps", "streams", "errors"):
            assert key in body


class TestItPutsItselfBackOnTheQueue:
    """An out-of-memory kill takes the process without running the relaunch."""

    def _stuck(self, monkeypatch, queue, calls):
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        # There is no Redis here, and a claim that cannot be written must not
        # be granted in production - so the in-process store stands in for it.
        monkeypatch.setattr("core.livestate._redis", lambda: None)
        monkeypatch.setattr(
            "api.routes.live._queue_view", lambda **k: {"verdict": "", "queue": queue}
        )
        monkeypatch.setattr(
            "api.routes.live.enqueue",
            lambda *a, **k: calls.append(1) or type("Job", (), {"id": "j"})(),
        )

    def _idle_queue(self, workers=1):
        return {
            "waiting": 0, "started": 0, "failed": 0,
            "workers_listening_on_live": [{"name": f"w{i}"} for i in range(workers)],
        }

    def test_a_watch_that_is_wanted_but_gone_is_requeued(self, client, monkeypatch):
        calls = []
        self._stuck(monkeypatch, self._idle_queue(), calls)
        body = client.get("/api/live", headers=_auth()).json()
        assert body["requeued"] is True
        assert len(calls) == 1

    def test_it_does_not_queue_a_second_copy_on_the_next_poll(self, client, monkeypatch):
        """The page polls every five seconds. A stall must not become a backlog."""
        calls = []
        self._stuck(monkeypatch, self._idle_queue(), calls)
        for _ in range(6):
            client.get("/api/live", headers=_auth())
        assert len(calls) == 1

    def test_a_job_already_waiting_is_left_alone(self, client, monkeypatch):
        calls = []
        queue = self._idle_queue()
        queue["waiting"] = 1
        self._stuck(monkeypatch, queue, calls)
        client.get("/api/live", headers=_auth())
        assert not calls

    def test_a_job_already_running_is_left_alone(self, client, monkeypatch):
        calls = []
        queue = self._idle_queue()
        queue["started"] = 1
        self._stuck(monkeypatch, queue, calls)
        client.get("/api/live", headers=_auth())
        assert not calls

    def test_nothing_is_queued_when_no_worker_could_take_it(self, client, monkeypatch):
        """Queueing into a queue nobody reads just builds a pile of jobs."""
        calls = []
        self._stuck(monkeypatch, self._idle_queue(workers=0), calls)
        client.get("/api/live", headers=_auth())
        assert not calls

    def test_stop_means_stop(self, client, monkeypatch):
        calls = []
        self._stuck(monkeypatch, self._idle_queue(), calls)
        livestate.want(False)
        client.get("/api/live", headers=_auth())
        assert not calls


class TestTheClaim:
    def test_only_the_first_caller_in_the_window_gets_it(self):
        assert livestate.claim("x", seconds=60) is True
        assert livestate.claim("x", seconds=60) is False

    def test_a_different_name_is_a_different_claim(self):
        assert livestate.claim("a", seconds=60) is True
        assert livestate.claim("b", seconds=60) is True


class TestItKeepsWatchingWithoutBeingAskedTo:
    """"Running" and "watching" were the same claim, and neither was checked
    by anything. A job lost by the queue, a relaunch that failed on a Redis
    blink, an out-of-memory kill of the job but not the worker: each leaves a
    live worker process with nothing watching, no error anywhere, and nobody
    told until somebody opens the page in the morning."""

    def test_the_lease_is_renewed_every_tick_and_given_back_after(self, monkeypatch):
        """Taking it once is not enough. It expires in five minutes and the
        watcher runs for days, so a watcher that stops renewing looks dead to
        the watchdog and gets a second one started on top of it."""
        from worker.tasks import live_watch

        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(live_watch, "_current", None)
        monkeypatch.setattr(livestate, "stop_requested", lambda: True)

        alive: list[bool] = []

        def tick(self, **k):
            # However long the last tick took, the lease has to be good again.
            # Recorded rather than asserted: the loop catches every exception
            # a tick raises, so an assert in here would be swallowed and the
            # test would pass against code that never renews anything.
            alive.append(livestate.watcher_alive())
            return []

        # Age the lease past its life the moment it is taken, so only a
        # renewal inside the loop can make watcher_alive() true.
        real_take = livestate.take_lease

        def take_and_age(holder):
            got = real_take(holder)
            livestate._fallback["lease"] = (holder, time.time() - livestate.LEASE_S - 1)
            return got

        monkeypatch.setattr(livestate, "take_lease", take_and_age)
        monkeypatch.setattr("core.supervisor.Supervisor.tick", tick)
        monkeypatch.setattr("core.supervisor.Supervisor.poll_roster", lambda self, **k: {})

        live_watch.run(max_seconds=0)
        assert alive == [True], "the lease lapsed while the watcher was working"
        assert not livestate.watcher_alive(), "a clean exit must hand the lease back"

    def test_a_second_watcher_will_not_start_next_to_a_live_one(self, monkeypatch):
        from worker.tasks import live_watch

        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(live_watch, "_current", None)
        # It waits for a lease now rather than refusing on sight, so the wait
        # is shortened here. What is under test is that it still gives up
        # rather than starting a second watcher beside a live one.
        monkeypatch.setattr(live_watch, "LEASE_WAIT_S", 0.2)
        monkeypatch.setattr(live_watch, "LEASE_POLL_S", 0.05)
        livestate.take_lease("somebody-else")

        result = live_watch.run(max_seconds=0)
        assert result["ok"] is False
        assert "lease" in result["reason"]


class TestADeployDoesNotCostFiveMinutesOfWatching:
    """Railway stops a container with SIGKILL, so the release in the watcher's
    `finally` does not run on a deploy and the lease sits in Redis until its
    five-minute TTL.

    Starting up, the watcher used to see that lease, return immediately, and
    leave the page saying "Another watcher already holds the lease; leaving it
    alone" - which reads as a permanent refusal. It was not permanent: the
    watchdog re-queued every sixty seconds and one of those eventually landed
    after the TTL. But it was five minutes of not watching after every deploy,
    and nothing on the page said it would recover on its own."""

    def test_it_waits_for_a_lease_that_is_about_to_expire(self, monkeypatch):
        from worker.tasks import live_watch

        monkeypatch.setattr(live_watch, "LEASE_WAIT_S", 5.0)
        monkeypatch.setattr(live_watch, "LEASE_POLL_S", 0.02)
        # A lease left behind by a container that was killed: nobody is
        # renewing it, so it lapses on its own a moment from now.
        livestate.take_lease("the-killed-one")
        livestate._fallback["lease"] = (
            "the-killed-one", time.time() - livestate.LEASE_S + 0.1)

        assert live_watch._claim("the-new-one") is True

    def test_it_gives_up_on_a_lease_that_keeps_being_renewed(self, monkeypatch):
        """A watcher that is actually alive holds its lease indefinitely, and
        a second one must never start beside it."""
        from worker.tasks import live_watch

        monkeypatch.setattr(live_watch, "LEASE_WAIT_S", 0.2)
        monkeypatch.setattr(live_watch, "LEASE_POLL_S", 0.05)
        livestate.take_lease("the-live-one")

        assert live_watch._claim("the-new-one") is False

    def test_a_free_lease_is_taken_without_waiting(self, monkeypatch):
        from worker.tasks import live_watch

        monkeypatch.setattr(live_watch, "LEASE_WAIT_S", 30.0)
        started = time.time()
        assert live_watch._claim("the-only-one") is True
        assert time.time() - started < 1.0, "it waited when nothing held it"

    def test_the_page_is_told_it_is_waiting_rather_than_refusing(self, monkeypatch):
        from worker.tasks import live_watch

        monkeypatch.setattr(live_watch, "LEASE_WAIT_S", 0.2)
        monkeypatch.setattr(live_watch, "LEASE_POLL_S", 0.05)
        livestate.take_lease("the-live-one")
        live_watch._claim("the-new-one")

        note = livestate.last_note() or {}
        assert "waiting" in str(note).lower(), note

    def test_how_long_is_left_can_be_asked(self):
        livestate.take_lease("somebody")
        left = livestate.lease_left_s()
        assert left is not None and 0 < left <= livestate.LEASE_S

    def test_the_watchdog_leaves_a_live_watcher_alone(self, monkeypatch):
        from worker.tasks import live_watch

        monkeypatch.setattr(settings, "live_enabled", True)
        livestate.take_lease("the-real-one")
        queued: list = []
        monkeypatch.setattr(live_watch, "relaunch", lambda why: queued.append(why) or True)

        assert live_watch.watchdog()["ok"] is True
        assert queued == [], "restarting a healthy watcher is how you get two"

    def test_the_watchdog_restarts_a_watcher_that_stopped_saying_anything(
        self, monkeypatch
    ):
        from worker.tasks import live_watch

        monkeypatch.setattr(settings, "live_enabled", True)
        queued: list = []
        monkeypatch.setattr(live_watch, "relaunch", lambda why: queued.append(why) or True)

        found = live_watch.watchdog()
        assert found["ok"] is True
        assert found["restarted"] is True
        assert queued, "nothing was queued, so nothing would ever watch again"

    def test_the_watchdog_respects_stop(self, monkeypatch):
        from worker.tasks import live_watch

        monkeypatch.setattr(settings, "live_enabled", True)
        livestate.want(False)
        queued: list = []
        monkeypatch.setattr(live_watch, "relaunch", lambda why: queued.append(why) or True)

        assert live_watch.watchdog()["ok"] is False
        assert queued == [], "Stop is a decision, not a fault to be repaired"

    def test_the_watchdog_runs_in_the_scheduler_rather_than_on_a_queue(self):
        """One worker serves every queue one job at a time, and the live job
        never returns - so a queued watchdog would sit behind the thing it
        exists to check on, forever."""
        from worker import scheduler

        monkey = [j for j in scheduler._jobs() if j.name == "live_watchdog"]
        assert monkey, "there is no watchdog at all"
        assert monkey[0].inline is True
        assert monkey[0].every_minutes == 1

    def test_the_live_queue_gets_its_own_worker(self, monkeypatch):
        """Otherwise the hours-long live job starves renders, metrics and
        autopost - they queue behind a job that does not return."""
        from worker import queue as workerq

        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        forked, served = [], []
        monkeypatch.setattr(workerq, "_fork_live_worker", lambda: forked.append(True))
        monkeypatch.setattr(workerq, "get_redis", lambda: object())
        monkeypatch.setattr(workerq, "get_queue", lambda n: served.append(n))

        class _Worker:
            def __init__(self, *a, **k):
                pass

            def work(self, **k):
                pass

        monkeypatch.setitem(__import__("sys").modules, "rq",
                            type("m", (), {"Worker": _Worker}))
        workerq.main([])

        assert forked == [True]
        assert "live" not in served
        assert "render" in served


class TestUnknownIsNotAnAnswer:
    """A Redis that will not answer means the two callers want opposite
    things, and collapsing that into a boolean gets one of them wrong."""

    def _mute(self, monkeypatch):
        class _Dead:
            def get(self, key):
                raise RuntimeError("connection refused")

        monkeypatch.setattr(livestate, "_redis", lambda: _Dead())

    def test_the_lease_says_it_cannot_tell(self, monkeypatch):
        self._mute(monkeypatch)
        assert livestate.watcher_alive() is None

    def test_the_watchdog_leaves_it_alone_rather_than_guessing(self, monkeypatch):
        """Otherwise it queues a relaunch a minute forever, and with Redis
        unreachable not one of them could run."""
        from worker.tasks import live_watch

        self._mute(monkeypatch)
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(livestate, "wanted", lambda default=True: True)
        queued = []
        monkeypatch.setattr(live_watch, "relaunch", lambda why: queued.append(why) or True)
        assert live_watch.watchdog()["ok"] is True
        assert queued == []

    def test_a_booting_worker_tries_anyway(self, monkeypatch):
        """The opposite call: the alternative is a worker that comes up beside
        a dead watcher and decides not to start one."""
        from worker.tasks import live_watch

        self._mute(monkeypatch)
        monkeypatch.setattr(settings, "live_enabled", True)
        monkeypatch.setattr(livestate, "wanted", lambda default=True: True)
        queued = []
        monkeypatch.setattr(live_watch, "relaunch", lambda why: queued.append(why) or True)
        assert live_watch.ensure_running()["ok"] is True
        assert queued
