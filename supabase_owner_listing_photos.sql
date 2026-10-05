-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Lets owners attach photos to a listing draft through the chat.
-- Needs supabase_owner_listings.sql to have been run first.
--
-- Photos themselves are NOT stored in this table - they're uploaded to a
-- PUBLIC Storage bucket called "property-photos" (created automatically
-- by src/services/property_photos.py the first time an owner attaches
-- one). This column just holds their public URLs.

alter table properties add column if not exists photos jsonb not null default '[]'::jsonb;
