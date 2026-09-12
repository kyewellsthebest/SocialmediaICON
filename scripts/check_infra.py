#!/usr/bin/env python3
"""Phase 0 acceptance check: Postgres writes, R2 writes, ffmpeg exists.

    python scripts/check_infra.py

Writes a throwaway row and a throwaway object, reads both back, deletes them.
"""

from __future__ import annotations

import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from core.config import settings  # noqa: E402
from core.db import session_scope  # noqa: E402
from core.models import RunLog  # noqa: E402
from core.storage import get_storage  # noqa: E402


def check_ffmpeg() -> str:
    import shutil

    missing = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    if missing:
        raise RuntimeError(
            f"{', '.join(missing)} not on PATH. They are system binaries, not "
            f"pip packages - the Dockerfile installs them."
        )
    return "ffmpeg + ffprobe on PATH"


def check_db() -> str:
    if not settings.has_db:
        return "SKIP (DATABASE_URL not set)"
    with session_scope() as session:
        session.execute(text("select 1"))
        # A throwaway run_log row: written, read back and deleted, so the
        # check proves the round trip rather than just that a connection
        # opened.
        row = RunLog(started_at=datetime.now(UTC), error="infra check")
        session.add(row)
        session.flush()
        row_id = row.id
    with session_scope() as session:
        found = session.get(RunLog, row_id)
        assert found is not None, "row did not read back"
        session.delete(found)
    return f"wrote + read + deleted run_log row {row_id}"


def check_storage() -> str:
    storage = get_storage()
    key = f"_infra_check/{int(time.time())}.txt"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "probe.txt"
        src.write_text("clip-engine infra check")
        storage.put_file(src, key)
        back = storage.get_file(key, Path(tmp) / "back.txt")
        assert back.read_text() == "clip-engine infra check", "object did not read back"
    return f"{storage.kind}: wrote + read {key}"


def main() -> int:
    checks = (
        ("ffmpeg", check_ffmpeg),
        ("postgres", check_db),
        ("storage", check_storage),
    )
    failures = 0
    for name, check in checks:
        try:
            print(f"[ok]   {name:<10} {check()}")
        except Exception as exc:  # noqa: BLE001 - this script reports, it does not raise
            failures += 1
            print(f"[FAIL] {name:<10} {type(exc).__name__}: {exc}")
    print("\nall good" if not failures else f"\n{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
