"""Every setting the code reaches for has to exist.

This file exists because of a specific failure. Trimming the configuration
down to what this app actually uses dropped two properties that were still
being read - `sqlalchemy_url` lost the psycopg driver rewrite and
`r2_endpoint_url` went entirely - and neither showed up in any test, because
every test that touched them stubbed the thing around them. The first anyone
knew was a container that said

    FATAL: could not reach the database within 60s: No module named 'psycopg2'

which reads as a missing dependency rather than as a URL naming a driver that
was never installed.

So this reads the source for every `settings.<name>` and checks it resolves.
It is a crude test and it would have caught both.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Settings, settings

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ("core", "api", "worker", "scripts")
USE = re.compile(r"\bsettings\.([a-z_][a-z_0-9]*)")


def _asked_for() -> dict[str, set[str]]:
    """Every settings attribute the code reads, and where."""
    found: dict[str, set[str]] = {}
    for folder in SOURCE:
        for path in (ROOT / folder).rglob("*.py"):
            for name in USE.findall(path.read_text(encoding="utf-8")):
                found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_setting_the_code_reads_exists():
    have = set(Settings.model_fields) | {n for n in dir(Settings) if not n.startswith("_")}
    missing = {name: sorted(where) for name, where in _asked_for().items()
               if name not in have}
    assert not missing, (
        "the code reads settings that do not exist: "
        + "; ".join(f"{name} (in {', '.join(where)})" for name, where in missing.items())
    )


def test_the_names_found_are_real_ones():
    """A guard on the guard: a regex that matched nothing would pass the test
    above while checking nothing at all."""
    asked = _asked_for()
    assert len(asked) > 20, f"only found {len(asked)} settings - the regex is wrong"
    # Read through a property rather than directly, so the raw
    # `reddit_subreddits` is deliberately not in this list.
    assert "reddit_rooms" in asked
    assert "queue_size" in asked
    assert "sqlalchemy_url" in asked


class TestTheDatabaseUrl:
    """Railway hands out a URL that needs two rewrites before SQLAlchemy will
    take it, and neither failure names the URL as the problem."""

    @pytest.mark.parametrize("given", [
        "postgres://u:p@host:5432/db",
        "postgresql://u:p@host:5432/db",
    ])
    def test_it_lands_on_the_driver_that_is_installed(self, given, monkeypatch):
        monkeypatch.setattr(settings, "database_url", given)
        assert settings.sqlalchemy_url.startswith("postgresql+psycopg://")

    def test_the_rest_of_the_url_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "postgres://u:p@host:5432/db")
        assert settings.sqlalchemy_url.endswith("//u:p@host:5432/db")

    def test_a_url_that_already_names_the_driver_is_untouched(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://u@h/db")
        assert settings.sqlalchemy_url == "postgresql+psycopg://u@h/db"

    def test_psycopg2_is_never_what_comes_out(self, monkeypatch):
        """The exact string from the failed deploy."""
        monkeypatch.setattr(settings, "database_url", "postgres://u@h/db")
        assert "psycopg2" not in settings.sqlalchemy_url

    def test_no_url_says_which_variable_is_missing(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", None)
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            _ = settings.sqlalchemy_url


class TestTheRoomsAreForgiving:
    """A dashboard variable once arrived in production as "=irl" - the name in
    the name box and "=irl" in the value box - and the filter matched nothing
    for thirty-six minutes while every page said it was fine."""

    @pytest.mark.parametrize("given", [
        "GYM", " GYM ", '"GYM"', "r/GYM", "/r/GYM", "REDDIT_SUBREDDITS=GYM", "=GYM",
    ])
    def test_all_of_these_mean_gym(self, given, monkeypatch):
        monkeypatch.setattr(settings, "reddit_subreddits", given)
        assert settings.reddit_rooms == ["GYM"]

    def test_the_default_is_a_real_list_of_rooms(self, monkeypatch):
        """Blank would mean site-wide search, and search only exists on the
        JSON routes - the ones a cloud host is refused from. So an unset
        value has to be a working default, not an empty one."""
        assert len(Settings().reddit_rooms) >= 20

    def test_case_is_kept_because_reddit_keeps_it(self, monkeypatch):
        monkeypatch.setattr(settings, "reddit_subreddits", "GYM,bodyweightfitness")
        assert settings.reddit_rooms == ["GYM", "bodyweightfitness"]
