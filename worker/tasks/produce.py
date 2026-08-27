"""Studio stage: turn a queued render row into an mp4.

Runs on the `render` queue alongside the clip renderer, because both are the
same kind of work - long, CPU-bound, and fine to be slow as long as nothing
else is waiting behind them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.db import session_scope
from core.models import Render
from core.produce import Options, produce

log = logging.getLogger(__name__)


def run(render_id: int) -> dict[str, Any]:
    """Render `render_id`, recording what got in and what did not."""
    with session_scope() as session:
        row = session.get(Render, render_id)
        if row is None:
            raise ValueError(f"render {render_id} does not exist")
        if row.status == "running":
            log.warning("render %s is already running - not starting a second", render_id)
            return {"id": render_id, "status": row.status}
        options = dict(row.options or {})
        archive_id = row.archive_id
        row.status = "running"
        row.error = None

    try:
        result = produce(
            Options(
                archive_id=archive_id,
                voice_hook=bool(options.get("voice_hook", False)),
                grade=options.get("grade"),
                overlay=options.get("overlay"),
                use_stock=bool(options.get("use_stock", True)),
                tape_offset_s=options.get("tape_offset_s"),
                tape_path=Path(options["tape_path"]) if options.get("tape_path") else None,
                archive_item=options.get("archive_item"),
                fps=options.get("fps"),
            )
        )
    except Exception as exc:  # noqa: BLE001 - the row must record the failure
        log.exception("render %s failed", render_id)
        with session_scope() as session:
            row = session.get(Render, render_id)
            if row is not None:
                row.status = "failed"
                # Filter graphs produce enormous errors; the tail is the part
                # that names what actually broke.
                row.error = str(exc)[-4000:]
        raise

    with session_scope() as session:
        row = session.get(Render, render_id)
        if row is None:  # deleted while it was rendering
            return {"id": render_id, "status": "orphaned"}
        row.status = "ready"
        row.storage_key = result.storage_key
        row.duration_s = result.duration_s
        row.layers = result.layers
        row.warnings = result.warnings
        row.cost_usd = result.cost_usd
        row.elapsed_s = result.elapsed_s

    log.info(
        "render %s ready: %s (%.1fs of video in %.0fs)",
        render_id, result.storage_key, result.duration_s, result.elapsed_s,
    )
    return {"id": render_id, "status": "ready", **result.as_dict()}
