#!/bin/bash
# Local development stack (never touches the live Supabase project):
#   Postgres (.localdb/pg, port 55440) -> local Supabase stand-in (port 54329)
#   -> Staybot app on port ${APP_PORT:-8005} in WhatsApp test mode.
#
# Usage:  bash dev/local_stack.sh            # start (creates + migrates the DB the first time)
#         bash dev/local_stack.sh reset      # delete the local DB and start fresh
#         bash dev/local_stack.sh stop
set -euo pipefail
export LC_ALL=C LANG=C
cd "$(dirname "$0")/.."

PGBIN="${PGBIN:-}"
for candidate in /opt/homebrew/opt/postgresql@17/bin /opt/homebrew/opt/postgresql@16/bin /usr/local/opt/postgresql@17/bin; do
  [ -z "$PGBIN" ] && [ -x "$candidate/initdb" ] && PGBIN="$candidate"
done
[ -z "$PGBIN" ] && command -v initdb >/dev/null && PGBIN="$(dirname "$(command -v initdb)")"
[ -z "$PGBIN" ] && { echo "Install Postgres first: brew install postgresql@17"; exit 1; }

ROOT="$(pwd)/.localdb"
PG_PORT=55440; API_PORT=54329; APP_PORT="${APP_PORT:-8005}"
DSN="host=127.0.0.1 port=$PG_PORT user=postgres dbname=staybot"
MIGRATIONS="supabase_schema.sql supabase_properties.sql supabase_owner_listings.sql supabase_viewings.sql supabase_ai_calls.sql supabase_lead_scoring.sql supabase_outcomes.sql supabase_onboarding.sql supabase_tenancy.sql supabase_lease_onboarding.sql supabase_maintenance_tickets.sql supabase_owner_listing_photos.sql supabase_owner_leads.sql supabase_mls_listings.sql supabase_deals.sql supabase_investors.sql"

stop() {
  [ -f "$ROOT/api.pid" ] && kill "$(cat "$ROOT/api.pid")" 2>/dev/null || true
  [ -f "$ROOT/app.pid" ] && kill "$(cat "$ROOT/app.pid")" 2>/dev/null || true
  rm -f "$ROOT/api.pid" "$ROOT/app.pid"
  [ -d "$ROOT/pg" ] && "$PGBIN/pg_ctl" -D "$ROOT/pg" stop -m fast >/dev/null 2>&1 || true
}

case "${1:-start}" in
  stop) stop; echo "Local stack stopped."; exit 0 ;;
  reset) stop; rm -rf "$ROOT" ;;
esac

mkdir -p "$ROOT"
if [ ! -d "$ROOT/pg" ]; then
  "$PGBIN/initdb" -D "$ROOT/pg" -U postgres --auth=trust -E UTF8 --locale=C >/dev/null
  FRESH=1
fi
"$PGBIN/pg_ctl" -D "$ROOT/pg" status >/dev/null 2>&1 || \
  "$PGBIN/pg_ctl" -D "$ROOT/pg" -o "-p $PG_PORT -k '' -c listen_addresses=127.0.0.1" -l "$ROOT/pg.log" -w start >/dev/null

if [ "${FRESH:-0}" = 1 ]; then
  "$PGBIN/psql" "host=127.0.0.1 port=$PG_PORT user=postgres dbname=postgres" -qc "create database staybot" >/dev/null
  "$PGBIN/psql" "$DSN" -q -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
create role anon nologin; create role authenticated nologin; create role service_role nologin bypassrls;
grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
SQL
  for f in $MIGRATIONS; do
    echo "Applying $f"
    PGOPTIONS="-c client_min_messages=warning" "$PGBIN/psql" "$DSN" -q -v ON_ERROR_STOP=1 -f "$f" >/dev/null
  done
fi

export LOCAL_SUPABASE_DSN="$DSN" LOCAL_SUPABASE_KEY=local-dev-key LOCAL_SUPABASE_FILES="$ROOT/files"
[ -f "$ROOT/api.pid" ] && kill "$(cat "$ROOT/api.pid")" 2>/dev/null || true
nohup .venv/bin/uvicorn dev.local_supabase:app --host 127.0.0.1 --port $API_PORT > "$ROOT/api.log" 2>&1 &
echo $! > "$ROOT/api.pid"

[ -f "$ROOT/app.pid" ] && kill "$(cat "$ROOT/app.pid")" 2>/dev/null || true
SUPABASE_URL="http://127.0.0.1:$API_PORT" SUPABASE_SERVICE_KEY=local-dev-key WHATSAPP_DRY_RUN=true \
ADMIN_PASSWORD= WHATSAPP_ACCESS_TOKEN= WHATSAPP_APP_SECRET= HUBSPOT_ACCESS_TOKEN= \
PUBLIC_BASE_URL="http://127.0.0.1:$APP_PORT" ONBOARDING_LINK_SECRET=local-dev-link-secret \
  nohup .venv/bin/uvicorn src.main:app --host 127.0.0.1 --port "$APP_PORT" > "$ROOT/app.log" 2>&1 &
echo $! > "$ROOT/app.pid"
sleep 3
echo "Local stack ready: http://127.0.0.1:$APP_PORT/ui   (logs in .localdb/*.log)"
