#!/usr/bin/env bash
# Entrypoint for all three Railway services. Usage: scripts/start.sh <role>
#
#   web        migrations, then the API + dashboard
#   worker     pipeline jobs
#   scheduler  the periodic heartbeat
#
# Only web runs migrations: three services racing on `alembic upgrade head` at
# boot is how you get a half-applied schema.

set -euo pipefail

ROLE="${1:-${ROLE:-web}}"

fatal() {
  echo "FATAL: $1" >&2
  shift
  for line in "$@"; do echo "       $line" >&2; done
  exit 1
}

require_database() {
  [ -n "${DATABASE_URL:-}" ] || fatal \
    "DATABASE_URL is not set." \
    "" \
    "In Railway: add a Postgres database to this project, then set this" \
    "service's variable to reference it:" \
    "" \
    "    DATABASE_URL=\${{Postgres.DATABASE_URL}}" \
    "" \
    "Every service (web, worker, scheduler) needs it."
}

require_redis() {
  [ -n "${REDIS_URL:-}" ] || fatal \
    "REDIS_URL is not set, and the $ROLE cannot queue work without it." \
    "" \
    "In Railway: add a Redis database, then set this service's variable to:" \
    "" \
    "    REDIS_URL=\${{Redis.REDIS_URL}}"
}

# Postgres can still be accepting connections a few seconds after the container
# starts, so retry rather than crash-looping on a cold boot.
wait_for_database() {
  python - <<'PY'
import sys, time

from sqlalchemy import create_engine, text

from core.config import settings

deadline = time.time() + 60
last = None
while time.time() < deadline:
    try:
        engine = create_engine(settings.sqlalchemy_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        print("database is up", flush=True)
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001 - any connection error is a retry
        last = exc
        time.sleep(2)

print(f"FATAL: could not reach the database within 60s: {last}", file=sys.stderr)
sys.exit(1)
PY
}

case "$ROLE" in
  web)
    require_database
    wait_for_database
    echo "running migrations..."
    alembic upgrade head
    exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  worker)
    require_database
    require_redis
    wait_for_database
    exec python -m worker.queue
    ;;
  scheduler)
    require_database
    require_redis
    wait_for_database
    exec python -m worker.scheduler
    ;;
  *)
    fatal "unknown role '$ROLE'" "" "Expected one of: web, worker, scheduler"
    ;;
esac
