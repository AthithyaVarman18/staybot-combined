-- Guided resident onboarding with lease agreement, approvals, final PDF and
-- WhatsApp delivery. Run AFTER supabase_onboarding.sql and supabase_tenancy.sql.
--
-- Non-destructive: only creates new tables/functions, adds nullable columns to
-- tenants/tenancies so a finalized case can be linked, and seeds a clearly
-- labelled DEMO template and document checklist. Safe to run again.
--
-- Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.

-- ---------------------------------------------------------------------
-- People (tenants and owners), staff, templates, document checklist
-- ---------------------------------------------------------------------
create table if not exists onboarding_people (
  id                  uuid primary key default gen_random_uuid(),
  full_name           text not null check (length(btrim(full_name)) between 1 and 200),
  whatsapp            text check (whatsapp ~ '^\+[1-9][0-9]{7,14}$'),
  email               text check (email is null or email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  preferred_language  text not null default 'en' check (preferred_language in ('en', 'es')),
  communication       jsonb not null default '{}'::jsonb,
  is_test             boolean not null default false,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
-- One person per WhatsApp number, so existing records are reused, not duplicated.
create unique index if not exists onboarding_people_whatsapp_uq on onboarding_people (whatsapp) where whatsapp is not null;

create table if not exists onboarding_staff (
  id          uuid primary key default gen_random_uuid(),
  name        text not null unique check (length(btrim(name)) between 1 and 120),
  active      boolean not null default true,
  created_at  timestamptz not null default now()
);

create table if not exists lease_templates (
  id                uuid primary key default gen_random_uuid(),
  name              text not null,
  jurisdiction      text not null,
  template_version  integer not null default 1,
  status            text not null check (status in ('demo', 'approved', 'retired')),
  clauses           jsonb not null check (jsonb_typeof(clauses) = 'array' and jsonb_array_length(clauses) > 0),
  approved_by       text,
  approved_at       timestamptz,
  notes             text,
  created_at        timestamptz not null default now(),
  unique (name, template_version),
  constraint lease_templates_approval_recorded
    check (status <> 'approved' or (approved_by is not null and approved_at is not null))
);

create table if not exists onboarding_document_requirements (
  id            uuid primary key default gen_random_uuid(),
  jurisdiction  text not null,
  property_id   text references properties(id),          -- null = every property in the jurisdiction
  party         text not null check (party in ('tenant', 'owner')),
  doc_key       text not null check (doc_key ~ '^[a-z0-9_]{1,60}$'),
  label         text not null,
  reason        text not null,
  required      boolean not null default true,
  active        boolean not null default true,
  created_at    timestamptz not null default now(),
  unique nulls not distinct (jurisdiction, property_id, party, doc_key)
);

-- ---------------------------------------------------------------------
-- Cases
-- ---------------------------------------------------------------------
create sequence if not exists onboarding_case_ref_seq;

create table if not exists onboarding_cases (
  id                uuid primary key default gen_random_uuid(),
  reference         text not null unique default ('OB-' || lpad(nextval('onboarding_case_ref_seq')::text, 6, '0')),
  status            text not null default 'active' check (status in ('active', 'finalized', 'cancelled')),
  current_step      integer not null default 1 check (current_step between 1 and 7),
  property_id       text not null references properties(id),
  unit              text not null default '' check (length(unit) <= 40),
  property_address  text not null default '' check (length(property_address) <= 300),
  tenant_id         uuid references onboarding_people(id),
  owner_id          uuid references onboarding_people(id),
  staff_id          uuid references onboarding_staff(id),
  conversation_id   uuid references conversations(id) on delete set null,
  jurisdiction      text not null default 'US-NC',
  template_id       uuid references lease_templates(id),
  terms             jsonb not null default '{}'::jsonb,
  preferences       jsonb not null default '{}'::jsonb,
  consent           jsonb not null default '{}'::jsonb,
  signature_method  text not null default 'approval_only' check (signature_method in ('approval_only', 'external_esign')),
  staff_review      jsonb,
  turbotenant       jsonb not null default '{"status": "not_connected"}'::jsonb,
  is_test           boolean not null default false,
  version           integer not null default 1,
  created_by        text not null default 'staff',
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  finalized_at      timestamptz,
  constraint onboarding_cases_parties_differ check (tenant_id is null or owner_id is null or tenant_id <> owner_id)
);
-- Prevent accidental duplicate cases for the same home.
create unique index if not exists onboarding_cases_one_active_per_unit
  on onboarding_cases (property_id, unit) where status = 'active';
create index if not exists onboarding_cases_updated_idx on onboarding_cases (updated_at desc);

create table if not exists onboarding_case_documents (
  id            uuid primary key default gen_random_uuid(),
  case_id       uuid not null references onboarding_cases(id),
  party         text not null check (party in ('tenant', 'owner')),
  doc_key       text not null,
  file_name     text not null check (length(file_name) between 1 and 200),
  content_type  text not null check (content_type in ('application/pdf', 'image/jpeg', 'image/png')),
  size_bytes    integer not null check (size_bytes between 1 and 10485760),
  sha256        text not null,
  storage_path  text not null unique,
  status        text not null default 'awaiting_review'
                check (status in ('awaiting_review', 'accepted', 'changes_required')),
  review_note   text,
  reviewed_by   text,
  reviewed_at   timestamptz,
  uploaded_by   text not null,
  created_at    timestamptz not null default now(),
  -- An upload is never "accepted" without a recorded reviewer.
  constraint onboarding_documents_review_recorded
    check (status = 'awaiting_review' or (reviewed_by is not null and reviewed_at is not null))
);
create index if not exists onboarding_case_documents_case_idx on onboarding_case_documents (case_id, party, doc_key, created_at desc);

create table if not exists onboarding_agreement_versions (
  id            uuid primary key default gen_random_uuid(),
  case_id       uuid not null references onboarding_cases(id),
  version       integer not null check (version >= 1),
  snapshot      jsonb not null,
  content_hash  text not null,
  template_id   uuid not null references lease_templates(id),
  is_demo       boolean not null,
  created_by    text not null,
  created_at    timestamptz not null default now(),
  unique (case_id, version)
);

create table if not exists onboarding_approvals (
  id                    uuid primary key default gen_random_uuid(),
  case_id               uuid not null references onboarding_cases(id),
  agreement_version_id  uuid not null references onboarding_agreement_versions(id),
  party                 text not null check (party in ('tenant', 'owner')),
  person_id             uuid not null references onboarding_people(id),
  decision              text not null check (decision in ('approved', 'changes_requested')),
  note                  text check (note is null or length(note) <= 2000),
  actor                 text not null,
  channel               text not null check (channel in ('secure_link', 'whatsapp')),
  created_at            timestamptz not null default now()
);
create index if not exists onboarding_approvals_version_idx on onboarding_approvals (agreement_version_id, party, created_at desc);

create table if not exists onboarding_final_documents (
  id                    uuid primary key default gen_random_uuid(),
  case_id               uuid not null unique references onboarding_cases(id),
  agreement_version_id  uuid not null unique references onboarding_agreement_versions(id),
  storage_path          text not null unique,
  sha256                text not null,
  size_bytes            integer not null,
  is_demo               boolean not null,
  created_by            text not null,
  created_at            timestamptz not null default now()
);

create table if not exists onboarding_access_links (
  id            uuid primary key default gen_random_uuid(),
  case_id       uuid not null references onboarding_cases(id),
  party         text not null check (party in ('tenant', 'owner')),
  person_id     uuid not null references onboarding_people(id),
  token_hash    text not null unique,
  expires_at    timestamptz not null,
  revoked_at    timestamptz,
  created_by    text not null,
  last_used_at  timestamptz,
  created_at    timestamptz not null default now()
);
create index if not exists onboarding_access_links_case_idx on onboarding_access_links (case_id, party);

create table if not exists onboarding_whatsapp_messages (
  id                   uuid primary key default gen_random_uuid(),
  case_id              uuid not null references onboarding_cases(id),
  kind                 text not null check (kind in ('invite', 'final_pdf')),
  party                text not null check (party in ('tenant', 'owner')),
  to_phone             text not null,
  final_document_id    uuid references onboarding_final_documents(id),
  idempotency_key      text not null unique,
  -- accepted = WhatsApp accepted the request; sent/delivered/read only come from provider status webhooks.
  status               text not null default 'queued'
                       check (status in ('queued', 'sending', 'accepted', 'sent', 'delivered', 'read', 'failed')),
  test_mode            boolean not null,
  provider_message_id  text unique,
  error                text,
  attempts             integer not null default 0,
  status_updated_at    timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);
alter table onboarding_whatsapp_messages drop constraint if exists onboarding_whatsapp_messages_status_check;
alter table onboarding_whatsapp_messages add constraint onboarding_whatsapp_messages_status_check
  check (status in ('queued', 'sending', 'accepted', 'sent', 'delivered', 'read', 'failed'));
create index if not exists onboarding_whatsapp_messages_case_idx on onboarding_whatsapp_messages (case_id, kind, party);

create table if not exists onboarding_whatsapp_status_events (
  event_key            text primary key,
  provider_message_id  text not null,
  status               text not null,
  provider_timestamp   timestamptz,
  payload              jsonb not null default '{}'::jsonb,
  received_at          timestamptz not null default now()
);

create table if not exists onboarding_party_progress (
  case_id               uuid not null references onboarding_cases(id),
  party                 text not null check (party in ('tenant', 'owner')),
  confirmed             jsonb not null default '{}'::jsonb,
  awaiting              text,
  invited_at            timestamptz,
  first_response_at     timestamptz,
  self_service_done_at  timestamptz,
  staff_help_requested  boolean not null default false,
  updated_at            timestamptz not null default now(),
  primary key (case_id, party)
);

create table if not exists onboarding_escalations (
  id           uuid primary key default gen_random_uuid(),
  case_id      uuid not null references onboarding_cases(id),
  party        text check (party in ('tenant', 'owner', 'staff')),
  category     text not null check (category in ('help_request', 'legal', 'pricing', 'conflict', 'documents', 'other')),
  message      text not null check (length(message) <= 2000),
  handoff      jsonb not null,
  status       text not null default 'open' check (status in ('open', 'resolved')),
  resolved_by  text,
  resolved_at  timestamptz,
  created_at   timestamptz not null default now()
);
create index if not exists onboarding_escalations_case_idx on onboarding_escalations (case_id, status);

create table if not exists onboarding_audit_log (
  id          bigint generated always as identity primary key,
  case_id     uuid references onboarding_cases(id),
  actor       text not null,
  action      text not null,
  details     jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);
create index if not exists onboarding_audit_log_case_idx on onboarding_audit_log (case_id, id desc);

-- Audit entries, approvals and agreement versions are append-only history.
create or replace function onboarding_append_only() returns trigger language plpgsql as $$
begin
  raise exception '% is append-only', tg_table_name;
end;
$$;
drop trigger if exists onboarding_audit_log_append_only on onboarding_audit_log;
create trigger onboarding_audit_log_append_only before update or delete on onboarding_audit_log
  for each row execute function onboarding_append_only();
drop trigger if exists onboarding_approvals_append_only on onboarding_approvals;
create trigger onboarding_approvals_append_only before update or delete on onboarding_approvals
  for each row execute function onboarding_append_only();
drop trigger if exists onboarding_versions_append_only on onboarding_agreement_versions;
create trigger onboarding_versions_append_only before update or delete on onboarding_agreement_versions
  for each row execute function onboarding_append_only();
drop trigger if exists onboarding_final_documents_append_only on onboarding_final_documents;
create trigger onboarding_final_documents_append_only before update or delete on onboarding_final_documents
  for each row execute function onboarding_append_only();

-- Link finalized cases into the existing tenant/tenancy history.
alter table tenants add column if not exists case_id uuid unique references onboarding_cases(id);
alter table tenants alter column source_onboarding_id drop not null;
alter table tenancies add column if not exists case_id uuid unique references onboarding_cases(id);
alter table tenancies alter column onboarding_id drop not null;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'tenants_has_source') then
    alter table tenants add constraint tenants_has_source check (source_onboarding_id is not null or case_id is not null);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'tenancies_has_source') then
    alter table tenancies add constraint tenancies_has_source check (onboarding_id is not null or case_id is not null);
  end if;
end $$;

-- ---------------------------------------------------------------------
-- Server-only access (the app uses the service_role key)
-- ---------------------------------------------------------------------
alter table onboarding_people enable row level security;
alter table onboarding_staff enable row level security;
alter table lease_templates enable row level security;
alter table onboarding_document_requirements enable row level security;
alter table onboarding_cases enable row level security;
alter table onboarding_case_documents enable row level security;
alter table onboarding_agreement_versions enable row level security;
alter table onboarding_approvals enable row level security;
alter table onboarding_final_documents enable row level security;
alter table onboarding_access_links enable row level security;
alter table onboarding_whatsapp_messages enable row level security;
alter table onboarding_whatsapp_status_events enable row level security;
alter table onboarding_party_progress enable row level security;
alter table onboarding_escalations enable row level security;
alter table onboarding_audit_log enable row level security;
revoke all on onboarding_people, onboarding_staff, lease_templates, onboarding_document_requirements,
  onboarding_cases, onboarding_case_documents, onboarding_agreement_versions, onboarding_approvals,
  onboarding_final_documents, onboarding_access_links, onboarding_whatsapp_messages,
  onboarding_whatsapp_status_events, onboarding_party_progress, onboarding_escalations, onboarding_audit_log
  from anon, authenticated;
revoke all on sequence onboarding_case_ref_seq from anon, authenticated;

-- ---------------------------------------------------------------------
-- Workflow functions. Each locks the case row so simultaneous requests are
-- applied one at a time, and returns jsonb.
-- ---------------------------------------------------------------------

-- Staff/party edits with an optimistic version check.
create or replace function onboarding_update_case(p_case_id uuid, p_expected_version integer, p_changes jsonb, p_actor text, p_action text)
returns jsonb language plpgsql security invoker as $$
declare
  c onboarding_cases;
begin
  select * into c from onboarding_cases where id = p_case_id for update;
  if not found then raise exception 'CASE_NOT_FOUND'; end if;
  if c.status <> 'active' then raise exception 'CASE_NOT_ACTIVE'; end if;
  if p_expected_version is not null and c.version <> p_expected_version then raise exception 'VERSION_CONFLICT'; end if;
  -- Finalization only happens through onboarding_finalize_case.
  if p_changes ? 'status' and p_changes->>'status' <> 'cancelled' then raise exception 'INVALID_STATUS_CHANGE'; end if;

  update onboarding_cases set
    unit             = coalesce(p_changes->>'unit', unit),
    property_address = coalesce(p_changes->>'property_address', property_address),
    tenant_id        = case when p_changes ? 'tenant_id' then (p_changes->>'tenant_id')::uuid else tenant_id end,
    owner_id         = case when p_changes ? 'owner_id' then (p_changes->>'owner_id')::uuid else owner_id end,
    staff_id         = case when p_changes ? 'staff_id' then (p_changes->>'staff_id')::uuid else staff_id end,
    template_id      = case when p_changes ? 'template_id' then (p_changes->>'template_id')::uuid else template_id end,
    terms            = coalesce(p_changes->'terms', terms),
    preferences      = coalesce(p_changes->'preferences', preferences),
    consent          = coalesce(p_changes->'consent', consent),
    signature_method = coalesce(p_changes->>'signature_method', signature_method),
    staff_review     = case when p_changes ? 'staff_review' then p_changes->'staff_review' else staff_review end,
    turbotenant      = coalesce(p_changes->'turbotenant', turbotenant),
    current_step     = coalesce((p_changes->>'current_step')::integer, current_step),
    status           = coalesce(p_changes->>'status', status),
    version          = version + 1,
    updated_at       = now()
  where id = p_case_id returning * into c;

  insert into onboarding_audit_log (case_id, actor, action, details)
  values (p_case_id, p_actor, p_action, p_changes - 'terms' - 'preferences' - 'consent'
          || jsonb_build_object('changed_fields', (select coalesce(jsonb_agg(k), '[]'::jsonb) from jsonb_object_keys(p_changes) k)));
  return to_jsonb(c);
end;
$$;

-- New agreement version from the current, validated details. If nothing
-- changed since the latest version, that version is returned unchanged.
create or replace function onboarding_create_agreement_version(p_case_id uuid, p_snapshot jsonb, p_hash text, p_template_id uuid, p_is_demo boolean, p_actor text)
returns jsonb language plpgsql security invoker as $$
declare
  c onboarding_cases;
  latest onboarding_agreement_versions;
  created onboarding_agreement_versions;
begin
  select * into c from onboarding_cases where id = p_case_id for update;
  if not found then raise exception 'CASE_NOT_FOUND'; end if;
  if c.status <> 'active' then raise exception 'CASE_NOT_ACTIVE'; end if;

  select * into latest from onboarding_agreement_versions where case_id = p_case_id order by version desc limit 1;
  if found and latest.content_hash = p_hash then
    return to_jsonb(latest) || jsonb_build_object('created', false);
  end if;

  insert into onboarding_agreement_versions (case_id, version, snapshot, content_hash, template_id, is_demo, created_by)
  values (p_case_id, coalesce(latest.version, 0) + 1, p_snapshot, p_hash, p_template_id, p_is_demo, p_actor)
  returning * into created;

  -- Approvals and staff review belong to a version; a new version needs them again.
  update onboarding_cases set staff_review = null, version = version + 1, updated_at = now() where id = p_case_id;
  insert into onboarding_audit_log (case_id, actor, action, details)
  values (p_case_id, p_actor, 'agreement_version_created',
          jsonb_build_object('version', created.version, 'content_hash', p_hash, 'previous_version', latest.version,
                             'previous_approvals_invalidated', latest.version is not null));
  return to_jsonb(created) || jsonb_build_object('created', true);
end;
$$;

-- Tenant/owner decision on one exact agreement version.
create or replace function onboarding_record_approval(p_case_id uuid, p_version_id uuid, p_party text, p_person_id uuid,
                                                     p_decision text, p_note text, p_actor text, p_channel text)
returns jsonb language plpgsql security invoker as $$
declare
  c onboarding_cases;
  latest onboarding_agreement_versions;
  previous onboarding_approvals;
  created onboarding_approvals;
begin
  select * into c from onboarding_cases where id = p_case_id for update;
  if not found then raise exception 'CASE_NOT_FOUND'; end if;
  if c.status <> 'active' then raise exception 'CASE_NOT_ACTIVE'; end if;
  if p_party not in ('tenant', 'owner') then raise exception 'WRONG_PERSON'; end if;
  if (p_party = 'tenant' and c.tenant_id is distinct from p_person_id)
     or (p_party = 'owner' and c.owner_id is distinct from p_person_id) then
    raise exception 'WRONG_PERSON';
  end if;

  select * into latest from onboarding_agreement_versions where case_id = p_case_id order by version desc limit 1;
  if not found or latest.id <> p_version_id then raise exception 'OUTDATED_AGREEMENT_VERSION'; end if;

  select * into previous from onboarding_approvals
  where agreement_version_id = p_version_id and party = p_party order by created_at desc limit 1;
  if found and previous.decision = p_decision and previous.note is not distinct from p_note then
    return to_jsonb(previous) || jsonb_build_object('created', false);   -- repeated request
  end if;

  insert into onboarding_approvals (case_id, agreement_version_id, party, person_id, decision, note, actor, channel)
  values (p_case_id, p_version_id, p_party, p_person_id, p_decision, p_note, p_actor, p_channel)
  returning * into created;
  update onboarding_cases set version = version + 1, updated_at = now() where id = p_case_id;
  insert into onboarding_audit_log (case_id, actor, action, details)
  values (p_case_id, p_actor, 'agreement_' || p_decision,
          jsonb_build_object('party', p_party, 'agreement_version', latest.version, 'channel', p_channel, 'note', p_note));
  return to_jsonb(created) || jsonb_build_object('created', true);
end;
$$;

-- Final checks and finalization in one transaction. The PDF was generated
-- from exactly p_version_id's snapshot and stored privately before this call.
create or replace function onboarding_finalize_case(p_case_id uuid, p_expected_version integer, p_version_id uuid,
                                                   p_storage_path text, p_sha256 text, p_size integer, p_actor text)
returns jsonb language plpgsql security invoker as $$
declare
  c onboarding_cases;
  latest onboarding_agreement_versions;
  existing onboarding_final_documents;
  created onboarding_final_documents;
  missing_docs integer;
  linked_tenant uuid;
  tenant_person onboarding_people;
  home properties;
begin
  select * into c from onboarding_cases where id = p_case_id for update;
  if not found then raise exception 'CASE_NOT_FOUND'; end if;

  select * into existing from onboarding_final_documents where case_id = p_case_id;
  if found then
    return to_jsonb(existing) || jsonb_build_object('created', false);   -- repeated request
  end if;

  if c.status <> 'active' then raise exception 'CASE_NOT_ACTIVE'; end if;
  if c.version <> p_expected_version then raise exception 'VERSION_CONFLICT'; end if;

  select * into latest from onboarding_agreement_versions where case_id = p_case_id order by version desc limit 1;
  if not found or latest.id <> p_version_id then raise exception 'OUTDATED_AGREEMENT_VERSION'; end if;

  if coalesce((select decision from onboarding_approvals where agreement_version_id = p_version_id and party = 'tenant'
               order by created_at desc limit 1), '') <> 'approved' then
    raise exception 'TENANT_APPROVAL_MISSING';
  end if;
  if coalesce((select decision from onboarding_approvals where agreement_version_id = p_version_id and party = 'owner'
               order by created_at desc limit 1), '') <> 'approved' then
    raise exception 'OWNER_APPROVAL_MISSING';
  end if;
  if c.staff_review is null or (c.staff_review->>'agreement_version')::integer is distinct from latest.version then
    raise exception 'STAFF_REVIEW_MISSING';
  end if;
  if c.signature_method = 'external_esign' then
    raise exception 'SIGNING_NOT_AVAILABLE';   -- no e-signature provider is connected
  end if;

  with reqs as (
    select distinct on (party, doc_key) party, doc_key, required, active
    from onboarding_document_requirements
    where jurisdiction = c.jurisdiction and (property_id is null or property_id = c.property_id)
    order by party, doc_key, (property_id is null)
  )
  select count(*) into missing_docs from reqs r
  where r.active and r.required and coalesce((
    select d.status from onboarding_case_documents d
    where d.case_id = c.id and d.party = r.party and d.doc_key = r.doc_key
    order by d.created_at desc limit 1), '') <> 'accepted';
  if missing_docs > 0 then raise exception 'DOCUMENTS_NOT_ACCEPTED'; end if;

  select * into home from properties where id = c.property_id for update;
  if not found or home.status in ('let', 'sold')
     or exists (select 1 from tenancies where property_id = c.property_id and status = 'active') then
    raise exception 'PROPERTY_UNAVAILABLE';
  end if;

  insert into onboarding_final_documents (case_id, agreement_version_id, storage_path, sha256, size_bytes, is_demo, created_by)
  values (p_case_id, p_version_id, p_storage_path, p_sha256, p_size, latest.is_demo, p_actor)
  returning * into created;

  select * into tenant_person from onboarding_people where id = c.tenant_id;
  insert into tenants (full_name, phone, case_id) values (tenant_person.full_name, tenant_person.whatsapp, p_case_id)
  on conflict (case_id) do nothing;
  select id into linked_tenant from tenants where case_id = p_case_id;
  insert into tenancies (tenant_id, property_id, case_id, rent, deposit, lease_start, lease_end, move_in_date)
  values (linked_tenant, c.property_id, p_case_id,
          round((latest.snapshot->'terms'->>'rent')::numeric)::integer, round((latest.snapshot->'terms'->>'deposit')::numeric)::integer,
          (latest.snapshot->'terms'->>'lease_start')::date, (latest.snapshot->'terms'->>'lease_end')::date,
          (latest.snapshot->'terms'->>'move_in_date')::date)
  on conflict (case_id) do nothing;
  update properties set status = 'let' where id = c.property_id;

  update onboarding_cases set status = 'finalized', current_step = 7, finalized_at = now(), version = version + 1, updated_at = now()
  where id = p_case_id;
  insert into onboarding_audit_log (case_id, actor, action, details)
  values (p_case_id, p_actor, 'case_finalized',
          jsonb_build_object('agreement_version', latest.version, 'content_hash', latest.content_hash, 'pdf_sha256', p_sha256));
  return to_jsonb(created) || jsonb_build_object('created', true);
end;
$$;

revoke all on function onboarding_update_case(uuid, integer, jsonb, text, text) from public, anon, authenticated;
revoke all on function onboarding_create_agreement_version(uuid, jsonb, text, uuid, boolean, text) from public, anon, authenticated;
revoke all on function onboarding_record_approval(uuid, uuid, text, uuid, text, text, text, text) from public, anon, authenticated;
revoke all on function onboarding_finalize_case(uuid, integer, uuid, text, text, integer, text) from public, anon, authenticated;
grant execute on function onboarding_update_case(uuid, integer, jsonb, text, text) to service_role;
grant execute on function onboarding_create_agreement_version(uuid, jsonb, text, uuid, boolean, text) to service_role;
grant execute on function onboarding_record_approval(uuid, uuid, text, uuid, text, text, text, text) to service_role;
grant execute on function onboarding_finalize_case(uuid, integer, uuid, text, text, integer, text) to service_role;

-- ---------------------------------------------------------------------
-- Seed: labelled DEMO template and a starter North Carolina checklist.
-- The demo template is for testing only and is never production-ready.
-- ---------------------------------------------------------------------
insert into lease_templates (name, jurisdiction, template_version, status, clauses, notes)
values ('DEMO residential lease (testing only)', 'US-NC', 1, 'demo', $json$[
  {"title": "Demo template notice", "body": "This agreement was produced from a DEMO template for software testing. It has not been reviewed by a lawyer or approved for use, and it must not be used as a real lease."},
  {"title": "Parties and premises", "body": "The Owner agrees to rent the Premises described in this agreement to the Tenant, and the Tenant agrees to rent the Premises from the Owner, on the terms set out in this agreement."},
  {"title": "Term", "body": "The lease starts and ends on the dates shown in the Lease term section. The Tenant may move in on the move-in date shown."},
  {"title": "Rent and payments", "body": "The Tenant will pay the rent and agreed charges shown in this agreement, in the currency and on the schedule shown."},
  {"title": "Security deposit", "body": "The Tenant will pay the security deposit shown. It will be held and returned as required by applicable law."},
  {"title": "Occupants", "body": "Only the people listed in the Occupants section may live at the Premises unless the Owner agrees otherwise in writing."},
  {"title": "Additional agreed terms", "body": "Any additional terms listed in this agreement were agreed by both parties and form part of this agreement."},
  {"title": "Entire agreement", "body": "This agreement, together with the details listed in it, is the entire agreement between the parties about the rental of the Premises."}
]$json$::jsonb, 'Seeded by supabase_lease_onboarding.sql for testing. Replace with a lawyer-approved template before real use.')
on conflict (name, template_version) do nothing;

insert into onboarding_document_requirements (jurisdiction, property_id, party, doc_key, label, reason, required)
values
  ('US-NC', null, 'tenant', 'photo_id', 'Photo ID', 'Confirms the person signing the lease is the tenant named in it.', true),
  ('US-NC', null, 'tenant', 'proof_of_income', 'Proof of income', 'Confirms the agreed rent is affordable, as reviewed during the application.', true),
  ('US-NC', null, 'owner', 'ownership_authority', 'Proof of ownership or authority to rent', 'Confirms the owner named in the lease has the right to rent out the home.', true)
on conflict (jurisdiction, property_id, party, doc_key) do nothing;
