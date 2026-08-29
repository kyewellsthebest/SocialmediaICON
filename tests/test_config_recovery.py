"""An unresolved Railway reference must not read as "never configured".

A ${{Service.VAR}} that cannot resolve becomes an empty string. That is
indistinguishable from the variable never having been added, and it sends you
to the wrong fix - it cost an evening here. The managed databases publish the
same connection under several names, so take whichever one arrived rather than
depending on one reference being typed correctly.
"""

from __future__ import annotations

import pytest

from core.config import Settings

REDIS_KEYS = (
    "REDIS_URL", "REDIS_PRIVATE_URL", "REDIS_PUBLIC_URL",
    "REDISHOST", "REDIS_HOST", "REDISPORT", "REDIS_PORT",
    "REDISUSER", "REDIS_USER", "REDISPASSWORD", "REDIS_PASSWORD",
)
DB_KEYS = ("DATABASE_URL", "DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for key in REDIS_KEYS + DB_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestRedis:
    def test_a_normal_url_is_used_unchanged(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "redis://a:b@h:6379")
        assert Settings().redis_url == "redis://a:b@h:6379"

    def test_an_empty_reference_falls_back_to_the_private_url(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "")
        monkeypatch.setenv("REDIS_PRIVATE_URL", "redis://d:p@redis.railway.internal:6379")
        assert Settings().redis_url == "redis://d:p@redis.railway.internal:6379"

    def test_an_unexpanded_template_is_treated_as_absent(self, monkeypatch):
        """Truthy, so it passes every "is it set" check and fails much later."""
        monkeypatch.setenv("REDIS_URL", "${{Redis.REDIS_URL}}")
        assert Settings().redis_url is None

    def test_an_unexpanded_template_still_falls_back(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "${{Redis.REDIS_URL}}")
        monkeypatch.setenv("REDIS_PRIVATE_URL", "redis://x:y@h:6379")
        assert Settings().redis_url == "redis://x:y@h:6379"

    def test_a_url_is_assembled_from_the_component_variables(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "")
        monkeypatch.setenv("REDISHOST", "redis.railway.internal")
        monkeypatch.setenv("REDISPORT", "6380")
        monkeypatch.setenv("REDISPASSWORD", "pw")
        assert Settings().redis_url == "redis://default:pw@redis.railway.internal:6380"

    def test_assembly_defaults_the_port_and_user(self, monkeypatch):
        monkeypatch.setenv("REDISHOST", "h")
        assert Settings().redis_url == "redis://h:6379"

    def test_nothing_configured_stays_none(self):
        assert Settings().redis_url is None
        assert Settings().has_redis is False

    def test_whitespace_only_is_absent(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "   ")
        assert Settings().redis_url is None


class TestDatabase:
    def test_an_empty_reference_falls_back_to_the_private_url(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setenv("DATABASE_PRIVATE_URL", "postgres://u:p@h/db")
        assert Settings().database_url == "postgres://u:p@h/db"

    def test_an_unexpanded_template_is_not_handed_to_sqlalchemy(self, monkeypatch):
        """A hostname with braces in it is a baffling DNS error much later."""
        monkeypatch.setenv("DATABASE_URL", "${{Postgres.DATABASE_URL}}")
        settings = Settings()
        assert settings.database_url is None
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            _ = settings.sqlalchemy_url

    def test_a_normal_url_still_normalises_onto_psycopg(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/db")
        assert Settings().sqlalchemy_url.startswith("postgresql+psycopg://")
