#!/usr/bin/env sh
set -eu

echo "[entrypoint] Running database migrations..."
python -m alembic -c /app/alembic.ini upgrade head

echo "[entrypoint] Starting application..."
exec "$@"
