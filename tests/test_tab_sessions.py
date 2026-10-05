import hashlib, unittest
from unittest import mock
from datetime import timedelta
from fastapi.testclient import TestClient
from src import main
from src.services import accounts

ACCTS = {
 "t": {"id":"t","name":"akilesh","email":"akilesh@gmail.com","role":"tenant","session_id":"acct-t","investor_id":None,"password_hash":"h","password_salt":"s","screening_seen":True},
 "i": {"id":"i","name":"inv","email":"inv@x.com","role":"existing_investor","session_id":"acct-i","investor_id":"x","password_hash":"h","password_salt":"s"},
}
SESS = {}
def fake_get(table, params):
    if table=="accounts":
        if "email" in params:
            e=params["email"][3:]; return [a for a in ACCTS.values() if a["email"]==e]
        return [ACCTS[params["id"][3:]]]
    if table=="account_sessions":
        h=params["token_hash"][3:]; return [SESS[h]] if h in SESS else []
def fake_post(table, row):
    if table=="account_sessions": SESS[row["token_hash"]]=row
    return row
def fake_delete(table, params): SESS.pop(params["token_hash"][3:], None)

class TwoWindows(unittest.TestCase):
    def test(self):
        with mock.patch.object(accounts.db,"ENABLED",True), mock.patch.object(accounts.db,"_get",fake_get), \
             mock.patch.object(accounts.db,"_post",fake_post), mock.patch.object(accounts.db,"_delete",fake_delete), \
             mock.patch.object(accounts,"verify_password",return_value=True):
            c = TestClient(main.app)  # one browser = one cookie jar
            r = c.post("/auth/login", json={"email":"akilesh@gmail.com","password":"p","role":"tenant"})
            tab_a = r.headers["X-Staybot-Session-Issued"]
            r = c.post("/auth/login", json={"email":"inv@x.com","password":"p","role":"existing_investor"})
            tab_b = r.headers["X-Staybot-Session-Issued"]
            # old behaviour: cookie only -> investor
            self.assertEqual(c.get("/auth/me").json()["role"], "existing_investor")
            # tab A with its own header stays tenant
            self.assertEqual(c.get("/auth/me", headers={"X-Staybot-Session":tab_a}).json()["role"], "tenant")
            self.assertEqual(c.get("/auth/me", headers={"X-Staybot-Session":tab_b}).json()["role"], "existing_investor")
            # focus tab A -> cookie follows it
            c.post("/auth/activate", headers={"X-Staybot-Session":tab_a})
            self.assertEqual(c.get("/auth/me").json()["role"], "tenant")
            # logging out tab B doesn't log out tab A or clear A's cookie
            r = c.post("/auth/logout", headers={"X-Staybot-Session":tab_b})
            self.assertEqual(r.headers["X-Staybot-Session-Cleared"], "1")
            self.assertEqual(c.get("/auth/me", headers={"X-Staybot-Session":tab_a}).status_code, 200)
            self.assertEqual(c.get("/auth/me").json()["role"], "tenant")
            # tab B's dead token must NOT fall back to the cookie (tenant)
            self.assertEqual(c.get("/auth/me", headers={"X-Staybot-Session":tab_b}).status_code, 401)
            self.assertEqual(c.post("/auth/activate", headers={"X-Staybot-Session":tab_b}).status_code, 401)
            self.assertEqual(c.get("/assets/tab-session.js").status_code, 200)
