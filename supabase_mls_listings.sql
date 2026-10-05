-- Homes for sale from an MLS export (CSV). Run once in Supabase:
-- SQL Editor -> New query -> paste -> Run. Only adds new tables; safe to run again.
--
-- MLS data is licensed: it stays server-side (team pages only, RLS on, no public
-- endpoint) and is re-imported from a fresh export rather than edited by hand.

create table if not exists mls_listings (
  list_number          text primary key,
  status               text not null,                 -- raw MLS code: A, P, H, ...
  status_label         text not null default 'unknown',
  property_type        text,
  property_sub_type    text,
  street_address       text,
  unit_number          text,
  city                 text,
  state                text,
  county               text,
  postal_code          text,
  subdivision          text,
  latitude             numeric(9, 6),
  longitude            numeric(9, 6),
  bedrooms             integer,
  bathrooms_full       integer,
  bathrooms_half       integer,
  living_area          integer,                       -- square feet
  lot_size             text,
  stories              integer,
  year_built           integer,
  garage_spaces        numeric(4, 1),
  list_price           numeric(14, 2),
  original_list_price  numeric(14, 2),
  close_price          numeric(14, 2),
  tax_assessed_value   numeric(14, 2),
  tax_annual           numeric(12, 2),
  hoa_fee              numeric(12, 2),
  hoa_frequency        text,
  listing_date         date,
  close_date           date,
  agency_name          text,
  listing_agent        text,
  agency_phone         text,
  remarks              text,
  raw                  jsonb not null default '{}'::jsonb,
  source_file          text,
  imported_at          timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);
create index if not exists mls_listings_search_idx on mls_listings (status_label, city, list_price);
create index if not exists mls_listings_price_idx on mls_listings (list_price);

create table if not exists mls_imports (
  id           uuid primary key default gen_random_uuid(),
  file_name    text not null,
  row_count    integer not null default 0,
  inserted     integer not null default 0,
  updated      integer not null default 0,
  skipped      integer not null default 0,
  created_by   text not null,
  created_at   timestamptz not null default now()
);

drop trigger if exists mls_listings_set_updated_at on mls_listings;
create trigger mls_listings_set_updated_at before update on mls_listings
for each row execute function set_updated_at();

alter table mls_listings enable row level security;
alter table mls_imports enable row level security;
revoke all on mls_listings, mls_imports from anon, authenticated;

-- Main photo and a few extra facts from the export.
alter table mls_listings add column if not exists photo_url text;
alter table mls_listings add column if not exists days_on_market integer;
alter table mls_listings add column if not exists price_per_sqft numeric(10, 2);
alter table mls_listings add column if not exists lot_acres numeric(10, 3);
alter table mls_listings add column if not exists neighborhood text;
alter table mls_listings add column if not exists new_construction boolean;
alter table mls_listings add column if not exists parking_total integer;
