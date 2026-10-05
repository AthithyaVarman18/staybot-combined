"""Hot alert for the staff member (src/services/staff_alerts.py): fires once
when a chat turns Hot / goes over 90, emails + WhatsApps the staff contacts,
never the customer, and never breaks the chat."""

import os
import unittest
from unittest import mock

from src.services import customers, email_sender, staff_alerts, whatsapp

LEAD = {"intent_score": 82, "lead_status": "hot", "components": {
    "urgency": {"score": 90, "weight": 2, "reason": "Wants to move in this month"},
    "budget": {"score": 80, "weight": 1, "reason": "Budget fits Garner rents"}}}
RESULT = {"role": "tenant", "summary": "Wants a 2 bed in Garner now.",
          "requirements": {"bedrooms": 2, "location": "Garner", "rent_or_buy": "rent", "budget": 1800}}
CONTACT = {"wa:+19195550199": {"name": "Maria Lopez", "phone": "+19195550199", "email": None}}


class StaffAlerts(unittest.TestCase):

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"STAFF_ALERT_EMAIL": "staff@example.com, owner@example.com",
                                                "STAFF_ALERT_WHATSAPP": "+1 (919) 555-0101",
                                                "WHATSAPP_TEMPLATE_HOT_LEAD": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.emails, self.texts, self.templates = [], [], []
        for p in (mock.patch.object(email_sender, "send", lambda to, subject, text, html, **k:
                                    self.emails.append((to, subject, text)) or {"ok": True, "test_mode": True}),
                  mock.patch.object(whatsapp, "send_text", lambda to, body: self.texts.append((to, body)) or []),
                  mock.patch.object(whatsapp, "send_template", lambda to, name, lang="en_US", comps=None:
                                    self.templates.append((to, name, comps)) or []),
                  mock.patch.object(customers, "contacts", lambda s, c: CONTACT)):
            p.start()
            self.addCleanup(p.stop)

    def test_turning_hot_tells_the_staff_with_the_details(self):
        out = staff_alerts.notify("wa:+19195550199", "c1", RESULT, LEAD, 60, "warm")
        self.assertEqual(out["trigger"], "hot")
        self.assertEqual([e[0] for e in self.emails], ["staff@example.com", "owner@example.com"])
        self.assertEqual(self.texts[0][0], "19195550101")           # digits only for WhatsApp
        text = self.texts[0][1]
        for bit in ("Maria Lopez", "+19195550199", "82", "2 bed", "Garner", "$1,800/month",
                    "Wants to move in this month", "/ui#customers"):
            self.assertIn(bit, text)

    def test_only_once_not_on_every_message(self):
        self.assertIsNone(staff_alerts.notify("wa:+19195550199", "c1", RESULT, LEAD, 82, "hot"))
        self.assertIsNone(staff_alerts.notify("wa:+19195550199", "c1", RESULT,
                                              {**LEAD, "intent_score": 50, "lead_status": "warm"}, 40, "nurture"))
        self.assertEqual((self.emails, self.texts), ([], []))

    def test_over_90_without_hot(self):
        out = staff_alerts.notify("wa:+19195550199", "c1", RESULT,
                                  {**LEAD, "intent_score": 92, "lead_status": "warm"}, 85, "warm")
        self.assertEqual(out["trigger"], "score_gt_90")
        self.assertIn("over 90", self.emails[0][1])

    def test_template_used_when_set(self):
        with mock.patch.dict(os.environ, {"WHATSAPP_TEMPLATE_HOT_LEAD": "hot_lead_alert"}):
            staff_alerts.notify("wa:+19195550199", "c1", RESULT, LEAD, 60, "warm")
        self.assertEqual(self.texts, [])
        to, name, comps = self.templates[0]
        self.assertEqual((to, name), ("19195550101", "hot_lead_alert"))
        self.assertEqual(comps[0]["parameters"][0]["text"], "Maria Lopez")

    def test_failures_never_break_the_chat(self):
        with mock.patch.object(whatsapp, "send_text", side_effect=RuntimeError("token expired")), \
             mock.patch.object(customers, "contacts", side_effect=RuntimeError("db down")):
            out = staff_alerts.notify("wa:+19195550199", "c1", RESULT, LEAD, 60, "warm")
        self.assertFalse(out["whatsapp"][0]["ok"])
        self.assertIn("19195550199", self.emails[0][2])             # phone still known from the WhatsApp id

    def test_nobody_configured_just_logs(self):
        with mock.patch.dict(os.environ, {"STAFF_ALERT_EMAIL": "", "STAFF_ALERT_WHATSAPP": ""}):
            out = staff_alerts.notify("wa:+19195550199", "c1", RESULT, LEAD, 60, "warm")
        self.assertEqual((out["email"], out["whatsapp"]), ([], []))
