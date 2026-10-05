-- Tenant-to-property linking. Run after supabase_onboarding.sql.
-- Non-destructive: only adds tables, an index, a trigger, a view and a new
-- version of save_resident_onboarding (same signature). Safe to run again.
--
-- Model:
--   properties (existing id, e.g. 'omr-3bhk')  1 ── many  tenancies  many ── 1  tenants
--   resident_onboardings.property_id  (fixed once onboarding starts)
--   tenancies.onboarding_id           (one tenancy per completed onboarding)
-- A property keeps every past tenancy; only one tenancy per property can be active.

-- ---------------------------------------------------------------------
-- Tenants: a person who completed onboarding. Created exactly once per
-- onboarding (source_onboarding_id is unique) and never matched by name.
-- ---------------------------------------------------------------------
create sequence if not exists tenant_ref_seq;

create table if not exists tenants (
  id                    uuid primary key default gen_random_uuid(),
  tenant_ref            text not null unique
                        default ('TEN-' || lpad(nextval('tenant_ref_seq')::text, 6, '0')),
  full_name             text not null,
  phone                 text,
  source_onboarding_id  uuid not null unique references resident_onboardings(id),
  created_at            timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Tenancies: which tenant occupies which existing property, and when.
-- ---------------------------------------------------------------------
create table if not exists tenancies (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id),
  property_id    text not null references properties(id),
  onboarding_id  uuid not null unique references resident_onboardings(id),
  status         text not null default 'active' check (status in ('active', 'ended')),
  rent           integer check (rent is null or rent >= 0),
  deposit        integer check (deposit is null or deposit >= 0),
  lease_start    date,
  lease_end      date,
  move_in_date   date,
  started_at     timestamptz not null default now(),
  ended_at       timestamptz,
  created_at     timestamptz not null default now(),
  constraint tenancies_ended_at_matches_status
    check ((status = 'active' and ended_at is null) or (status = 'ended' and ended_at is not null))
);

-- Tenancy history is kept; at most one ACTIVE tenancy per property.
create unique index if not exists tenancies_one_active_per_property
  on tenancies (property_id) where status = 'active';
create index if not exists tenancies_property_history_idx
  on tenancies (property_id, started_at desc);
create index if not exists tenancies_tenant_idx on tenancies (tenant_id);

-- Server-only, like every other table.
alter table tenants enable row level security;
alter table tenancies enable row level security;
revoke all on tenants, tenancies from anon, authenticated;
-- Defence in depth for the existing table: RLS already returns no rows to the
-- public roles, and the app reads it with the server key only.
revoke all on resident_onboardings from anon, authenticated;
revoke all on sequence tenant_ref_seq from anon, authenticated;

-- ---------------------------------------------------------------------
-- The property chosen when onboarding starts can never be changed silently.
-- ---------------------------------------------------------------------
create or replace function prevent_onboarding_property_change()
returns trigger language plpgsql as $$
begin
  if new.property_id is distinct from old.property_id then
    raise exception 'Property assignment for onboarding % cannot be changed (% -> %)',
      old.id, old.property_id, new.property_id;
  end if;
  return new;
end;
$$;

drop trigger if exists resident_onboardings_property_fixed on resident_onboardings;
create trigger resident_onboardings_property_fixed
before update of property_id on resident_onboardings
for each row execute function prevent_onboarding_property_change();

-- ---------------------------------------------------------------------
-- Completion: same verified, locked process as before, now also creating
-- the tenant and tenancy in the same transaction. Retrying a completion
-- that already succeeded returns the completed record and creates nothing.
-- Saving a draft (finish = false) never touches the property.
-- ---------------------------------------------------------------------
create or replace function save_resident_onboarding(record_id uuid, expected_version integer, new_data jsonb, finish boolean)
returns setof resident_onboardings language plpgsql security invoker as $$
declare
  current_record resident_onboardings;
  available_property properties;
  linked_tenant_id uuid;
begin
  select * into current_record from resident_onboardings where id = record_id for update;
  if not found then raise exception 'Record missing'; end if;

  if current_record.status = 'completed' then
    if finish then
      -- Completion retry (e.g. the first response was lost): no new tenant.
      return query select * from resident_onboardings where id = record_id;
      return;
    end if;
    raise exception 'Record changed or already completed';
  end if;

  if current_record.version <> expected_version then
    raise exception 'Record changed or already completed';
  end if;

  if finish then
    select * into available_property from properties where id = current_record.property_id for update;
    if not found or available_property.status <> 'active' or available_property.listing_type <> 'rent' then
      raise exception 'Property unavailable';
    end if;
    if exists (select 1 from tenancies where property_id = current_record.property_id and status = 'active') then
      raise exception 'Property unavailable';
    end if;

    insert into tenants (full_name, phone, source_onboarding_id)
    values (nullif(btrim(new_data->>'resident_name'), ''), nullif(btrim(new_data->>'phone'), ''), record_id)
    on conflict (source_onboarding_id) do nothing;

    select id into linked_tenant_id from tenants where source_onboarding_id = record_id;

    insert into tenancies (tenant_id, property_id, onboarding_id, rent, deposit, lease_start, lease_end, move_in_date)
    values (
      linked_tenant_id, current_record.property_id, record_id,
      (new_data->>'rent')::integer, (new_data->>'deposit')::integer,
      (new_data->>'lease_start')::date, (new_data->>'lease_end')::date, (new_data->>'move_in_date')::date
    )
    on conflict (onboarding_id) do nothing;

    update properties set status = 'let' where id = current_record.property_id;
  end if;

  return query update resident_onboardings set data = new_data,
    status = case when finish then 'completed' else 'in_progress' end,
    completed_at = case when finish then now() else null end,
    version = version + 1, updated_at = now()
    where id = record_id returning *;
end;
$$;
revoke all on function save_resident_onboarding(uuid, integer, jsonb, boolean) from public, anon, authenticated;
grant execute on function save_resident_onboarding(uuid, integer, jsonb, boolean) to service_role;

-- ---------------------------------------------------------------------
-- Backfill onboardings completed before this migration: one tenant and one
-- tenancy each. The newest completion per property stays active if the
-- property is still let; older ones are recorded as ended.
-- ---------------------------------------------------------------------
insert into tenants (full_name, phone, source_onboarding_id, created_at)
select coalesce(nullif(btrim(o.data->>'resident_name'), ''), 'Resident (name not recorded)'),
       nullif(btrim(o.data->>'phone'), ''), o.id, coalesce(o.completed_at, o.updated_at)
from resident_onboardings o
where o.status = 'completed'
order by coalesce(o.completed_at, o.updated_at), o.id
on conflict (source_onboarding_id) do nothing;

insert into tenancies (tenant_id, property_id, onboarding_id, status, rent, deposit,
                       lease_start, lease_end, move_in_date, started_at, ended_at)
select t.id, o.property_id, o.id,
       case when ranked.newest and p.status = 'let'
                 and not exists (select 1 from tenancies a where a.property_id = o.property_id and a.status = 'active')
            then 'active' else 'ended' end,
       (o.data->>'rent')::integer, (o.data->>'deposit')::integer,
       (o.data->>'lease_start')::date, (o.data->>'lease_end')::date, (o.data->>'move_in_date')::date,
       coalesce(o.completed_at, o.updated_at),
       case when ranked.newest and p.status = 'let'
                 and not exists (select 1 from tenancies a where a.property_id = o.property_id and a.status = 'active')
            then null else coalesce(ranked.next_completed_at, now()) end
from resident_onboardings o
join tenants t on t.source_onboarding_id = o.id
join properties p on p.id = o.property_id
join (
  select id,
         row_number() over (partition by property_id order by coalesce(completed_at, updated_at) desc) = 1 as newest,
         lead(coalesce(completed_at, updated_at)) over (partition by property_id order by coalesce(completed_at, updated_at)) as next_completed_at
  from resident_onboardings where status = 'completed'
) ranked on ranked.id = o.id
where o.status = 'completed'
  and not exists (select 1 from tenancies x where x.onboarding_id = o.id);

-- ---------------------------------------------------------------------
-- One row per onboarding with its property reference and linked tenant.
-- security_invoker: reading it needs the same rights as the tables (server only).
-- ---------------------------------------------------------------------
create or replace view resident_onboarding_details with (security_invoker = true) as
select o.id, o.conversation_id, o.property_id, o.property_title, o.data, o.status,
       o.version, o.completed_at, o.created_at, o.updated_at,
       p.ref    as property_ref,
       p.title  as property_name,
       p.status as property_status,
       tn.id         as tenancy_id,
       tn.status     as tenancy_status,
       tn.started_at as tenancy_started_at,
       tn.ended_at   as tenancy_ended_at,
       t.id          as tenant_id,
       t.tenant_ref,
       t.full_name   as tenant_name
from resident_onboardings o
join properties p on p.id = o.property_id
left join tenancies tn on tn.onboarding_id = o.id
left join tenants t on t.id = tn.tenant_id;

revoke all on resident_onboarding_details from anon, authenticated;
grant select on resident_onboarding_details to service_role;
