#!/bin/bash
# Runs the database tests (tenancy linking and the full lease onboarding journey)
# against a throwaway local Postgres.
# It creates a temporary database server, runs tests/test_tenancy_sql.py,
# then stops and deletes it. Your Supabase data is never touched.
#
# Usage (from the project folder):   bash tests/run_tenancy_db_tests.sh
# Needs Postgres installed, e.g.:    brew install postgresql@17
set -euo pipefail
export LC_ALL=C LANG=C

cd "$(dirname "$0")/.."

PGBIN="${PGBIN:-}"
if [ -z "$PGBIN" ]; then
  for candidate in /opt/homebrew/opt/postgresql@17/bin /opt/homebrew/opt/postgresql@16/bin /opt/homebrew/opt/postgresql@15/bin /usr/local/opt/postgresql@17/bin; do
    [ -x "$candidate/initdb" ] && PGBIN="$candidate" && break
  done
fi
if [ -z "$PGBIN" ] && command -v initdb >/dev/null; then PGBIN="$(dirname "$(command -v initdb)")"; fi
if [ -z "$PGBIN" ]; then
  echo "Postgres not found. Install it with: brew install postgresql@17"
  exit 1
fi

PORT="${TENANCY_TEST_PORT:-55433}"
DATA="$(mktemp -d /tmp/re5-tenancy-pg.XXXXXX)"

cleanup() {
  "$PGBIN/pg_ctl" -D "$DATA/data" stop -m fast >/dev/null 2>&1 || true
  rm -rf "$DATA"
}
trap cleanup EXIT

echo "Starting a throwaway Postgres on port $PORT ..."
"$PGBIN/initdb" -D "$DATA/data" -U postgres --auth=trust -E UTF8 --locale=C >/dev/null
"$PGBIN/pg_ctl" -D "$DATA/data" -o "-p $PORT -k '' -c listen_addresses=127.0.0.1" -l "$DATA/log" -w start >/dev/null

TENANCY_TEST_PG="host=127.0.0.1 port=$PORT user=postgres" \
TENANCY_TEST_PSQL="$PGBIN/psql" \
  .venv/bin/python -m unittest tests.test_tenancy_sql tests.test_onboarding_e2e tests.test_lease_onboarding -v
