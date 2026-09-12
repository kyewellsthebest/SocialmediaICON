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
  have_connection database && return 0
  # The reason matters more than the fact: empty, unexpanded and absent are
  # three different faults and only one of them means "you forgot".
  python -m core.envcheck DATABASE_URL >&2 || true
  fatal \
    "DATABASE_URL is not usable." \
    "" \
    "In Railway: add a Postgres database to this project, then set this" \
    "service's variable to reference it:" \
    "" \
    "    DATABASE_URL=\${{Postgres.DATABASE_URL}}" \
    "" \
    "Every service (web, worker, scheduler) needs it."
}

# A Railway ${{Service.VAR}} reference that cannot resolve arrives as an empty
# string, and one pasted as text arrives with its braces intact. Both are
# useless and neither is "unset", so ask whether there is a connection to be
# had at all rather than whether one variable is non-empty: Python knows how to
# recover one from the other names the managed databases publish, and this must
# not refuse to boot in a case Python can handle.
have_connection() {
  python - "$1" <<'PYCHECK'
import sys

from core.config import settings

which = sys.argv[1]
sys.exit(0 if (settings.redis_url if which == "redis" else settings.database_url) else 1)
PYCHECK
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

    # The port is resolved in Python, not here: a start command carrying
    # `--port ${PORT:-8000}` only works if a shell expands it, and when one
    # does not, uvicorn gets the literal string and never listens.
    exec python -m api.serve
    ;;
  worker|scheduler)
    fatal "there is no '$ROLE' service any more." \
      "" \
      "The daily run happens inside the web service, on a thread. One job" \
      "once a day does not need a process of its own - and a service that is" \
      "not running is a failure with no symptom: the dashboard accepts a run," \
      "the queue accepts the job, and nothing ever picks it up." \
      "" \
      "Delete this service in Railway. Everything runs in 'web'."
    ;;
  *)
    fatal "unknown role '$ROLE'" "" "The only role is: web"
    ;;
esac
