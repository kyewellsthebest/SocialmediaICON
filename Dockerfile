# One image, three processes. Railway runs it as `web`, `worker` and
# `scheduler`, each overriding the start command.
#
# Built as a Dockerfile rather than Nixpacks because ffmpeg is a system binary:
# adding it to a Nixpacks setup phase replaces the Python provider's own
# package list, which takes pip off PATH and fails the build.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg does the cutting, reframing and caption burn-in; without it ingest and
# render fail at runtime rather than at build time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change does not reinstall the whole tree.
COPY pyproject.toml ./
COPY api ./api
COPY core ./core
COPY worker ./worker
RUN pip install .

# Everything else: migrations, alembic.ini, scripts, static assets.
COPY . .

EXPOSE 8000

# Default is the web service. worker and scheduler override the argument:
#   worker     -> ./scripts/start.sh worker
#   scheduler  -> ./scripts/start.sh scheduler
#
# Leave the start command EMPTY in Railway to use this. A custom start command
# containing shell syntax like ${PORT:-8000} is passed through literally and
# will not expand.
CMD ["./scripts/start.sh", "web"]
