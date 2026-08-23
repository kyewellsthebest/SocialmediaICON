# Railway and Heroku read this and it OVERRIDES the Dockerfile CMD, so it must
# stay in step with scripts/start.sh. No shell syntax here: process commands are
# not run through a shell, so a "${PORT:-8000}" would reach the program as text.
web: ./scripts/start.sh web
worker: ./scripts/start.sh worker
scheduler: ./scripts/start.sh scheduler
