-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Creates the properties table the AI searches, and adds 14 sample North Carolina listings.
-- (Only for a fresh setup - if your table already has your own homes you don't need to rerun this.)
-- Safe to run again: existing listings with the same id are updated, not duplicated.

create table if not exists properties (
  id              text primary key,                 -- short slug used by the app, e.g. 'garner-3br'
  ref             text,                             -- your own reference code
  title           text not null,
  area            text,                             -- neighbourhood shown on cards, e.g. 'Sholinganallur'
  city            text default 'North Carolina',
  location        text,                             -- full location text the search matches against
  listing_type    text not null check (listing_type in ('rent', 'sale')),
  property_type   text,                             -- apartment | villa | independent house | studio ...
  bedrooms        int,
  bathrooms       int,
  rent            int,                              -- per month, INR (rent listings)
  sale_price      bigint,                           -- INR (sale listings)
  deposit         int,
  pets_allowed    boolean,                          -- null = not known (the AI will say it needs checking)
  parking         boolean,
  furnished       text,
  amenities       jsonb default '[]'::jsonb,
  available_from  text,
  description     text,
  rating          numeric(3, 2),
  badge           text,
  status          text not null default 'active' check (status in ('active', 'let', 'sold', 'hidden')),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists properties_search_idx on properties (status, listing_type, bedrooms);

drop trigger if exists properties_set_updated_at on properties;
create trigger properties_set_updated_at
before update on properties
for each row execute function set_updated_at();   -- defined in supabase_schema.sql

-- Only the backend (service_role key) can read or write listings.
alter table properties enable row level security;

insert into properties (id, ref, title, area, city, location, listing_type, property_type, bedrooms, bathrooms, rent, sale_price, deposit, pets_allowed, parking, furnished, amenities, available_from, description, rating, badge) values
  ('garner-3br', 'NC-GAR-301', 'Sunlit 3BR near downtown Garner', 'Garner', 'Garner', 'Main St area, Garner, NC', 'rent', 'house', 3, 2, 1850, null, 1850, true, true, 'unfurnished', '["fenced yard", "attached garage", "washer/dryer hookups"]'::jsonb, '2026-10-01', 'Single-family home on a quiet street, ten minutes from I-40 and downtown Raleigh.', 4.92, 'Guest favourite'),
  ('fuquay-2br-townhome', 'NC-FUQ-204', 'Cozy 2BR townhome in Fuquay-Varina', 'Fuquay-Varina', 'Fuquay-Varina', 'Downtown Fuquay-Varina, NC', 'rent', 'townhouse', 2, 2, 1500, null, 1500, false, true, 'unfurnished', '["two assigned spaces", "patio", "community pool"]'::jsonb, 'immediately', 'End-unit townhome a short walk from the shops and restaurants on Main Street.', 4.85, 'Rare find'),
  ('raleigh-1br-glenwood', 'NC-RAL-110', 'Modern 1BR near Glenwood South', 'Glenwood South', 'Raleigh', 'Glenwood South, Raleigh, NC', 'rent', 'apartment', 1, 1, 1400, null, null, null, false, 'unfurnished', '["walk to restaurants", "fitness center"]'::jsonb, '2026-11-15', 'Bright apartment in the heart of Raleigh''s restaurant district.', 4.71, 'New'),
  ('cary-4br-sale', 'NC-CAR-900', '4BR family home for sale in Cary', 'Cary', 'Cary', 'West Cary, NC', 'sale', 'house', 4, 3, null, 525000, null, true, true, 'unfurnished', '["two-car garage", "screened porch", "top-rated schools"]'::jsonb, 'immediately', 'Two-story home on a cul-de-sac with a large backyard and updated kitchen.', 4.97, 'For sale'),
  ('apex-2br', 'NC-APX-212', 'Airy 2BR apartment in Apex', 'Apex', 'Apex', 'Beaver Creek, Apex, NC', 'rent', 'apartment', 2, 2, 1600, null, 1600, true, true, 'unfurnished', '["balcony", "dog park", "in-unit washer/dryer"]'::jsonb, '2026-10-15', 'Second-floor unit close to Beaver Creek Commons shopping.', 4.8, 'Guest favourite'),
  ('holly-springs-3br', 'NC-HSP-305', 'Family 3BR in a Holly Springs neighborhood', 'Holly Springs', 'Holly Springs', 'Holly Springs, NC', 'rent', 'house', 3, 2, 2100, null, 2100, true, true, 'unfurnished', '["community pool", "playground", "two-car garage"]'::jsonb, '2026-10-01', 'Ranch-style home in a family neighborhood with sidewalks and a community pool.', 4.88, 'Rare find'),
  ('durham-3br-furnished', 'NC-DUR-330', 'Furnished 3BR near Duke Park', 'Duke Park', 'Durham', 'Duke Park, Durham, NC', 'rent', 'house', 3, 2, 2400, null, 2400, false, true, 'fully furnished', '["front porch", "home office", "driveway parking"]'::jsonb, 'immediately', 'Fully furnished bungalow across from the park, ten minutes from Duke.', 4.95, 'Guest favourite'),
  ('wake-forest-2br-sale', 'NC-WKF-220', '2BR condo for sale in Wake Forest', 'Wake Forest', 'Wake Forest', 'Downtown Wake Forest, NC', 'sale', 'condo', 2, 2, null, 289000, null, true, true, 'unfurnished', '["assigned parking", "storage unit"]'::jsonb, 'immediately', 'Low-maintenance condo steps from the historic downtown.', 4.6, 'For sale'),
  ('greensboro-2br', 'NC-GSO-118', 'Budget 2BR near UNCG', 'College Hill', 'Greensboro', 'College Hill, Greensboro, NC', 'rent', 'apartment', 2, 1, 1100, null, 1100, false, true, 'unfurnished', '["walk to campus", "off-street parking"]'::jsonb, 'immediately', 'Affordable duplex unit a few blocks from UNCG.', 4.5, 'New'),
  ('charlotte-1br-southend', 'NC-CLT-140', 'Furnished 1BR in South End', 'South End', 'Charlotte', 'South End, Charlotte, NC', 'rent', 'apartment', 1, 1, 1750, null, 1750, true, true, 'fully furnished', '["rooftop terrace", "light rail nearby", "garage parking"]'::jsonb, '2026-10-10', 'Move-in-ready apartment steps from the Rail Trail and light rail.', 4.83, 'Guest favourite'),
  ('wilmington-3br-sale', 'NC-ILM-310', '3BR house for sale near Wrightsville Beach', 'Wrightsville Beach area', 'Wilmington', 'Wilmington, NC', 'sale', 'house', 3, 2, null, 465000, null, true, true, 'unfurnished', '["screened porch", "outdoor shower", "carport"]'::jsonb, 'immediately', 'Coastal cottage about ten minutes from the beach.', 4.9, 'For sale'),
  ('raleigh-4br-north-hills', 'NC-RAL-410', 'Spacious 4BR near North Hills', 'North Hills', 'Raleigh', 'North Hills, Raleigh, NC', 'rent', 'house', 4, 3, 3200, null, 3200, true, true, 'unfurnished', '["two-car garage", "finished basement", "fenced yard"]'::jsonb, '2026-11-01', 'Large home minutes from North Hills shopping and the Beltline.', 4.94, 'Guest favourite'),
  ('garner-2br-duplex', 'NC-GAR-205', 'Simple 2BR duplex in Garner', 'Garner', 'Garner', 'Garner, NC', 'rent', 'duplex', 2, 1, 1250, null, 1250, true, true, 'unfurnished', '["driveway parking", "small yard"]'::jsonb, 'immediately', 'Well-kept duplex side close to White Oak shopping.', 4.55, 'New'),
  ('chapel-hill-studio', 'NC-CHH-001', 'Compact studio near UNC', 'Chapel Hill', 'Chapel Hill', 'Franklin St area, Chapel Hill, NC', 'rent', 'studio', 0, 1, 1150, null, 1150, false, false, 'semi-furnished', '["walk to campus", "bus line"]'::jsonb, '2026-12-01', 'Efficient studio a short walk from Franklin Street and UNC.', 4.62, 'Rare find')
on conflict (id) do update set
  ref = excluded.ref, title = excluded.title, area = excluded.area, city = excluded.city, location = excluded.location, listing_type = excluded.listing_type, property_type = excluded.property_type, bedrooms = excluded.bedrooms, bathrooms = excluded.bathrooms, rent = excluded.rent, sale_price = excluded.sale_price, deposit = excluded.deposit, pets_allowed = excluded.pets_allowed, parking = excluded.parking, furnished = excluded.furnished, amenities = excluded.amenities, available_from = excluded.available_from, description = excluded.description, rating = excluded.rating, badge = excluded.badge;
