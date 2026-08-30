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
