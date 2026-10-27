#!/bin/sh
set -eu

: "${DATABASE_URL:?Set DATABASE_URL before starting the hosted application}"
: "${SECRET_KEY:?Set a permanent SECRET_KEY before starting the hosted application}"

# Render supplies a PostgreSQL URL without the psycopg 3 driver suffix.
case "$DATABASE_URL" in
    postgres://*) DATABASE_URL="postgresql+psycopg://${DATABASE_URL#postgres://}" ;;
    postgresql://*) DATABASE_URL="postgresql+psycopg://${DATABASE_URL#postgresql://}" ;;
esac
export DATABASE_URL

python deploy/restore_demo_uploads.py
python -m flask --app app:create_app db upgrade
exec gunicorn 'app:create_app()' --bind "0.0.0.0:${PORT:-10000}" --workers 1 --threads 4 --timeout 60 --access-logfile - --error-logfile -
