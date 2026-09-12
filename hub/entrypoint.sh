#!/bin/sh
set -eu
poetry run alembic upgrade head

exec poetry run fastapi run --host 0.0.0.0 --port 8000 hub/main.py
