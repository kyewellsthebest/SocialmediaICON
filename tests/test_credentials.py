"""The credential store and the refresh job.

The failure this guards against is quiet: a token lapses, publishing stops, and
nothing says why until someone looks. So the tests care about what happens when
a refresh fails as much as when it succeeds.
"""

from __future__ import annotations

import httpx
import pytest

from core import credentials
from core.config import settings
from worker.tasks import refresh_tokens


@pytest.fixture
def no_db(monkeypatch):
    monkeypatch.setattr(settings, "database_url", None, raising=False)
    monkeypatch.setattr(settings, "meta_access_token", "env-token", raising=False)
    monkeypatch.setattr(settings, "instagram_access_token", None, raising=False)
    monkeypatch.setattr(settings, "threads_access_token", "th-env", raising=False)
    return settings


def test_without_a_database_the_environment_is_the_answer(no_db):
    assert credentials.get("META_ACCESS_TOKEN") == "env-token"
    assert credentials.get("INSTAGRAM_ACCESS_TOKEN") is None


def test_storing_without_a_database_is_a_no_op_not_a_crash(no_db):
    credentials.put("META_ACCESS_TOKEN", "new", 3600)
    credentials.record_failure("META_ACCESS_TOKEN", "nope")
    assert credentials.status() == []


def test_refresh_uses_each_platforms_own_endpoint(no_db, monkeypatch):
    monkeypatch.setattr(settings, "meta_app_id", "app", raising=False)
    monkeypatch.setattr(settings, "meta_app_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "instagram_access_token", "ig-env", raising=False)
    seen: list[tuple[str, str]] = []
    stored: dict[str, str] = {}
    monkeypatch.setattr(
        refresh_tokens.credentials, "put", lambda n, v, s=None, e=None: stored.update({n: v})
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.params["grant_type"]))
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 5184000})

    outcome = refresh_tokens.run(httpx.Client(transport=httpx.MockTransport(handler)))

    assert outcome == {
        "META_ACCESS_TOKEN": "refreshed",
        "INSTAGRAM_ACCESS_TOKEN": "refreshed",
        "THREADS_ACCESS_TOKEN": "refreshed",
    }
    assert seen == [
        ("graph.facebook.com", "fb_exchange_token"),
        ("graph.instagram.com", "ig_refresh_token"),
        ("graph.threads.net", "th_refresh_token"),
    ]
    assert set(stored) == set(credentials.MANAGED)


def test_an_unconfigured_token_is_skipped_not_failed(no_db, monkeypatch):
    monkeypatch.setattr(settings, "meta_access_token", None, raising=False)
    monkeypatch.setattr(settings, "threads_access_token", None, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call out for a token that is not set")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert all(v == "not configured" for v in refresh_tokens.run(client).values())


def test_a_refused_refresh_is_reported_and_the_old_token_is_kept(no_db, monkeypatch):
    monkeypatch.setattr(settings, "meta_app_id", "app", raising=False)
    monkeypatch.setattr(settings, "meta_app_secret", "secret", raising=False)
    written: list[str] = []
    monkeypatch.setattr(refresh_tokens.credentials, "put", lambda *a, **k: written.append("wrote"))
    failures: list[tuple[str, str]] = []
    monkeypatch.setattr(
        refresh_tokens.credentials, "record_failure", lambda n, e: failures.append((n, e))
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Session has expired"}})

    outcome = refresh_tokens.run(httpx.Client(transport=httpx.MockTransport(handler)))

    assert written == []  # a failed refresh must not overwrite a working token
    assert any("Session has expired" in detail for detail in outcome.values())
    assert failures


def test_missing_app_credentials_are_named_not_guessed(no_db, monkeypatch):
    monkeypatch.setattr(settings, "meta_app_id", None, raising=False)
    monkeypatch.setattr(settings, "meta_app_secret", None, raising=False)

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        ok, detail = refresh_tokens._refresh_one(client, "META_ACCESS_TOKEN")

    assert ok is False
    assert "META_APP_ID" in detail
