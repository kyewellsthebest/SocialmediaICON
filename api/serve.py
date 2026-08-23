"""Server entrypoint: `python -m api.serve`.

The port is read here, in Python, rather than interpolated into a shell command.
A start command like `--port ${PORT:-8000}` only expands if something runs it
through a shell; when it does not, uvicorn receives the literal string and dies
before it ever listens. Reading the environment directly cannot fail that way.
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

log = logging.getLogger("serve")

DEFAULT_PORT = 8000


def resolve_port(raw: str | None = None, default: int = DEFAULT_PORT) -> int:
    """PORT from the environment, falling back to `default`.

    Tolerates the ways it arrives wrong in practice: unset, empty, padded with
    whitespace, or an unexpanded shell placeholder.
    """
    value = (raw if raw is not None else os.environ.get("PORT", "")).strip()
    if not value:
        return default
    try:
        port = int(value)
    except ValueError:
        log.warning("PORT=%r is not a number - falling back to %d", value, default)
        return default
    if not 1 <= port <= 65535:
        log.warning("PORT=%d is out of range - falling back to %d", port, default)
        return default
    return port


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    port = resolve_port()
    source = "PORT" if os.environ.get("PORT", "").strip() else "default"
    print(f"binding 0.0.0.0:{port} (from {source})", flush=True)

    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
