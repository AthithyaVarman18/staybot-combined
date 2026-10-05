-- Investor portfolio alerts: lease-ending and stale-maintenance nudges,
-- shown on the staff dashboard and sent as one daily WhatsApp digest per
-- investor. Run once in Supabase: SQL Editor -> New query -> paste -> Run.
-- Needs supabase_investor_journeys.sql (investors, investor_portfolio_properties)
-- and supabase_maintenance_tickets.sql first. Safe to run again.
--
-- See src/services/investor_alerts.py for what reads/writes these tables,
-- and src/services/investor_journey.py for the CSV upload that populates
-- investor_portfolio_properties.

-- ---------------------------------------------------------------------
-- 1. CSV upload needs unit + lease fields that didn't exist yet.
-- ---------------------------------------------------------------------
alter table investor_portfolio_properties add column if not exists unit_label text;
alter table investor_portfolio_properties add column if not exists tenant_name text;
alter table investor_portfolio_properties add column if not exists lease_start_date date;
alter table investor_portfolio_properties add column if not exists lease_end_date date;
create index if not exists investor_portfolio_lease_end_idx on investor_portfolio_properties (lease_end_date);

-- ---------------------------------------------------------------------
-- 2. WhatsApp digest opt-in. No opt-in, no message - see
--    investor_alerts.run_daily_digest().
-- ---------------------------------------------------------------------
alter table investors add column if not exists whatsapp_alerts_opt_in boolean not null default false;
alter table investors add column if not exists whatsapp_alerts_opt_in_at timestamptz;

-- ---------------------------------------------------------------------
-- 3. Maintenance tickets aren't linked to an investor today (they're filed
--    by a tenant against a marketplace `properties` listing). Best-effort
--    backfill (investor_alerts.refresh_alerts()) fills these in by matching
--    a ticket's property_id to a portfolio property's linked listing, so a
--    ticket can only be attributed - and alerted on - when that link exists.
-- ---------------------------------------------------------------------
alter table maintenance_tickets add column if not exists investor_id uuid references investors(id) on delete set null;
alter table maintenance_tickets add column if not exists portfolio_property_id uuid references investor_portfolio_properties(id) on delete set null;
create index if not exists maintenance_tickets_investor_idx on maintenance_tickets (investor_id, ticket_status);

-- ---------------------------------------------------------------------
-- 4. The three alert types (and only these three - no rent alerts yet).
--    One row per (alert_type, property or ticket), kept up to date by
--    investor_alerts.refresh_alerts() rather than recreated from scratch
--    each run, so a staff dismissal sticks and "created_at" reflects when
--    the alert first fired.
-- ---------------------------------------------------------------------
create table if not exists investor_alerts (
  id                    uuid primary key default gen_random_uuid(),
  investor_id           uuid not null references investors(id) on delete cascade,
  portfolio_property_id uuid references investor_portfolio_properties(id) on delete cascade,
  maintenance_ticket_id uuid references maintenance_tickets(id) on delete cascade,
  alert_type            text not null check (alert_type in ('lease_60', 'lease_30', 'maintenance_7')),
  dedupe_key            text not null unique,   -- e.g. 'lease_60:<portfolio_property_id>', 'maintenance_7:<ticket_id>'
  title                 text not null,
  detail                text not null,
  reference_date        date,                   -- lease end date, or the day the ticket was opened
  status                text not null default 'open' check (status in ('open', 'resolved', 'dismissed')),
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);
create index if not exists investor_alerts_investor_idx on investor_alerts (investor_id, status, created_at desc);
create index if not exists investor_alerts_status_idx on investor_alerts (status, alert_type);

drop trigger if exists investor_alerts_set_updated_at on investor_alerts;
create trigger investor_alerts_set_updated_at before update on investor_alerts
for each row execute function set_updated_at();

-- ---------------------------------------------------------------------
-- 5. One row per investor per calendar day a digest was attempted - the
--    unique constraint is what makes "one daily WhatsApp digest per
--    investor" hold even if the run-digest endpoint is called more than
--    once in a day (see investor_alerts.run_daily_digest()).
-- ---------------------------------------------------------------------
create table if not exists investor_alert_digests (
  id            uuid primary key default gen_random_uuid(),
  investor_id   uuid not null references investors(id) on delete cascade,
  sent_date     date not null,       -- local calendar date (APP_TIMEZONE), not UTC
  alert_ids     jsonb not null default '[]'::jsonb,
  channel       text not null default 'whatsapp',
  mode          text,                -- 'session' (24h window) | 'template' (outside it)
  status        text not null default 'sent' check (status in ('sent', 'skipped', 'failed')),
  error         text,
  created_at    timestamptz not null default now(),
  unique (investor_id, sent_date)
);
create index if not exists investor_alert_digests_investor_idx on investor_alert_digests (investor_id, sent_date desc);

-- Server-only, like every other table.
alter table investor_alerts enable row level security;
alter table investor_alert_digests enable row level security;
revoke all on investor_alerts, investor_alert_digests from anon, authenticated;
