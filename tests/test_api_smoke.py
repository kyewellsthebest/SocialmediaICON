"""API smoke tests that do not need a database.

They prove the app boots, the dashboard is served, auth is enforced, and routes
that need Postgres fail with 503 rather than a blank 500.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api.main as main
from core.config import settings


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def test_health_is_public_and_reports_subsystems(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert set(body) >= {"db", "db_configured", "redis_configured", "storage", "publisher"}


def test_dashboard_and_assets_are_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_routes_needing_postgres_answer_503_not_500(client, monkeypatch):
    monkeypatch.setattr(settings, "database_url", None)
    response = client.get("/api/review/queue")
    assert response.status_code == 503
    assert "DATABASE_URL" in response.json()["detail"]


def test_token_gate(client, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_token", "sekrit")
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers={"X-Dashboard-Token": "nope"}).status_code == 401
    # right token gets past auth (503 here only because there is no database)
    assert client.get("/api/overview", headers={"X-Dashboard-Token": "sekrit"}).status_code != 401
    assert client.get("/api/overview?token=sekrit").status_code != 401


def test_health_stays_public_when_a_token_is_set(client, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_token", "sekrit")
    assert client.get("/health").status_code == 200


def test_worker_refuses_to_start_without_redis(monkeypatch, capsys):
    """A missing REDIS_URL is a config mistake — it should print one actionable
    line, not a traceback on every restart."""
    from worker import queue

    monkeypatch.setattr(settings, "redis_url", None)
    assert queue.main([]) == 1
    err = capsys.readouterr().err
    assert "REDIS_URL is not set" in err
    assert "${{Redis.REDIS_URL}}" in err
    assert "Traceback" not in err


def test_scheduler_refuses_to_run_jobs_inline_in_prod(monkeypatch, capsys):
    from worker import scheduler

    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "env", "prod")
    assert scheduler.main() == 1
    assert "REDIS_URL is not set" in capsys.readouterr().err


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("8080", 8080),
        (" 8080 ", 8080),
        ("", 8000),          # unset or empty
        ("${PORT:-8000}", 8000),  # an unexpanded shell placeholder
        ("not-a-port", 8000),
        ("0", 8000),         # out of range
        ("70000", 8000),
    ],
)
def test_port_resolution_survives_every_way_it_arrives_wrong(raw, expected):
    from api.serve import resolve_port

    assert resolve_port(raw) == expected
