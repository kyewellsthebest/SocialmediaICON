"""Default backend: render, queue, and wait for a human.

Phases 1–2 of the plan. Nothing is posted automatically; the clip sits in the
review queue with its caption written, and you post it yourself.
"""

from __future__ import annotations

import logging

from core.publishers import PublishRequest, PublishResult

log = logging.getLogger(__name__)


class ManualPublisher:
    name = "manual"

    def publish(self, request: PublishRequest) -> list[PublishResult]:
        log.info(
            "manual publisher: %s is ready to post by hand (%s)",
            request.clip_path.name,
            ", ".join(request.platforms) or "no platforms configured",
        )
        return [
            PublishResult(
                platform=platform,
                ok=False,
                error="manual publishing - download the clip and post it yourself",
            )
            for platform in (request.platforms or ["manual"])
        ]
