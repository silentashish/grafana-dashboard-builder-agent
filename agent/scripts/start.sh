#!/bin/sh
set -eu

alembic upgrade head

exec python -Xfrozen_modules=off -m uvicorn api:app --app-dir app --host 0.0.0.0 --port 8000
