#!/bin/bash
# Claude Code cloud sessions only: install Python deps and start a local Postgres 16
# so `uv run ruff check .` and `uv run pytest` work immediately. Safe to re-run.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

# 1. Python 3.12 + dependencies (uv reads .python-version and uv.lock)
uv sync --frozen

# 2. Postgres 16 for tests (port 5433 to avoid clashing with anything on 5432)
PG_BIN=/usr/lib/postgresql/16/bin
PG_ROOT=/var/tmp/solecra-pg
PG_DATA=$PG_ROOT/data
PG_PORT=5433

if [ -x "$PG_BIN/postgres" ]; then
  if [ ! -f "$PG_DATA/PG_VERSION" ]; then
    mkdir -p "$PG_ROOT" && chown postgres "$PG_ROOT"
    su postgres -c "$PG_BIN/initdb -D $PG_DATA -U postgres --auth=trust" >/dev/null
  fi
  if ! su postgres -c "$PG_BIN/pg_ctl -D $PG_DATA status" >/dev/null 2>&1; then
    su postgres -c "$PG_BIN/pg_ctl -D $PG_DATA -o '-p $PG_PORT -k /tmp' -l $PG_ROOT/log -w start" >/dev/null
  fi
  psql -h localhost -p $PG_PORT -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='solecra'" | grep -q 1 \
    || psql -h localhost -p $PG_PORT -U postgres -qc "CREATE ROLE solecra LOGIN PASSWORD 'solecra' CREATEDB"
  for db in solecra solecra_test; do
    psql -h localhost -p $PG_PORT -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
      || psql -h localhost -p $PG_PORT -U postgres -qc "CREATE DATABASE $db OWNER solecra"
  done

  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "export TEST_DATABASE_URL=postgresql://solecra:solecra@localhost:$PG_PORT/solecra_test" >> "$CLAUDE_ENV_FILE"
    echo "export DATABASE_URL=postgresql://solecra:solecra@localhost:$PG_PORT/solecra" >> "$CLAUDE_ENV_FILE"
  fi
else
  echo "Postgres 16 binaries not found; DB tests will be skipped." >&2
fi
