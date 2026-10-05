-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Moves an existing Staybot database from the old Chennai demo setup to North Carolina.
-- Safe to run again. It never deletes anything.

-- 1. New homes saved without a city were labelled 'Chennai' by the column default.
alter table properties alter column city set default 'North Carolina';

-- 2. Homes that got that 'Chennai' default but are really in North Carolina
--    (their location says ", NC" or "North Carolina"): fix the city first, so
--    step 3 doesn't hide them.
update properties
   set city = 'North Carolina'
 where city = 'Chennai'
   and location ~* '(,\s*NC\M|north carolina)';

-- 3. Hide the old Chennai demo listings that are still there, so tenants and
--    the AI never see them. They stay in the table - set status back to
--    'active' to undo.
update properties
   set status = 'hidden'
 where status = 'active'
   and (city ilike '%chennai%' or location ilike '%chennai%'
        or id in ('omr-3bhk', 'adyar-2bhk', 'velachery-1bhk', 'ecr-villa', 'perungudi-2bhk', 'navalur-3bhk',
                  'annanagar-3bhk', 'tnagar-2bhk-sale', 'porur-2bhk', 'guindy-1bhk', 'tambaram-3bhk-sale',
                  'sholinganallur-4bhk', 'medavakkam-2bhk', 'kilpauk-studio'));
