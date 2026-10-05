"""
Create a clearly labelled DEMO onboarding case through the real API.

    .venv/bin/python dev/seed_demo.py                         # local stack on :8005, stops at "review"
    .venv/bin/python dev/seed_demo.py --complete              # also approve, finalize and send (test mode)
    .venv/bin/python dev/seed_demo.py --base http://127.0.0.1:8004 --password '...'

Demo people use reserved fictional 555-01xx numbers and the case is marked
is_test, so WhatsApp is always simulated for it. The PDF uses the DEMO template.
"""

import argparse
import sys
import uuid

import requests

PDF = b"%PDF-1.4\n% DEMO document for Staybot onboarding testing - not a real ID\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8005")
    parser.add_argument("--password", default="")
    parser.add_argument("--complete", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    s = requests.Session()
    s.headers["X-Staybot-Staff"] = "DEMO Staff Sam"
    if args.password:
        s.auth = ("team", args.password)

    def call(method, path, **kw):
        r = s.request(method, path if path.startswith("http") else args.base + path, timeout=60, **kw)
        if not r.ok:
            sys.exit(f"{method} {path} failed: {r.status_code} {r.text[:300]}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r

    tag = uuid.uuid4().hex[:4]
    staff = call("POST", "/onboarding/cases/staff", json={"name": "DEMO Staff Sam"})
    tenant = call("POST", "/onboarding/cases/people", json={"full_name": "DEMO Tenant Taylor Test", "whatsapp": "+17045550101", "is_test": True})
    owner = call("POST", "/onboarding/cases/people", json={"full_name": "DEMO Owner Olivia Example", "whatsapp": "+17045550102", "is_test": True})
    case = call("POST", "/onboarding/cases", json={
        "new_property": {"id": f"demo-nc-test-lane-{tag}", "title": "DEMO 101 Test Lane (not a real listing)", "city": "Charlotte"},
        "unit": "2B", "property_address": "101 Test Lane, Unit 2B, Charlotte, NC 28202 (DEMO)",
        "tenant_id": tenant["id"], "owner_id": owner["id"], "staff_id": staff["id"], "is_test": True})
    cid = case["id"]
    for step, body in ((1, {}), (2, {"consent": {"whatsapp_opt_in": True, "source": "customer_messaged_first"}}),
                       (3, {"consent": {"whatsapp_opt_in": True, "source": "confirmed_by_phone_or_in_person"}})):
        case = call("PUT", f"/onboarding/cases/{cid}/steps/{step}", json={"version": case["version"], "advance": True, **body})
    case = call("PUT", f"/onboarding/cases/{cid}/steps/5", json={"version": case["version"], "advance": True,
        "terms": {"lease_start": "2026-10-01", "lease_end": "2027-09-30", "move_in_date": "2026-10-01", "rent": "1850.00",
                  "deposit": "1850.00", "currency": "USD", "payment_schedule": "monthly", "rent_due_day": 1,
                  "charges": [{"label": "Trash and recycling service", "amount": "25.00", "frequency": "monthly"}],
                  "occupants": ["DEMO Tenant Taylor Test"], "pets": "No pets", "utilities": "Tenant pays electricity and water",
                  "additional_terms": ["No smoking inside the home."]},
        "preferences": {"lease_setup": "digital_copy_whatsapp", "account_setup": "undecided"}})
    links = {party: call("POST", f"/onboarding/cases/{cid}/invite", json={"party": party})["link"] for party in ("tenant", "owner")}
    call("POST", f"/onboarding/cases/{cid}/agreement")
    print(f"DEMO case {case['reference']} created: {args.base}/ui#onboarding")
    for party, link in links.items():
        print(f"  {party} test link: {link}")
    if not args.complete:
        return
    for party, key in (("tenant", "photo_id"), ("tenant", "proof_of_income"), ("owner", "ownership_authority")):
        call("POST", f"{links[party]}/api/documents/{key}", data=PDF, headers={"Content-Type": "application/pdf", "X-File-Name": f"DEMO-{key}.pdf"})
    view = call("GET", f"/onboarding/cases/{cid}")
    for d in view["documents"]:
        call("POST", f"/onboarding/cases/{cid}/documents/{d['latest']['id']}/review", json={"decision": "accepted"})
    for party in ("tenant", "owner"):
        state = call("GET", f"{links[party]}/api/state")
        call("POST", f"{links[party]}/api/decision", json={"version_id": state["agreement"]["version_id"], "decision": "approved", "reviewed_agreement": True})
    view = call("GET", f"/onboarding/cases/{cid}")
    call("POST", f"/onboarding/cases/{cid}/staff-review", json={"agreement_version": view["agreement"]["latest_version"]["version"]})
    call("POST", f"/onboarding/cases/{cid}/finalize")
    call("POST", f"/onboarding/cases/{cid}/send-final")
    if args.out:
        with open(args.out, "wb") as f:
            f.write(call("GET", f"/onboarding/cases/{cid}/final.pdf").content)
        print(f"  final PDF saved to {args.out}")
    print("  finalized and sent in test mode (simulated, nothing delivered)")


if __name__ == "__main__":
    main()
