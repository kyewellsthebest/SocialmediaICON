"""The probe endpoints exist so the answer needs a browser, not a shell.

They run on the deployment, where the network works. What can be tested from
here is the part that matters when it does not: a blocked or failing probe has
to come back as a readable verdict, never a 500 and never a hang.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr("api.deps.require_token", lambda: None)
    return TestClient(app, raise_server_exceptions=False)


def _auth() -> dict[str, str]:
    from core.config import settings

    return {"X-Dashboard-Token": settings.dashboard_token or ""}


class TestKickProbe:
    def test_the_routes_are_registered(self):
        paths = app.openapi()["paths"]
        assert "/api/probe/kick" in paths
        assert "/api/probe/ladder" in paths

    def test_a_probe_that_raises_returns_a_verdict_not_a_500(self, client, monkeypatch):
        def explode(*_args, **_kwargs):
            raise RuntimeError("CONNECT tunnel failed, response 403")

        monkeypatch.setattr("scripts.probe_sources.probe_kick_live", explode)
        response = client.get("/api/probe/kick", params={"channel": "x"}, headers=_auth())
        assert response.status_code == 200, "a dead network is a finding, not a server error"
        body = response.json()
        assert body["ok"] is False
        assert "403" in body["error"]

    def test_kick_turning_us_away_says_kick_did_not_serve_us(self, client, monkeypatch):
        from scripts.probe_sources import Result

        monkeypatch.setattr(
            "scripts.probe_sources.probe_kick_live",
            lambda *a, **k: [
                Result("kick", "live /x", detail="HTTP 403 - Cloudflare challenge")
            ],
        )
        body = client.get("/api/probe/kick", params={"channel": "x"}, headers=_auth()).json()
        assert body["playback_url_served"] is False
        assert body["verdict"] == "Kick did not hand over a playback URL"

    def test_a_working_probe_says_so_plainly(self, client, monkeypatch):
        from scripts.probe_sources import Result

        monkeypatch.setattr(
            "scripts.probe_sources.probe_kick_live",
            lambda *a, **k: [
                Result("kick", "live /x", ok=True, detail="12000 viewers, 6 formats"),
                Result("kick", "rolling buffer", ok=True, detail="held 20s in 5.8MB"),
            ],
        )
        body = client.get("/api/probe/kick", params={"channel": "x"}, headers=_auth()).json()
        assert body["playback_url_served"] is True
        assert body["buffered"] is True
        assert "serves this datacenter IP" in body["verdict"]

    def test_serving_but_not_buffering_is_a_distinct_verdict(self, client, monkeypatch):
        """A URL that resolves but will not stream is its own problem."""
        from scripts.probe_sources import Result

        monkeypatch.setattr(
            "scripts.probe_sources.probe_kick_live",
            lambda *a, **k: [
                Result("kick", "live /x", ok=True, detail="live"),
                Result("kick", "rolling buffer", detail="nothing buffered"),
            ],
        )
        body = client.get("/api/probe/kick", params={"channel": "x"}, headers=_auth()).json()
        assert body["playback_url_served"] is True
        assert body["buffered"] is False
        assert "buffer did not fill" in body["verdict"]

    def test_the_run_length_is_capped(self, client):
        """A diagnostic that hangs for minutes looks like a broken deployment."""
        response = client.get(
            "/api/probe/kick", params={"channel": "x", "seconds": 9999}, headers=_auth()
        )
        assert response.status_code == 422


class TestLadderProbe:
    def test_a_failure_is_reported_not_raised(self, client, monkeypatch):
        def explode(*_args, **_kwargs):
            raise RuntimeError("CONNECT tunnel failed")

        monkeypatch.setattr("core.ytdlp.run", explode)
        body = client.get("/api/probe/ladder", params={"channel": "x"}, headers=_auth()).json()
        assert body["ok"] is False
        assert "CONNECT" in body["error"]

    def test_the_ladder_is_costed_per_job(self, client, monkeypatch):
        monkeypatch.setattr(
            "core.ytdlp.run",
            lambda *a, **k: {
                "is_live": True,
                "concurrent_view_count": 12_000,
                "formats": [
                    {"url": "a", "tbr": 128, "vcodec": "none"},
                    {"url": "b", "tbr": 230, "width": 284, "height": 160},
                    {"url": "c", "tbr": 6000, "width": 1920, "height": 1080},
                ],
            },
        )
        body = client.get("/api/probe/ladder", params={"channel": "x"}, headers=_auth()).json()
        assert body["ok"] is True
        assert body["detect"]["label"].startswith("160p")
        assert body["deliver"]["label"].startswith("1080p")
        assert body["detect"]["gb_per_day_x10"] < body["deliver"]["gb_per_day_x10"] / 20

    def test_a_channel_offering_nothing_says_so(self, client, monkeypatch):
        monkeypatch.setattr("core.ytdlp.run", lambda *a, **k: {"formats": []})
        body = client.get("/api/probe/ladder", params={"channel": "x"}, headers=_auth()).json()
        assert body["ok"] is False
        assert "no formats" in body["error"]


class TestTellingTheTwoFailuresApart:
    """Our proxy refusing and Kick refusing both arrive as 403."""

    def test_a_local_egress_block_is_inconclusive_not_a_kick_refusal(self, client, monkeypatch):
        from scripts.probe_sources import Result

        monkeypatch.setattr(
            "scripts.probe_sources.probe_kick_live",
            lambda *a, **k: [
                Result("kick", "live /x", detail="curl: (7) CONNECT tunnel failed, response 403")
            ],
        )
        body = client.get("/api/probe/kick", params={"channel": "x"}, headers=_auth()).json()
        assert body["network_blocked_locally"] is True
        assert "inconclusive" in body["verdict"]
        assert "never contacted" in body["verdict"]

    def test_a_real_kick_refusal_is_not_excused_as_a_network_fault(self, client, monkeypatch):
        from scripts.probe_sources import Result

        monkeypatch.setattr(
            "scripts.probe_sources.probe_kick_live",
            lambda *a, **k: [Result("kick", "live /x", detail="HTTP 403 from kick.com")],
        )
        body = client.get("/api/probe/kick", params={"channel": "x"}, headers=_auth()).json()
        assert body["network_blocked_locally"] is False
        assert body["verdict"] == "Kick did not hand over a playback URL"
