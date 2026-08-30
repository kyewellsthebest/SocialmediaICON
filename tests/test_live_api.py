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
        livestate.publish({"running": True, "streams": []})
        monkeypatch.setattr(settings, "live_enabled", True)
        assert live_watch.ensure_running()["reason"] == "already running"

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
