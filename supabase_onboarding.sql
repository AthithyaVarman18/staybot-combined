-- Run after supabase_schema.sql and supabase_properties.sql.
-- This record holds the resident profile, lease terms and document review status.
create table if not exists resident_onboardings (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null unique references conversations(id),
  property_id text not null references properties(id),
  property_title text not null,
  data jsonb not null,
  status text not null default 'in_progress' check(status in ('in_progress','completed')),
  version integer not null default 1,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table resident_onboardings enable row level security;

create or replace function save_resident_onboarding(record_id uuid, expected_version integer, new_data jsonb, finish boolean)
returns setof resident_onboardings language plpgsql security invoker as $$
declare current_record resident_onboardings; available_property properties;
begin
  select * into current_record from resident_onboardings where id = record_id for update;
  if not found then raise exception 'Record missing'; end if;
  if current_record.version <> expected_version or current_record.status = 'completed' then
    raise exception 'Record changed or already completed';
  end if;
  if finish then
    select * into available_property from properties where id = current_record.property_id for update;
    if available_property.status <> 'active' or available_property.listing_type <> 'rent' then
      raise exception 'Property unavailable';
    end if;
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
