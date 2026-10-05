# Real Estate AI

FastAPI service that analyzes tenant/owner messages with OpenRouter, plus an Airbnb-style test page, a Supabase-backed chat history, a leads dashboard, a photo-based maintenance issue classifier, and a guided resident onboarding/lease journey.

> **Security note:** the maintenance classifier merged into this project came from a zip whose `.env` file contained a **live OpenRouter API key and a live Supabase database connection string** committed in plain text. Those files are excluded here (`.env.example` only), but the original key/connection string were exposed to whoever received that zip. If they're still yours, **rotate the OpenRouter key at https://openrouter.ai/settings/keys and reset the Supabase database password immediately**, then put the new values only in your own untracked `.env`.

## Run (macOS / Linux)

```bash
cd real_estate
chmod +x run.sh
./run.sh
```

The first run creates `.env`. Add your API key, save, then run `./run.sh` again.

## Choosing the AI

Set `AI_PROVIDER` in `.env`:

- `gemini` - free key from https://aistudio.google.com/apikey, put it in `GEMINI_API_KEY`
- `openrouter` - key from https://openrouter.ai/settings/keys, put it in `OPENROUTER_API_KEY`

Models are tried in order from `GEMINI_MODELS` / `OPENROUTER_MODEL` (comma-separated). Restart the server after editing `.env`.

Open http://127.0.0.1:8000/ui

Port 8000 busy? Use another one: `./run.sh 8001`

## Run manually

```bash
cp .env.example .env          # then set AI_PROVIDER and add that API key
python3 -m venv .venv
.venv/bin/pip install -r requirement.txt
.venv/bin/uvicorn src.main:app --reload --port 8000
```

On Windows use `.venv\Scripts\pip` and `.venv\Scripts\uvicorn`.

## Database (Supabase) - chat history + leads dashboard

Without this configured, the app still works exactly as before (no memory
between refreshes). With it configured:

- Every chat is saved. Refreshing the page, or switching away from a listing
  and back, resumes the same conversation for that browser.
- The **Leads** tab (top of `/ui`) shows every saved conversation as a scored
  lead, with the full message thread on click.

Setup:

1. Create a free project at https://supabase.com.
2. In the Supabase dashboard, open **SQL Editor -> New query**, paste the
   contents of `supabase_schema.sql` (in this folder), and run it. This
   creates the `conversations` and `messages` tables.
3. In **Project Settings -> API**, copy the **Project URL** and the
   **`service_role`** secret key (not the `anon` key - the backend needs
   the service role key to write on the user's behalf, and it never leaves
   the server).
4. Add them to `.env`:
   ```
   SUPABASE_URL=https://your-project-ref.supabase.co
   SUPABASE_SERVICE_KEY=your-service-role-key
   ```
5. Restart the server (`./run.sh`).

A browser identifies its own chats with a random ID generated on first visit
and stored in `localStorage` (`staybot_session_id`) - there's no login. Each
(browser, listing) pair has one thread; "New chat" clears and deletes it.

## Owner listings (added through the chat)

Built into the same chat/WhatsApp AI as everything else - there's no separate
"list your property" form. Pick **Owner** above the message box (or, on
WhatsApp, just message like an owner) and describe the home; the chat AI
sets `role: "owner"` (`src/prompts/system_prompt.py`) and
`src/services/owner_listings.py` saves/updates a draft listing as you go.

- **Ask up front, not one at a time**: on the owner's very first message,
  the AI asks together for whichever of the four core details it's
  missing - property type, bedrooms, location, and rent/sale price - in a
  single reply, and invites a photo in that same message. Give it all four
  in one go ("I want to list my 2BHK in Anna Nagar for 30k rent") and it
  skips straight to asking for a phone number and a photo instead of
  repeating what you already said.
- **Photos**: the same 📷 button used for maintenance reports also works
  here - attach one or more photos of the property right in the chat (as
  an owner) and each is uploaded to a **public** Supabase Storage bucket
  (`property-photos`, created automatically) and added to the draft's
  `photos` list. They show up on the listing's card in the **New listings**
  tab once uploaded, and stay on the property after it's approved.
- Once there's enough to describe the home (type or bedrooms, location, and
  a rent or sale price), a **pending** draft is created in the `properties`
  table (`source: "owner_chat"`); further messages update the same draft in
  place instead of creating duplicates.
- **New listings tab** (top of `/ui`) - every pending draft, with
  **Approve & publish** (status -> `active`, now visible to tenants) or
  **Reject** (status -> `hidden`), and a **View chat** link back to the
  full conversation.
- Needs `supabase_owner_listings.sql` run in Supabase (after
  `supabase_properties.sql`) to save drafts at all, and
  `supabase_owner_listing_photos.sql` (after that) to save photos. Without
  either, the chat still replies and asks the right questions - it just
  can't save what it collects.

## Property search

Pick **No property** in the chat and describe what you want ("3BHK in OMR
under 40k"). The AI pulls out the requirements, the app searches the
listings with fixed rules (so it can't invent a home), and the best 3
matches appear as cards with a **Chat about this** button. Follow-ups
like "is the second one pet friendly?" are answered from that listing's
real data.

Listings come from the Supabase `properties` table. To create it with 14
sample Chennai homes, run `supabase_properties.sql` in **SQL Editor -> New
query** (after `supabase_schema.sql`). Until then the same samples are read
from `src/data/properties.json`. Add, edit or hide homes (`status` =
`let` / `sold` / `hidden`) in Supabase's **Table Editor**; the app picks up
changes within 30 seconds.

## Maintenance reports (photo classifier, inside the chat)

Built into the same chat/WhatsApp AI that handles everything else - not a
separate page. When a property is selected and the tenant describes
something broken, damaged or not working (rather than asking to rent/buy
it), the chat AI itself sets `intent: "maintenance_issue"`
(`src/prompts/system_prompt.py`), and `src/services/maintenance.py` takes
it from there: it classifies the message - and a photo, if the tenant
attaches one right in the chat - with an OpenRouter vision model
(`src/services/maintenance_ai.py`, adapted from a standalone "Property
Maintenance AI Classifier" service) into an issue type, urgency, and a
recommended next step, independent of the `AI_PROVIDER` setting used for
the rest of the chat.

- **In the Chat tab**: pick a property, then just describe the problem
  ("the kitchen tap is leaking") - the 📷 button next to the message box
  attaches a photo (JPG/PNG/WEBP/GIF, up to `MAINTENANCE_MAX_IMAGE_MB`,
  default 10 MB) to that message or a follow-up one. The AI's reply
  acknowledges the issue and, underneath it, the app appends what actually
  happened ("🔧 Logged as a plumbing issue (urgent priority) for the
  team."). The **View analysis** panel shows the same ticket details.
- **Unit number first**: the very first thing the AI asks, before
  discussing the problem at all, is the tenant's unit/apartment number
  (unless they already gave it in the same message, e.g. "Unit 302, the
  tap is leaking"). Nothing is classified or saved until it's given - it's
  then remembered for the rest of that conversation and stored on the
  ticket (`tenant_id` in `maintenance_tickets`, shown as **Unit** in the
  Maintenance tab).
- **Safety rule**: if the photo and the tenant's text describe different
  things (`photoTextMatch: false`), or a photo can't be confidently
  verified against the text, the ticket is always forced into
  `needs_review` rather than trusted at face value.
- **Follow-ups update the same ticket**: describe the issue, then attach a
  photo a message later (or the other way around) and it updates the still
  -open ticket for that conversation instead of creating a duplicate.
- **Maintenance tab** (top of `/ui`) - the team's dashboard: every saved
  ticket, filterable by workflow status (`needs_review` -> `open` ->
  `in_progress` -> `resolved`, or `dismissed`), with the AI's classification
  and one-click status changes.
- Needs `OPENROUTER_API_KEY` in `.env` (used regardless of `AI_PROVIDER`)
  and, to save/list tickets, `supabase_maintenance_tickets.sql` run in
  Supabase's SQL Editor (needs `supabase_schema.sql` first). Without
  Supabase configured, the chat still classifies and replies, the ticket
  just isn't saved anywhere.

## Resident onboarding (guided lease journey)

**Onboarding** tab → **+ Start onboarding** (or open a lead → **Start onboarding**;
works even if the lead has no chat messages). Each case walks through 7 steps:

1. Let's choose the home. (property + unit + address, responsible staff member)
2. Let's meet the tenant. (name, WhatsApp, email, language, contact preference, WhatsApp consent)
3. Let's confirm the owner.
4. Let's collect the required documents. (upload → staff review: accepted / changes required)
5. Let's agree on the rental terms. (dates, rent, deposit, USD, schedule, charges, occupants, lease/account setup, template, signing)
6. Let's review the agreement together. (versioned draft PDF with DRAFT watermark; tenant and owner approve the same version; staff review)
7. Your final lease PDF is ready. (final PDF from the approved version, private storage, WhatsApp delivery status, TurboTenant manual handoff)

Tenants and owners never need an account: **Send WhatsApp invitation** sends a
personal link (`/p/<token>`, 7 days, one case + one person, revoked when a new
one is sent). On that page they confirm their details, upload documents, read
the draft and approve or request changes, and download the final PDF. On
WhatsApp they can confirm role/name/email/language step by step, reply `LINK`
for a new link and `HELP` for staff. Legal, pricing, conflicting-information
and document problems create an escalation with a handoff summary for staff.

The server (and the database functions) enforce every rule: required details,
reviewed documents, both approvals on the current version, staff review, no
e-signature step without a provider, optimistic version checks, append-only
audit log, approvals, versions and final PDFs.

### Setup

1. Supabase SQL Editor: run `supabase_lease_onboarding.sql` **after**
   `supabase_onboarding.sql` and `supabase_tenancy.sql`. It only adds tables,
   functions and nullable columns; safe to run again.
2. `.env`: `PUBLIC_BASE_URL` (address in the WhatsApp links). The private
   storage bucket `onboarding-private` is created automatically.
3. `.venv/bin/pip install -r requirement.txt` (adds `reportlab` for PDFs), restart the server.
4. Add a lawyer-approved lease template in **Templates & checklist**. Until
   then the seeded **DEMO** template is used and every agreement/PDF says
   "DEMO TEMPLATE - FOR TESTING ONLY".
5. Real WhatsApp: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
   `WHATSAPP_APP_SECRET` (required for delivery webhooks), `WHATSAPP_DRY_RUN=false`,
   plus approved templates `WHATSAPP_TEMPLATE_INVITE` (body: name, home, link) and
   `WHATSAPP_TEMPLATE_LEASE_READY` (document header; body: name, reference).
   Without a template, messages only go to people who wrote to you in the last 24 hours;
   otherwise the send is marked **failed** with the reason. In test mode nothing is sent
   and every status is labelled **Simulated**.

### Local development stack and tests (never touches Supabase)

```bash
bash dev/local_stack.sh                  # local Postgres + Supabase stand-in + app on http://127.0.0.1:8005/ui
.venv/bin/python dev/seed_demo.py         # DEMO case waiting for approvals (prints tenant/owner test links)
.venv/bin/python dev/seed_demo.py --complete --out docs/example-final-lease-DEMO.pdf
bash tests/run_tenancy_db_tests.sh        # 31 database + full-journey tests on a throwaway Postgres
bash dev/local_stack.sh stop
```
Needs `brew install postgresql@17 poppler` and `.venv/bin/pip install -r requirements-dev.txt`.
See `docs/ONBOARDING_CHECKLIST.md` for requirement-by-requirement status.

### Earlier onboarding records

The earlier single-form onboarding (`supabase_onboarding.sql`) still works via
`/onboarding` and is listed under "Earlier onboarding records" in the tab.

### Tenant ID and property linking

Each onboarding keeps the existing property ID chosen at start (it can never be
changed). Completing onboarding creates one tenant with a unique Tenant ID
(`TEN-000001`, ...) and one tenancy linking that tenant to the property. Past
tenancies are kept as history; only one tenancy per property can be active.
The onboarding details and completed view show Property, Property ID/ref and
Tenant ID. Retrying a completion never creates a second tenant, and tenants
are never merged by name.

Setup (Supabase SQL Editor, in this order; both are safe to re-run and do not
delete data):

1. Put `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in `.env`.
2. Run `supabase_onboarding.sql` (if not already run).
3. Run `supabase_tenancy.sql`. Existing completed onboardings get a tenant and
   tenancy backfilled.

Tests against a throwaway local Postgres (needs `brew install postgresql@17`;
your Supabase data is never touched): `bash tests/run_tenancy_db_tests.sh`.

## WhatsApp

Customers chat on WhatsApp; the reply comes from exactly the same code as the
web chat (`src/services/chat.py`): search, viewings, owner listings, lead score.
Each phone number is its own saved conversation (`session_id` = `wa:+91...`),
and the customer's WhatsApp number and name are filled in automatically, so the
AI never asks for a phone number.

### Test without Meta (dry run - default)

Open the **WhatsApp test** tab, pick a customer (or type any number) and chat.
Messages go through the real webhook parsing and reply code; replies are shown
on the page instead of being sent. Results appear in Leads / Viewings / New
listings as usual.

### Go live

1. Put the server online with HTTPS (Render / Railway). Meta can't reach
   `127.0.0.1`.
2. In https://developers.facebook.com/apps create an app, add **WhatsApp**,
   and from **WhatsApp -> API Setup** copy the *Phone number ID* and a
   *permanent (system user) access token*. From **Settings -> Basic** copy the
   *App secret*.
3. Fill in `.env` on the server (never paste tokens into chats or code):
   `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`,
   a `WHATSAPP_VERIFY_TOKEN` you make up, and `WHATSAPP_DRY_RUN=false`.
   Restart.
4. In **WhatsApp -> Configuration -> Webhook**: callback URL
   `https://YOUR-SERVER/webhook/whatsapp`, verify token = your
   `WHATSAPP_VERIFY_TOKEN`. Click **Verify and save**, then subscribe to the
   **messages** field.
5. Send a WhatsApp message to your business number.

Notes:
- Replies are free-form text, which WhatsApp only allows within 24 hours of
  the customer's last message. Messages you start later (reminders,
  follow-ups) need Meta-approved templates.
- Duplicate deliveries and per-customer ordering are tracked in memory, which
  is fine for one server instance. Use a shared store before running several.
- Photos/voice notes get a "please type" reply for now.

## AI Lead Score

Every AI reply scores the lead on nine parts (0-100 each, with a reason),
combined by weight in `src/services/lead_scoring.py`:

| Part | Weight | From |
|---|---|---|
| Intent | 20 | intent + action signals (visit, apply, negotiate...) |
| Engagement | 10 | messages, questions, detail |
| Property fit | 15 | selected home or best search match vs their needs |
| Buying/moving readiness | 15 | move-in urgency, viewing requested, wants to apply |
| Sentiment | 5 | positive / neutral / mixed / negative |
| Response behaviour | 10 | how fast they reply to the bot |
| Qualification completeness | 10 | area, size, budget, rent/buy, move date known |
| Financial/requirement fit | 10 | budget vs similar listings' prices |
| Human verification | 5 | team marks Genuine / Not genuine |

Tiers: **hot** 75+, **warm** 50-74, **nurture** 25-49, **unqualified** below 25.
"Just browsing" caps at nurture; "not interested" or "not genuine" makes it
unqualified. Owners are scored on listing completeness and price vs market.
Run `supabase_lead_scoring.sql` to save the breakdown and enable the
Genuine / Not genuine buttons in the Leads panel. Change `WEIGHTS` or `TIERS`
to tune it, then run the exam.

## Fact-checking replies

Before a reply is sent, `src/services/fact_check.py` compares the AI's words
with the listing data it was given (selected home, or listings already shown):
rupee amounts (must be a real rent/deposit/price or the customer's own
budget), pets, parking, furnishing, amenities and room count. Hedged
sentences ("needs to be checked") are never flagged.

If something is wrong, the AI is asked once to rewrite it with the exact
mistakes listed. If the rewrite is still wrong, a safe "let me check with
the team" reply is sent instead. The insights panel shows ✅ passed,
🛠️ corrected (with the first draft) or 🚫 blocked.
Tests: `.venv/bin/python -m unittest tests.test_fact_check`.

## Lead outcomes and score accuracy

Open a lead in the Leads tab and mark what happened: Rented, Bought, Listed,
Lost or No response (with an optional note). The score, tier and 9-part
breakdown at that moment are saved with it. **Score accuracy** at the top of
the Leads tab shows the win rate per tier and which score parts are higher
for won leads than lost ones - use it to tune `WEIGHTS` in
`lead_scoring.py`. Needs `supabase_outcomes.sql` (it includes the lead
scoring columns).

## Speed and cost

- **Instant replies**: "hi", "thanks", "ok 👍", "bye" are answered in code
  (`src/services/quick_replies.py`) with no AI call. "ok"/"yes" right after
  the bot asked a question still goes to the AI, because it's an answer.
  Instant replies don't change the lead score.
- **Quota memory**: when a model says its limit is reached, it's skipped for
  an hour (daily limits) or for the provider's retry delay (per-minute
  limits), instead of being tried and failing on every message.
- **Stats tab**: reply time, instant-reply share, tokens per reply, models
  used and quota skips, from a log of every message
  (`src/services/metrics.py`). Run `supabase_ai_calls.sql` in Supabase to
  keep the log across restarts; without it, stats cover the time since the
  server started.
- The exam can also check speed: `"max_ms": 2000` in a case's `expect`.

## Put it online (Render)

Customers only use WhatsApp, and Meta must reach the server over HTTPS, so
the app runs on a host such as Render. Everything except `/health` and the
WhatsApp webhook needs the **team password** (`ADMIN_PASSWORD`); without it
the pages refuse to open anywhere except on your own computer.

1. Put this folder in a **private** GitHub repository. `.gitignore` keeps
   `.env`, `.venv` and exam reports out of it - check `.env` is not uploaded.
2. On https://render.com: **New -> Blueprint**, pick the repository.
   `render.yaml` sets up the web service.
3. Fill in the secret values Render asks for: `GEMINI_API_KEY`,
   `OPENROUTER_API_KEY` (used by the maintenance photo classifier even when
   `AI_PROVIDER=gemini`), `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
   `ADMIN_PASSWORD` (long and random), `PUBLIC_BASE_URL` (your
   onrender.com URL, used in onboarding WhatsApp links), and later the
   `WHATSAPP_*` values.
4. Deploy, then open `https://YOUR-APP.onrender.com/ui` and log in with
   username `team` and your `ADMIN_PASSWORD`.
5. Follow **WhatsApp -> Go live** above with
   `https://YOUR-APP.onrender.com/webhook/whatsapp`.

Notes:
- `render.yaml` uses a paid always-on plan: free services sleep when idle,
  so the first WhatsApp message after a quiet spell would wait for the
  server to wake up. Check Render's current pricing.
- Railway and similar hosts work too: `Procfile` has the start command.
- The server's Python packages are pinned in `requirement.txt`.

## AI test exam

`evals/cases.json` holds ~50 example chats, each with rules for what a
correct answer must (and must not) contain. `evals/run_evals.py` sends them
all to the running server and prints a report card. Run it after every
change to the prompt or code, before customers see it.

```bash
.venv/bin/python evals/run_evals.py                   # everything
.venv/bin/python evals/run_evals.py --only viewings   # one category
.venv/bin/python evals/run_evals.py --case pets-adyar-no
.venv/bin/python evals/run_evals.py --repeat 3        # catch answers that only sometimes fail
```

- Nothing is saved to Supabase during the exam.
- It paces itself for free Gemini (15 requests/minute), so a full run takes
  a few minutes. Cases that hit the AI quota are listed as "couldn't run",
  not failed. Free `gemini-3.5-flash` only allows ~20 requests a day, so the
  app mostly falls back to `gemini-3.5-flash-lite` on the free plan.
- Each run saves a full report in `evals/results/`.
- To add a question: copy a case in `cases.json`, change the messages and
  the `expect` rules. Checks available: `role`, `intent_in`,
  `lead_status_in`, `score_min`, `score_max`, `breakdown_has`,
  `breakdown_has_any`, `breakdown_not_has`, `reply_contains_any`,
  `reply_contains_all`, `reply_not_contains`, `requirements`,
  `property_details`, `matches_include`, `matches_exclude`, `first_match`,
  `no_matches`, `has_matches`, `no_search`, `viewing`
  (dates as `{tomorrow}` / `{saturday}`), `viewing_missing_includes`,
  `viewing_problem`, `no_viewing`.

## Endpoints

- `GET/POST /webhook/whatsapp` - Meta webhook (verification + incoming messages)
- `POST /whatsapp/simulate` - fake WhatsApp message (dry run only)
- `GET /whatsapp/status`, `GET /whatsapp/outbox` - dry-run status and replies

- `GET /properties` - listings shown on the chat page

- `GET /ui` - the app: **Chat** tab and **Leads** tab (`/ui#leads`)
- `GET /dashboard` - redirects to `/ui#leads`
- `GET /health` - health check
- `POST /analyze` - `{"message": "...", "conversation_history": [], "property_context": {}, "session_id": "...", "listing_id": "...", "listing_title": "...", "persona": "tenant"}`
- `GET /conversations/resume?session_id=...&listing_id=...` - load a saved thread
- `DELETE /conversations/resume?session_id=...&listing_id=...` - delete a saved thread
- `GET /leads` - all saved conversations, most recently active first
- `GET /leads/{conversation_id}` - one lead's full message thread

- `POST /analyze/photo` - multipart form version of `POST /analyze`, for a message with a photo attached: `message`, `conversation_history` (JSON array as a string), `property_context` (JSON object as a string), `session_id`, `listing_id`, `listing_title`, `persona`, `image` (file)
- `GET /maintenance/tickets?status=...` - saved maintenance tickets, newest first (`status` one of `needs_review`, `open`, `in_progress`, `resolved`, `dismissed`)
- `PATCH /maintenance/tickets/{ticket_id}` - `{"ticket_status": "...", "notes": "..."}`

- `GET/POST /onboarding`, `GET/PUT /onboarding/{id}` - earlier single-form onboarding records
- `GET/POST /onboarding/cases`, plus the guided-journey endpoints under it - the 7-step onboarding console flow
- `GET /p/{token}` - the tenant/owner's personal onboarding page (no team login)

## Owner leads (provider CSV → outreach → AI chat → listing)

**Owner leads** tab: upload the provider's CSV, check the preview (new / duplicate /
skipped, Do Not Call, WhatsApp consent), then import. Columns are matched by name
(owner_name/name, phone/mobile, email, area/locality, price/rent, bhk/bedrooms,
consent_to_contact, do_not_call, ...). Phones are cleaned to +91 or +1.

Staff call or email owners and record the result (Contacted, Interested, Call back,
Not interested, Do not contact). **WhatsApp intro** is only allowed when consent is
"whatsapp" and the owner is not Do Not Call; record consent after a call with
**Record consent**. The intro is saved into the owner's WhatsApp conversation, so
their reply continues in the AI chat. Replying **STOP** opts the owner out.
**Create listing** (for Interested owners) adds a pending listing to New listings.

Setup: run `supabase_owner_leads.sql` in Supabase. Demo file:
`demo_owner_leads_chennai.csv` (fake DEMO owners). Messaging owners who have not
written to you in 24 hours needs an approved template (`WHATSAPP_TEMPLATE_OWNER_INTRO`).

## MLS listings and deal analysis

**MLS import**: `POST /mls/import` takes an MLS CSV export (List Number is the key,
so re-importing a newer export updates prices and statuses instead of duplicating).
Setup: run `supabase_mls_listings.sql`. MLS data is licensed: it stays behind the
team password and is never served publicly. Search with `GET /mls`, summary at
`GET /mls/stats`.

**Deals tab**: browse those homes, enter the expected monthly rent, and see cash
flow, cap rate, cash-on-cash return, DSCR and the total cash needed, with every
assumption shown and editable (Assumptions tab, saved for the whole team).
Shortlist a deal and download a one-page investor PDF (`GET /deals/{id}/summary.pdf`).
Setup: run `supabase_deals.sql`.

Nothing is invented: price, taxes and HOA come from the MLS row, rent is typed by
staff (a guide is offered only when we have at least 3 comparable rentals of our
own), and the PDF says it is a projection, not advice or a promise of returns.

## Investors

**Investors tab**: people who want to BUY property to rent out or resell. The AI
collects their budget, financing, goal, areas and timeline during the normal chat
(role "investor" in `src/prompts/system_prompt.py`) and `src/services/investors.py`
saves and scores them (budget, financing readiness, timeline, experience, clarity,
contact -> hot / warm / nurture / unqualified). Staff can add or correct anything.

**Find homes that fit**: from their cash, the app works out the highest price they
can buy (down payment + closing costs), runs the deal maths over the MLS listings,
and ranks the best matches. Rent is *assumed* as a percent of price until staff
type the real expected rent, and every screen says so. **Send on WhatsApp** saves
the deal and sends the one-page PDF (test mode until WhatsApp is live; outside the
24-hour window an approved template is required).

The AI must never promise returns, invent rents or give mortgage/tax/legal advice;
those rules are in the prompt. Setup: run `supabase_investors.sql`.

## A tenant buying the home they rent ("Own this house")

`src/services/home_purchase.py`. Setup: run `supabase_home_buy_requests.sql` (after
`supabase_rental_applications.sql`).

1. A tenant renting an investor's home presses **🏠 Own this house** on their Homes tab.
   The owner sees the request on their Tenants tab (badge + a note in Messages).
2. The owner **declines** with a message: the tenant sees "Offer declined" with it,
   and the button stays disabled for that tenancy. Or the owner **accepts**: the tenant
   gets an investor profile on the new-investor journey (at "Offer preparation") and
   the two agree the price with the usual purchase offers / counter-offers.
3. Once either side accepts the other's offer, the **buyer check** is already done (they
   were screened when they became a tenant). The **property check** is the same inspection
   flow as an investor purchase: the sale gets a purchase-offer record (`PO-…`), and the
   team books the inspector, uploads the report and records findings/repairs in the
   **Inspections** tab (needs `supabase_acquisition_workflows.sql`). Then, in Applications →
   "Tenant buying", they mark the property check passed (only once a report is uploaded)
   or failed - a failed check cancels the purchase and the tenant keeps renting.
4. Property check passed → bought: the tenancy ends, the home leaves the seller's portfolio
   and joins the buyer's as "owned", the listing moves to the buyer (unlisted), and
   their account becomes an Existing Property Investor.
5. The new owner's Portfolio tab asks: **live there** (stays unlisted) or **rent it
   out** (may we list it? would you like homes to rent recommended?).

## Admin login (hidden)

One staff admin account that sees everything in `/ui` (Leads, Owner leads,
Investing, Alerts, New listings, Onboarding, Stats, ...).

1. Run `supabase_admin.sql` in Supabase (after `supabase_accounts.sql`).
2. Open `/admin/login` and sign in with `akileshkumar123@gmail.com` / `12345678`.

The page isn't linked from anywhere, sends `noindex`, and the public `/login`
page refuses the admin account. Customer accounts (tenant / investors) now get
`403` from the staff-only APIs (`/leads`, `/owner-leads`, `/reports/outcomes`,
`/alerts`, `/listings/pending`) even if they call them directly.
To change the admin password, generate a new hash with
`python -c "from src.services.accounts import hash_password; print(hash_password('NEW-PASSWORD'))"`
and update `password_hash` / `password_salt` for that row.

## 3D property viewer (login / register)

The dark brand panel on `/login` and `/register` shows a small 3D model house
(`src/static/property-scene.js` - one file containing Three.js r149, the
viewer and its styles, so the page doesn't rely on a CDN; each page just
loads it with one `<script>` line). It builds itself
on load, its windows light up, then it turns slowly; drag on an empty part of
the panel to spin and tilt it. It places itself in the largest gap above or
below the panel's text so it never sits behind the copy, pauses when the tab
is hidden, is hidden on screens under 960px wide, and stays still for visitors
who have "reduce motion" turned on. Without WebGL the panel simply keeps its
plain gradient.

## Customer reminders (lease, maintenance, purchase offers)

Tenants and investors get a **Notifications** tab with an unread badge and a pop-up for new reminders (`src/services/lease_notifications.py`, API `/me/account-notifications`):

- Lease: every 30 days from the start, daily from 15 days before the end, then a "lease date exceeded" notice. Sent to the tenant and the owner who listed the home.
- Maintenance: a daily reminder for each open ticket, to the tenant and the owner.
- Purchase offers: 3 days / 2 days / tomorrow, then 12 / 6 / 3 / 1 hours before the offer expires (and before closing, when a closing time is set on the offer).

Run `supabase_lease_notifications.sql` once in Supabase. Details: `NOTIFICATION_IMPLEMENTATION.md`. This is separate from the owners' "someone asked about your house" pop-ups below, which use `/me/notifications`.

## Owner notifications ("someone asked about your house")

When a **tenant** or a **New Property Investor** asks about a home an investor
account listed, that owner gets a pop-up on `/ui` with the asker's name,
account type, email, phone, what we know about them from their Portfolio
(income, move-in, pets, budget, financing ...) and their question. A bell next
to the account box keeps the list. It triggers on:

- a Chat or WhatsApp message about a specific home (the listing open in the
  chat, "the second one" from the homes just shown, the home's title, or a
  viewing request for it),
- "Enquire about this" on a listing card,
- a tenant's message to the owner on the Messages tab.

Setup: run `supabase_owner_notifications.sql` once in the Supabase SQL Editor.
Code: `src/services/owner_notifications.py`, `src/static/notifications.js`.

## Forgot password (customer accounts)

Login -> **Forgot password?** -> `/forgot-password` -> emailed link `/reset-password?token=...` -> new password -> back to `/login`.
This belongs to the customer account system and is independent of the staff Team login: it never creates a session,
never uses `ADMIN_PASSWORD`, and does not log anyone in after the reset.

Setup: run `supabase_password_reset.sql` (after `supabase_accounts.sql`), set `APP_BASE_URL` (or `PUBLIC_BASE_URL`) and the
`EMAIL_SMTP_*` settings, and set `EMAIL_DRY_RUN=false`. In test mode the email is kept in the staff-only email outbox instead of
being sent, and that outbox shows the reset link - don't leave a real deployment in test mode.

- Only a SHA-256 of each token is stored; tokens expire (`PASSWORD_RESET_TTL_MINUTES`, default 60) and work once.
- Asking for a link gives the same answer whether or not the email has an account; one email per account per minute; the newest link wins.
- The staff admin account (`/admin/login`) is not resettable by email.
- `PASSWORD_RESET_REVOKE_SESSIONS=true` also signs the account out of all devices after a reset (default: sessions untouched).
- Hosting-proxy access logs (e.g. Render's) can still record the `?token=` URL; the app's own access log redacts it.
- Tests: `tests/test_password_reset.py` (the migration tests run with `TENANCY_TEST_PG`, see `tests/run_tenancy_db_tests.sh`).
