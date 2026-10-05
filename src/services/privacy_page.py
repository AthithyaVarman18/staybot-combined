"""
Public privacy policy at /privacy - Meta needs this link before the
WhatsApp number can go live, and it's linked from the login / register /
chat pages.

The business details come from .env, so the client's real name, email and
address can be put in without touching code:

    BUSINESS_NAME=Real Company Name LLC
    PRIVACY_EMAIL=info@realcompany.com
    BUSINESS_POSTAL_ADDRESS=123 Main St, Garner, NC 27529
    PRIVACY_UPDATED=October 3, 2026

Until then placeholders are shown (and /privacy says so in the server log).
"""

import html
import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PLACEHOLDERS = {
    "BUSINESS_NAME": "Staybot Realty",
    "PRIVACY_EMAIL": "privacy@example.com",
    "BUSINESS_POSTAL_ADDRESS": "Garner, NC",
    "PRIVACY_UPDATED": "October 3, 2026",
}


def details() -> dict:
    out = {k: (os.getenv(k) or "").strip() or v for k, v in PLACEHOLDERS.items()}
    out["using_placeholders"] = [k for k in ("BUSINESS_NAME", "PRIVACY_EMAIL", "BUSINESS_POSTAL_ADDRESS")
                                 if not (os.getenv(k) or "").strip()]
    return out


def render() -> str:
    d = details()
    e = {k: html.escape(str(v)) for k, v in d.items() if k != "using_placeholders"}
    name, email, address, updated = e["BUSINESS_NAME"], e["PRIVACY_EMAIL"], e["BUSINESS_POSTAL_ADDRESS"], e["PRIVACY_UPDATED"]
    mail = f'<a href="mailto:{email}">{email}</a>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy · {name}</title>
<style>
  :root {{ --bg:#F7F7F5; --card:#FFFFFF; --ink:#1F2328; --muted:#5B636E; --line:#E4E4E0; --accent:#1F6FEB; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#111315; --card:#1A1D21; --ink:#E8EAED; --muted:#A0A7B1; --line:#2B3036; --accent:#6EA8FE; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 32px 16px 64px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:28px 24px; }}
  h1 {{ font-size: 28px; line-height:1.25; margin: 0 0 4px; }}
  h2 {{ font-size: 18px; margin: 28px 0 6px; }}
  p, li {{ color: var(--ink); }}
  ul {{ padding-left: 20px; margin: 6px 0; }}
  .muted {{ color: var(--muted); font-size: 14px; }}
  a {{ color: var(--accent); }}
  .box {{ border-left: 3px solid var(--accent); padding: 4px 0 4px 14px; margin: 14px 0; }}
</style>
</head>
<body>
<main>
<div class="card">
<h1>Privacy Policy</h1>
<p class="muted">{name} · Last updated {updated}</p>

<div class="box">
<b>In short:</b> we use what you tell us only to help you rent, buy, sell or look after a home.
We never sell your information. You can ask us to delete it at any time by emailing {mail}.
</div>

<h2>Who we are</h2>
<p>{name} ("we", "us") is a real estate business based in {address}. This policy covers our website,
our chat assistant and our WhatsApp number.</p>

<h2>What we collect</h2>
<ul>
  <li><b>Contact details</b> - your name, phone / WhatsApp number and email.</li>
  <li><b>What you're looking for</b> - area, budget, bedrooms, move-in date, pets and similar details you share in the chat.</li>
  <li><b>Your messages</b> - the chat and WhatsApp messages you send us, and any photos you send (for example of a repair).</li>
  <li><b>Applications</b> - if you apply to rent or buy: details such as employment, income, current address,
      references and documents you upload.</li>
  <li><b>Property details</b> - if you list a home with us: its address, price, photos and your contact details.</li>
  <li><b>Account details</b> - if you create an account: your email and a securely stored (hashed) password.</li>
  <li><b>Basic technical data</b> - cookies needed to keep you logged in. We do not use advertising cookies.</li>
</ul>

<h2>How we use it</h2>
<ul>
  <li>To answer your questions and show you homes that fit what you want.</li>
  <li>To book viewings, process applications, leases, offers and repair requests.</li>
  <li>So a member of our team can call or message you about your enquiry.</li>
  <li>To prioritise enquiries: our system estimates how ready you are to move (for example, from your budget
      and timeline) so our team replies to urgent requests first. This score is only used inside our team and
      does not decide whether you can rent or buy a home.</li>
  <li>To keep records the law requires, and to keep our service safe.</li>
</ul>

<h2>Our AI assistant</h2>
<p>Our chat and WhatsApp replies are written by an AI assistant. Your messages are sent to our AI provider to
create the reply. A real person from our team follows up on serious enquiries. You can ask to speak to a person at any time.</p>

<h2>Who we share it with</h2>
<p><b>We do not sell or rent your personal information</b>, and we do not share your phone number or messages
with anyone for their own marketing. We share only what is needed with:</p>
<ul>
  <li><b>The owner or landlord</b> of a home you enquire about or apply for.</li>
  <li><b>Service providers</b> that run our service for us: website hosting (Render), our database and file
      storage (Supabase), WhatsApp messaging (Meta), AI providers (Google Gemini and OpenRouter), email delivery
      and our customer records system (HubSpot). They may only use your information to provide their service to us.</li>
  <li><b>Authorities</b>, if the law requires it.</li>
</ul>

<h2>WhatsApp and text messages</h2>
<p>We message you on WhatsApp only after you message us first or give us your number. Message and data rates
may apply. To stop messages, email {mail} or tell us in the chat and we will stop.</p>

<h2>How long we keep it</h2>
<p>We keep your information while we are helping you and for as long as needed for leases, sales and legal
records. When it is no longer needed, we delete it.</p>

<h2>Your choices</h2>
<p>You can ask us to <b>see</b>, <b>correct</b> or <b>delete</b> your information, or to stop contacting you.
Email {mail} and we will reply within 30 days. We will not treat you differently for asking.</p>

<h2>Keeping it safe</h2>
<p>Your information is sent over encrypted connections (HTTPS), stored with access limited to our team, and
passwords are never stored in plain text. No system is perfectly secure, but we work to protect your information.</p>

<h2>Children</h2>
<p>Our service is for adults. We do not knowingly collect information from children under 13.</p>

<h2>Changes</h2>
<p>If we change this policy, we will update the date at the top of this page.</p>

<h2>Contact us</h2>
<p>{name}<br>{address}<br>{mail}</p>
</div>
</main>
</body>
</html>"""


@router.get("/privacy", include_in_schema=False)
def privacy():
    d = details()
    if d["using_placeholders"]:
        print("Privacy page is using placeholders for: " + ", ".join(d["using_placeholders"])
              + " - set them in .env before giving the link to Meta.")
    return HTMLResponse(render(), headers={"Cache-Control": "no-cache"})
