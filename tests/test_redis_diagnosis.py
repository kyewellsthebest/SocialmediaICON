"""A missing connection variable is three different faults wearing one message.

The variable was never added; it was added but the Railway reference resolved
to nothing; or it is right there in the environment and something between
there and here is dropping it. Each has a different fix, and telling them
apart from the outside cost an evening. So the message has to do it.
"""

from __future__ import annotations

import pytest

from worker.queue import redis_diagnosis


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)


class TestItNamesTheActualFault:
    def test_absent_says_it_was_never_added_to_this_service(self):
        found = redis_diagnosis()
        assert "absent" in found
        assert "THIS service" in found, "the usual mistake is setting it on web only"

    def test_empty_points_at_an_unresolved_reference(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "")
        found = redis_diagnosis()
        assert "EMPTY" in found
        assert "resolve" in found

    def test_whitespace_counts_as_empty(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "   ")
        assert "EMPTY" in redis_diagnosis()

    def test_an_unexpanded_reference_is_recognised(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "${{Redis.REDIS_URL}}")
        found = redis_diagnosis()
        assert "literal text" in found
        assert "did not expand" in found

    def test_a_real_url_present_means_the_fault_is_ours(self, monkeypatch):
        """If the value is there and we still failed, stop blaming the config."""
        monkeypatch.setenv("REDIS_URL", "redis://default:pw@host:6379")
        found = redis_diagnosis()
        assert "is* set" in found or "*is*" in found
        assert "our bug" in found


class TestItNeverLeaksTheValue:
    def test_a_real_url_is_not_printed(self, monkeypatch):
        secret = "redis://default:hunter2@redis.railway.internal:6379"
        monkeypatch.setenv("REDIS_URL", secret)
        found = redis_diagnosis()
        assert "hunter2" not in found
        assert secret not in found

    def test_related_variables_are_listed_by_name_only(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@host/db")
        monkeypatch.setenv("REDIS_PRIVATE_URL", "redis://pw@host")
        found = redis_diagnosis()
        assert "DATABASE_URL" in found and "REDIS_PRIVATE_URL" in found
        assert "pw@host" not in found

    def test_an_unexpanded_reference_is_shown_because_it_is_not_a_secret(self, monkeypatch):
        """The one value worth echoing: it is a template, not a credential."""
        monkeypatch.setenv("REDIS_URL", "${{Redis.REDIS_URL}}")
        assert "${{Redis.REDIS_URL}}" in redis_diagnosis()


class TestItWorksForAnyConnectionVariable:
    """The same fault hit DATABASE_URL a day later, so it is not Redis-specific."""

    def test_the_database_variable_gets_the_same_treatment(self, monkeypatch):
        from core.envcheck import explain

        monkeypatch.setenv("DATABASE_URL", "")
        found = explain("DATABASE_URL", service_hint="Postgres")
        assert "EMPTY" in found
        assert "Postgres" in found, "the hint names the service to look for"

    def test_unrelated_variables_are_not_listed(self, monkeypatch):
        """PG as a substring matches plenty of noise; a noisy list goes unread."""
        from core.envcheck import explain

        monkeypatch.setenv("USE_BUILTIN_RIPGREP", "1")
        monkeypatch.setenv("REDISHOST", "h")
        found = explain("REDIS_URL")
        assert "REDISHOST" in found
        assert "RIPGREP" not in found

    def test_the_cli_entry_point_prints_and_succeeds(self, capsys, monkeypatch):
        from core.envcheck import main

        monkeypatch.delenv("REDIS_URL", raising=False)
        assert main(["REDIS_URL"]) == 0
        assert "absent" in capsys.readouterr().out

    def test_the_cli_refuses_without_a_name(self):
        from core.envcheck import main

        assert main([]) == 2
