-- Purchase offer + inspection assistance. Run after the existing base schema,
-- properties, investors, deals and viewings migrations.
-- No purchase contract is generated here; the broker's approved platform remains
-- the contract source of truth.

create table if not exists acquisition_offers (
  id uuid primary key default gen_random_uuid(),
  reference text not null unique,
  deal_id uuid references investment_deals(id) on delete set null,
  property_id text not null references properties(id),
  property_ref text,
  property_title text,
  investor_id uuid references investors(id) on delete set null,
  investor_name text not null,
  investor_email text,
  investor_phone text,
  status text not null default 'draft' check (status in ('draft','awaiting_investor_approval','investor_approved','handed_off','negotiating','accepted','rejected','withdrawn','expired')),
  current_version integer not null default 1,
  state_version integer not null default 1,
  created_by text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists acquisition_offers_status_idx on acquisition_offers(status, updated_at desc);
create index if not exists acquisition_offers_investor_idx on acquisition_offers(investor_id, updated_at desc);
create index if not exists acquisition_offers_property_idx on acquisition_offers(property_id, updated_at desc);

drop trigger if exists acquisition_offers_set_updated_at on acquisition_offers;
create trigger acquisition_offers_set_updated_at before update on acquisition_offers for each row execute function set_updated_at();

create table if not exists acquisition_offer_versions (
  id uuid primary key default gen_random_uuid(),
  offer_id uuid not null references acquisition_offers(id) on delete cascade,
  version_number integer not null,
  terms jsonb not null,
  content_hash text not null,
  created_by text not null,
  superseded_at timestamptz,
  created_at timestamptz not null default now(),
  unique (offer_id, version_number)
);
create index if not exists acquisition_offer_versions_offer_idx on acquisition_offer_versions(offer_id, version_number desc);

create table if not exists acquisition_offer_approvals (
  id uuid primary key default gen_random_uuid(),
  offer_id uuid not null references acquisition_offers(id) on delete cascade,
  version_id uuid not null references acquisition_offer_versions(id) on delete cascade,
  actor_id uuid,
  decision text not null check (decision in ('pending','approved','changes_requested')),
  note text not null default '',
  superseded_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists acquisition_offer_approvals_offer_idx on acquisition_offer_approvals(offer_id, created_at desc);

create table if not exists acquisition_offer_access_links (
  id uuid primary key default gen_random_uuid(),
  offer_id uuid not null references acquisition_offers(id) on delete cascade,
  version_id uuid not null references acquisition_offer_versions(id) on delete cascade,
  version_number integer not null,
  actor_id uuid,
  token_hash text not null unique,
  expires_at timestamptz not null,
  revoked_at timestamptz,
  last_used_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists acquisition_offer_access_links_offer_idx on acquisition_offer_access_links(offer_id, version_number desc);

create table if not exists acquisition_offer_documents (
  id uuid primary key default gen_random_uuid(),
  offer_id uuid not null references acquisition_offers(id) on delete cascade,
  version_id uuid not null references acquisition_offer_versions(id) on delete cascade,
  kind text not null check (kind in ('review_summary_pdf')),
  file_name text not null,
  storage_path text not null unique,
  sha256 text not null,
  size_bytes integer not null,
  created_by text not null,
  created_at timestamptz not null default now()
);
create index if not exists acquisition_offer_documents_offer_idx on acquisition_offer_documents(offer_id, created_at desc);

create table if not exists acquisition_offer_handoffs (
  id uuid primary key default gen_random_uuid(),
  offer_id uuid not null references acquisition_offers(id) on delete cascade,
  platform text not null check (platform in ('zipform','dotloop','skyslope','other')),
  platform_link text,
  status text not null check (status in ('pending','handed_off','active','completed')),
  note text not null default '',
  created_by text not null,
  created_at timestamptz not null default now()
);
create index if not exists acquisition_offer_handoffs_offer_idx on acquisition_offer_handoffs(offer_id, created_at desc);

create table if not exists acquisition_offer_events (
  id bigint generated always as identity primary key,
  offer_id uuid not null references acquisition_offers(id) on delete cascade,
  sequence integer not null,
  event_type text not null,
  actor_type text not null,
  actor_id text,
  amount numeric(14,2),
  terms jsonb not null default '{}'::jsonb,
  message text not null default '',
  status_after text,
  created_at timestamptz not null default now(),
  unique (offer_id, sequence)
);
create index if not exists acquisition_offer_events_offer_idx on acquisition_offer_events(offer_id, sequence);

create table if not exists acquisition_offer_deadlines (
  id uuid primary key default gen_random_uuid(),
  offer_id uuid not null references acquisition_offers(id) on delete cascade,
  version_id uuid references acquisition_offer_versions(id) on delete set null,
  kind text not null,
  label text not null,
  due_at timestamptz not null,
  remind_at timestamptz not null,
  status text not null default 'active' check (status in ('active','resolved','superseded')),
  reminder_claimed_at timestamptz,
  reminder_sent_at timestamptz,
  reminder_status text,
  created_at timestamptz not null default now()
);
create index if not exists acquisition_offer_deadlines_due_idx on acquisition_offer_deadlines(status, remind_at);

create table if not exists acquisition_offer_notifications (
  id bigint generated always as identity primary key,
  offer_id uuid not null references acquisition_offers(id) on delete cascade,
  kind text not null,
  message text not null,
  read_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists acquisition_offer_notifications_idx on acquisition_offer_notifications(read_at, created_at desc);

create table if not exists inspectors (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  license_number text not null,
  areas_covered text not null,
  price text,
  turnaround text,
  contact text not null,
  active boolean not null default true,
  created_by text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists inspectors_active_idx on inspectors(active, name);

drop trigger if exists inspectors_set_updated_at on inspectors;
create trigger inspectors_set_updated_at before update on inspectors for each row execute function set_updated_at();

create table if not exists inspection_bookings (
  id uuid primary key default gen_random_uuid(),
  offer_id uuid references acquisition_offers(id) on delete set null,
  deal_id uuid references investment_deals(id) on delete set null,
  inspector_id uuid not null references inspectors(id),
  proposed_slots jsonb not null default '[]'::jsonb,
  confirmed_date date,
  confirmed_time time,
  listing_agent_name text,
  listing_agent_contact text,
  access_confirmed boolean not null default false,
  status text not null default 'proposed' check (status in ('proposed','confirmed','cancelled','completed')),
  fee_payer text not null default 'unknown' check (fee_payer in ('investor','seller','broker','other','unknown')),
  note text not null default '',
  confirmation_note text,
  created_by text not null,
  confirmed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists inspection_bookings_offer_idx on inspection_bookings(offer_id, created_at desc);
create index if not exists inspection_bookings_when_idx on inspection_bookings(confirmed_date, confirmed_time);

drop trigger if exists inspection_bookings_set_updated_at on inspection_bookings;
create trigger inspection_bookings_set_updated_at before update on inspection_bookings for each row execute function set_updated_at();

create table if not exists inspection_reports (
  id uuid primary key default gen_random_uuid(),
  offer_id uuid references acquisition_offers(id) on delete set null,
  deal_id uuid references investment_deals(id) on delete set null,
  inspector_id uuid references inspectors(id) on delete set null,
  file_name text not null,
  storage_path text not null unique,
  sha256 text not null,
  size_bytes integer not null,
  extraction_status text not null default 'manual_required' check (extraction_status in ('extracted','manual_required')),
  uploaded_by text not null,
  created_at timestamptz not null default now()
);
create index if not exists inspection_reports_offer_idx on inspection_reports(offer_id, created_at desc);

create table if not exists inspection_findings (
  id bigint generated always as identity primary key,
  report_id uuid not null references inspection_reports(id) on delete cascade,
  severity text not null check (severity in ('major','minor')),
  summary text not null,
  report_quote text not null,
  source_page text,
  created_by text not null,
  created_at timestamptz not null default now()
);
create index if not exists inspection_findings_report_idx on inspection_findings(report_id, id);

create table if not exists inspection_summary_deliveries (
  id bigint generated always as identity primary key,
  report_id uuid not null references inspection_reports(id) on delete cascade,
  channel text not null,
  status text not null,
  message text not null,
  created_at timestamptz not null default now()
);

create table if not exists inspection_repairs (
  id bigint generated always as identity primary key,
  offer_id uuid not null references acquisition_offers(id) on delete cascade,
  request text not null,
  cost_amount numeric(14,2),
  cost_source text not null default 'none' check (cost_source in ('staff_quote','client_approved_table','none')),
  requested_by text not null,
  status text not null default 'requested' check (status in ('requested','agreed','rejected')),
  seller_response text,
  agreed boolean,
  agreed_amount numeric(14,2),
  responded_by text,
  responded_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists inspection_repairs_offer_idx on inspection_repairs(offer_id, id);

-- Generic versioned term update with optimistic concurrency. A changed term set
-- creates a new review version and resets investor approval.
create or replace function acquisition_offer_update_terms(p_offer_id uuid, p_expected_state_version integer,
                                                       p_terms jsonb, p_actor_id text, p_content_hash text)
returns jsonb
language plpgsql as $$
declare
  o acquisition_offers;
  current_v acquisition_offer_versions;
  new_v acquisition_offer_versions;
begin
  select * into o from acquisition_offers where id = p_offer_id for update;
  if not found then raise exception 'OFFER_NOT_FOUND'; end if;
  if o.state_version <> p_expected_state_version then raise exception 'VERSION_CONFLICT'; end if;
  if o.status in ('handed_off','negotiating','accepted','rejected','withdrawn','expired') then raise exception 'OFFER_NOT_EDITABLE'; end if;

  select * into current_v from acquisition_offer_versions where offer_id = p_offer_id order by version_number desc limit 1 for update;
  if not found then raise exception 'VERSION_NOT_FOUND'; end if;
  update acquisition_offer_versions set superseded_at = now() where id = current_v.id and superseded_at is null;

  insert into acquisition_offer_versions(offer_id, version_number, terms, content_hash, created_by)
  values(p_offer_id, current_v.version_number + 1, p_terms, p_content_hash, p_actor_id)
  returning * into new_v;

  update acquisition_offers set current_version = new_v.version_number, state_version = state_version + 1,
    status = 'awaiting_investor_approval', updated_at = now() where id = p_offer_id;

  update acquisition_offer_approvals set superseded_at = now(), note = note || ' [Superseded by new terms version ' || new_v.version_number || ']'
  where offer_id = p_offer_id and version_id = current_v.id and superseded_at is null;
  update acquisition_offer_access_links set revoked_at = now()
  where offer_id = p_offer_id and version_number = current_v.version_number and revoked_at is null;

  return jsonb_build_object('offer_id', p_offer_id, 'current_version', new_v.version_number,
                            'state_version', o.state_version + 1, 'version', to_jsonb(new_v));
end;
$$;

create or replace function acquisition_offer_record_event(p_offer_id uuid, p_expected_state_version integer,
                                                        p_event_type text, p_actor_type text, p_actor_id text,
                                                        p_amount numeric, p_terms jsonb, p_note text, p_status_after text)
returns jsonb
language plpgsql as $$
declare
  o acquisition_offers;
  seq integer;
  next_version integer;
  event_row acquisition_offer_events;
begin
  select * into o from acquisition_offers where id = p_offer_id for update;
  if not found then raise exception 'OFFER_NOT_FOUND'; end if;
  if o.state_version <> p_expected_state_version then raise exception 'VERSION_CONFLICT'; end if;
  if o.status in ('accepted','rejected','withdrawn','expired') then raise exception 'OFFER_CLOSED'; end if;
  select coalesce(max(sequence),0)+1 into seq from acquisition_offer_events where offer_id = p_offer_id;
  next_version := o.state_version + 1;
  insert into acquisition_offer_events(offer_id, sequence, event_type, actor_type, actor_id, amount, terms, message, status_after)
  values(p_offer_id, seq, p_event_type, p_actor_type, p_actor_id, p_amount, coalesce(p_terms,'{}'::jsonb), coalesce(p_note,''), p_status_after)
  returning * into event_row;
  update acquisition_offers set state_version = next_version, status = p_status_after, updated_at = now() where id = p_offer_id;
  return jsonb_build_object('event', to_jsonb(event_row), 'state_version', next_version, 'status', p_status_after);
end;
$$;

create or replace function acquisition_offer_record_handoff(p_offer_id uuid, p_expected_state_version integer,
                                                         p_platform text, p_link text, p_status text, p_actor_id text, p_note text)
returns jsonb
language plpgsql as $$
declare
  o acquisition_offers;
  h acquisition_offer_handoffs;
begin
  select * into o from acquisition_offers where id = p_offer_id for update;
  if not found then raise exception 'OFFER_NOT_FOUND'; end if;
  if o.state_version <> p_expected_state_version then raise exception 'VERSION_CONFLICT'; end if;
  insert into acquisition_offer_handoffs(offer_id, platform, platform_link, status, note, created_by)
  values(p_offer_id, p_platform, p_link, p_status, coalesce(p_note,''), p_actor_id) returning * into h;
  update acquisition_offers set state_version = state_version + 1,
    status = case when p_status in ('active','handed_off','completed') then 'handed_off' else status end,
    updated_at = now() where id = p_offer_id;
  return jsonb_build_object('handoff', to_jsonb(h), 'state_version', o.state_version + 1);
end;
$$;

create or replace function acquisition_offer_record_approval(p_offer_id uuid, p_version_id uuid, p_actor_id uuid,
                                                          p_decision text, p_note text)
returns jsonb
language plpgsql as $$
declare
  o acquisition_offers;
  v acquisition_offer_versions;
  a acquisition_offer_approvals;
begin
  select * into o from acquisition_offers where id = p_offer_id for update;
  select * into v from acquisition_offer_versions where id = p_version_id and offer_id = p_offer_id;
  if not found then raise exception 'VERSION_NOT_FOUND'; end if;
  if o.current_version <> v.version_number then raise exception 'OUTDATED_APPROVAL_VERSION'; end if;
  if o.status not in ('awaiting_investor_approval','investor_approved') then raise exception 'APPROVAL_NOT_OPEN'; end if;
  insert into acquisition_offer_approvals(offer_id, version_id, actor_id, decision, note)
  values(p_offer_id, p_version_id, p_actor_id, p_decision, coalesce(p_note,'')) returning * into a;
  update acquisition_offers set status = case when p_decision = 'approved' then 'investor_approved' else 'awaiting_investor_approval' end,
    state_version = state_version + 1, updated_at = now() where id = p_offer_id;
  if p_decision in ('approved','changes_requested') then
    update acquisition_offer_access_links set revoked_at = now()
    where offer_id = p_offer_id and version_id = p_version_id and revoked_at is null;
  end if;
  return jsonb_build_object('approval', to_jsonb(a), 'status', case when p_decision='approved' then 'investor_approved' else 'awaiting_investor_approval' end,
                            'state_version', o.state_version + 1);
end;
$$;

-- Build deadlines from the current offer version.
create or replace function acquisition_offer_rebuild_deadlines(p_offer_id uuid, p_version integer)
returns void
language plpgsql as $$
declare
  v acquisition_offer_versions;
  d date;
  expires timestamptz;
begin
  select * into v from acquisition_offer_versions where offer_id = p_offer_id and version_number = p_version;
  if not found then return; end if;
  update acquisition_offer_deadlines set status='superseded' where offer_id = p_offer_id and status='active';
  if (v.terms->>'due_diligence_end') is not null then
    d := (v.terms->>'due_diligence_end')::date;
    insert into acquisition_offer_deadlines(offer_id, version_id, kind, label, due_at, remind_at)
    values(p_offer_id, v.id, 'due_diligence_end', 'Due diligence end', d + time '23:59:59', d + time '09:00:00');
  end if;
  if (v.terms->>'financing_deadline') is not null then
    d := (v.terms->>'financing_deadline')::date;
    insert into acquisition_offer_deadlines(offer_id, version_id, kind, label, due_at, remind_at)
    values(p_offer_id, v.id, 'financing_deadline', 'Financing deadline', d + time '23:59:59', d + time '09:00:00');
  end if;
  if (v.terms->>'closing_date') is not null then
    d := (v.terms->>'closing_date')::date;
    insert into acquisition_offer_deadlines(offer_id, version_id, kind, label, due_at, remind_at)
    values(p_offer_id, v.id, 'closing_date', 'Closing date', d + time '17:00:00', d + time '09:00:00');
  end if;
  if (v.terms->>'expires_at') is not null then
    expires := (v.terms->>'expires_at')::timestamptz;
    insert into acquisition_offer_deadlines(offer_id, version_id, kind, label, due_at, remind_at)
    values(p_offer_id, v.id, 'offer_expiration', 'Offer expiration', expires, expires - interval '24 hours');
  end if;
end;
$$;

-- Server-only. The backend uses the service_role key.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'acquisition_offers','acquisition_offer_versions','acquisition_offer_approvals','acquisition_offer_access_links',
    'acquisition_offer_documents','acquisition_offer_handoffs','acquisition_offer_events','acquisition_offer_deadlines',
    'acquisition_offer_notifications','inspectors','inspection_bookings','inspection_reports','inspection_findings',
    'inspection_summary_deliveries','inspection_repairs'
  ] LOOP
    EXECUTE format('alter table %I enable row level security', t);
    EXECUTE format('revoke all on %I from anon, authenticated', t);
  END LOOP;
END$$;

grant execute on function acquisition_offer_update_terms(uuid,integer,jsonb,text,text) to service_role;
grant execute on function acquisition_offer_record_event(uuid,integer,text,text,text,numeric,jsonb,text,text) to service_role;
grant execute on function acquisition_offer_record_handoff(uuid,integer,text,text,text,text,text) to service_role;
grant execute on function acquisition_offer_record_approval(uuid,uuid,uuid,text,text) to service_role;
grant execute on function acquisition_offer_rebuild_deadlines(uuid,integer) to service_role;
