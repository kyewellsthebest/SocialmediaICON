"""The migration chain must link up before it reaches a database.

There is no Postgres in CI, so a broken `down_revision` is invisible to every
other test and only shows up as a crashed deploy - alembic raises a bare
`KeyError: '<missing id>'` from inside its revision map, which does not say
which file is wrong. These tests read the files directly and name it.
"""

from __future__ import annotations

import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"

REVISION = re.compile(r"^revision\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
DOWN = re.compile(r"^down_revision\s*=\s*(?:[\"']([^\"']+)[\"']|None)", re.MULTILINE)


def _migrations() -> dict[str, tuple[str, str | None]]:
    """filename -> (revision, down_revision)."""
    found: dict[str, tuple[str, str | None]] = {}
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        body = path.read_text(encoding="utf-8")
        revision = REVISION.search(body)
        down = DOWN.search(body)
        assert revision, f"{path.name} has no revision identifier"
        assert down, f"{path.name} has no down_revision"
        found[path.name] = (revision.group(1), down.group(1))
    return found


def test_there_are_migrations_to_check() -> None:
    assert _migrations(), "no migrations found - has the versions directory moved?"


def test_every_down_revision_points_at_a_real_migration() -> None:
    migrations = _migrations()
    known = {rev for rev, _ in migrations.values()}
    for name, (_, down) in migrations.items():
        if down is None:
            continue
        assert down in known, (
            f"{name}: down_revision {down!r} does not exist. "
            f"Known revisions: {', '.join(sorted(known))}"
        )


def test_revision_ids_are_unique() -> None:
    migrations = _migrations()
    seen: dict[str, str] = {}
    for name, (rev, _) in migrations.items():
        assert rev not in seen, f"{name} reuses revision {rev!r} from {seen[rev]}"
        seen[rev] = name


def test_exactly_one_base_and_one_head() -> None:
    """A second head means alembic cannot decide what `upgrade head` means."""
    migrations = _migrations()
    revisions = {rev for rev, _ in migrations.values()}
    downs = {down for _, down in migrations.values() if down}

    bases = [name for name, (_, down) in migrations.items() if down is None]
    assert len(bases) == 1, f"expected one base migration, found {bases}"

    heads = sorted(revisions - downs)
    assert len(heads) == 1, f"expected one head, found {heads} - the chain has branched"


def test_revision_ids_follow_the_projects_convention() -> None:
    """Bare zero-padded numbers, matching the filename prefix.

    The convention is not cosmetic: `0004_studio.py` declaring revision
    `"0004_studio"` while its neighbours use `"0003"` is exactly the mismatch
    that produced a crashed deploy.
    """
    for name, (rev, _) in _migrations().items():
        assert rev.isdigit(), f"{name}: revision {rev!r} should be a bare number like '0004'"
        assert name.startswith(rev), f"{name}: filename does not start with its revision {rev!r}"
