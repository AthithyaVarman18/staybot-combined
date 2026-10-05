"""
Forgot password / email password reset (src/services/password_reset.py).

Three layers:
  * HTTP tests through the real FastAPI app with the Supabase helpers replaced by
    a small in-memory database (FakeDB) and the outgoing email captured.
  * Authentication-isolation tests: the reset feature must never touch the Team
    login, create a session, or leave /forgot-password behind a Team prompt.
  * A real-Postgres test of supabase_password_reset.sql (skipped unless
    TENANCY_TEST_PG is set, like the other database tests - see
    tests/run_tenancy_db_tests.sh).
"""

import ast
import base64
import contextlib
import copy
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests
from fastapi.testclient import TestClient

from src import main
from src.services import accounts, db, password_reset, team_auth

ROOT = Path(__file__).resolve().parents[1]
REAL_EMAIL_SEND = password_reset.email_sender.send      # the genuine function, before any test patches it
BASE = "https://app.staybot.test"
OLD_PASSWORD = "OldPassw0rd!"
NEW_PASSWORD = "BrandNewPass1!"
TOKEN_RE = re.compile(r"/reset-password\?token=([A-Za-z0-9_\-]+)")


class FakeDB:
    """In-memory stand-in for the PostgREST helpers in src.services.db, covering
    the tables and filters the password reset flow uses (eq / neq / is.null,
    order, limit). Mirrors db._patch's return shape (the row, or [] if nothing
    matched) because the single-use claim depends on it."""

    def __init__(self):
        self.tables = {"accounts": [], "password_reset_tokens": [], "account_sessions": []}

    @staticmethod
    def _match(row, params):
        for key, raw in (params or {}).items():
            if key in ("select", "order", "limit", "offset"):
                continue
            op, _, value = raw.partition(".")
            actual = row.get(key)
            if op == "eq":
                ok = actual is not None and str(actual) == value
            elif op == "neq":
                ok = actual is None or str(actual) != value
            elif op == "is" and value == "null":
                ok = actual is None
            else:
                raise AssertionError(f"FakeDB: unsupported filter {key}={raw}")
            if not ok:
                return False
        return True

    def _get(self, path, params=None):
        rows = [copy.deepcopy(r) for r in self.tables[path] if self._match(r, params)]
        order = (params or {}).get("order")
        if order:
            column, _, direction = order.partition(".")
            rows.sort(key=lambda r: str(r.get(column)), reverse=(direction == "desc"))
        if (params or {}).get("limit"):
            rows = rows[: int(params["limit"])]
        return rows

    def _post(self, path, body, params=None):
        row = copy.deepcopy(body)
        row.setdefault("id", str(uuid.uuid4()))
        if path == "password_reset_tokens":
            row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            row.setdefault("used_at", None)
            if any(r["token_hash"] == row["token_hash"] for r in self.tables[path]):
                raise requests.HTTPError("duplicate key value violates unique constraint")
        self.tables[path].append(row)
        return copy.deepcopy(row)

    def _patch(self, path, body, params):
        updated = []
        for row in self.tables[path]:
            if self._match(row, params):
                row.update(copy.deepcopy(body))
                updated.append(copy.deepcopy(row))
        return updated[0] if updated else []

    def _delete(self, path, params):
        self.tables[path] = [r for r in self.tables[path] if not self._match(r, params)]

    # test conveniences
    def account(self, email):
        return next(r for r in self.tables["accounts"] if r["email"] == email)

    def token_rows(self):
        return self.tables["password_reset_tokens"]


class ResetTestCase(unittest.TestCase):
    """Common setup: fake database, captured email, fixed config, fast hashing."""

    def setUp(self):
        self.fake = FakeDB()
        self.sent = []

        def fake_send(to, subject, text, html, unsubscribe_url=None):
            self.sent.append({"to": to, "subject": subject, "text": text, "html": html})
            return {"ok": True, "test_mode": False, "error": None}

        patches = [
            mock.patch.object(db, "ENABLED", True),
            mock.patch.object(db, "_get", self.fake._get),
            mock.patch.object(db, "_post", self.fake._post),
            mock.patch.object(db, "_patch", self.fake._patch),
            mock.patch.object(db, "_delete", self.fake._delete),
            mock.patch.object(password_reset.email_sender, "send", fake_send),
            mock.patch.object(accounts, "PBKDF2_ITERATIONS", 1000),   # same algorithm, just quicker tests
            mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("APP_BASE_URL", "PASSWORD_RESET_TTL_MINUTES", "PASSWORD_RESET_REVOKE_SESSIONS"):
            os.environ.pop(name, None)

        self.client = TestClient(main.app, follow_redirects=False)
        self.jane = self.make_account("jane@example.com", "tenant")

    # ---- helpers ----

    def make_account(self, email, role="tenant", password=OLD_PASSWORD):
        password_hash, salt = accounts.hash_password(password)
        return self.fake._post("accounts", {
            "name": "Jane Doe", "email": email, "password_hash": password_hash, "password_salt": salt,
            "role": role, "session_id": f"acct-{uuid.uuid4().hex[:12]}", "investor_id": None,
            "education_seen": True, "screening_seen": True,
        })

    def forgot(self, email="jane@example.com", **kwargs):
        return self.client.post("/auth/forgot-password", json={"email": email}, **kwargs)

    def request_link(self, email="jane@example.com"):
        """Ask for a reset email through the real endpoint; return the raw token from the email."""
        before = len(self.sent)
        self.assertEqual(self.forgot(email).status_code, 200)
        self.assertEqual(len(self.sent), before + 1, "expected a reset email to be sent")
        match = TOKEN_RE.search(self.sent[-1]["text"])
        self.assertIsNotNone(match, self.sent[-1]["text"])
        return match.group(1)

    def reset(self, token, password=NEW_PASSWORD, confirm=None):
        return self.client.post("/auth/reset-password", json={
            "token": token, "password": password, "confirm_password": password if confirm is None else confirm})

    def login(self, email, password):
        return self.client.post("/auth/login", json={"email": email, "password": password})

    def password_ok(self, email, password):
        row = self.fake.account(email)
        return accounts.verify_password(password, row["password_hash"], row["password_salt"])

    @contextlib.contextmanager
    def minutes_later(self, minutes):
        later = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        with mock.patch.object(password_reset, "now", lambda: later):
            yield


# ---------------------------------------------------------------------
# Forgot Password
# ---------------------------------------------------------------------

class ForgotPassword(ResetTestCase):

    def test_registered_email_gets_generic_message_and_one_email(self):
        res = self.forgot("jane@example.com")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"message": password_reset.GENERIC_MESSAGE})
        self.assertEqual(password_reset.GENERIC_MESSAGE,
                         "If an account exists for this email address, a password reset link has been sent.")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["to"], "jane@example.com")

    def test_unregistered_email_gets_exactly_the_same_response(self):
        known = self.forgot("jane@example.com")
        unknown = self.forgot("nobody@example.com")
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.json(), unknown.json())
        self.assertEqual(known.content, unknown.content)
        self.assertEqual(sorted(known.headers.keys()), sorted(unknown.headers.keys()))

    def test_unregistered_email_sends_nothing_and_stores_nothing(self):
        self.forgot("nobody@example.com")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.fake.token_rows(), [])

    def test_response_never_says_whether_the_email_is_registered(self):
        for email in ("jane@example.com", "nobody@example.com"):
            text = self.forgot(email).text.lower()
            for phrase in ("not registered", "no account", "does not exist", "is registered", "not found"):
                self.assertNotIn(phrase, text)

    def test_email_is_matched_case_insensitively(self):
        self.forgot("  JANE@Example.COM ")
        self.assertEqual(len(self.sent), 1)

    def test_malformed_email_is_rejected_without_sending(self):
        res = self.forgot("not-an-email")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.sent, [])

    def test_creates_hashed_expiring_unused_token(self):
        token = self.request_link()
        rows = self.fake.token_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["account_id"], self.jane["id"])
        self.assertEqual(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertIsNone(row["used_at"])
        lifetime = datetime.fromisoformat(row["expires_at"]) - datetime.now(timezone.utc)
        self.assertAlmostEqual(lifetime.total_seconds(), 3600, delta=30)

    def test_raw_token_is_long_and_random(self):
        first = self.request_link()
        with self.minutes_later(2):
            second = self.request_link()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 40)          # 256 bits, url-safe base64

    def test_database_never_contains_the_raw_token_or_any_password(self):
        token = self.request_link()
        self.reset(token)
        dump = json.dumps(self.fake.tables)
        self.assertNotIn(token, dump)
        self.assertNotIn(NEW_PASSWORD, dump)
        self.assertNotIn(OLD_PASSWORD, dump)

    def test_email_content(self):
        token = self.request_link()
        message = self.sent[0]
        self.assertEqual(message["subject"], "Staybot Password Reset")
        for part in ("Hello,", "We received a request to reset your Staybot password.",
                     "you can safely ignore this email", f"{BASE}/reset-password?token={token}", "expire in 1 hour"):
            self.assertIn(part, message["text"])
        self.assertIn("Reset Password", message["html"])
        self.assertIn(f'href="{BASE}/reset-password?token={token}"', message["html"])

    def test_link_uses_configured_base_url_not_the_request_host(self):
        # A forged Host header must not be able to aim the victim's link at another server.
        res = self.forgot(headers={"Host": "evil.example"})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("evil.example", self.sent[0]["text"])
        self.assertIn(f"{BASE}/reset-password?token=", self.sent[0]["text"])

    def test_app_base_url_takes_precedence_over_public_base_url(self):
        with mock.patch.dict(os.environ, {"APP_BASE_URL": "https://override.staybot.test/"}):
            self.request_link()
        self.assertIn("https://override.staybot.test/reset-password?token=", self.sent[0]["text"])

    def test_configurable_expiry(self):
        with mock.patch.dict(os.environ, {"PASSWORD_RESET_TTL_MINUTES": "15"}):
            self.request_link()
        lifetime = datetime.fromisoformat(self.fake.token_rows()[0]["expires_at"]) - datetime.now(timezone.utc)
        self.assertAlmostEqual(lifetime.total_seconds(), 900, delta=30)
        self.assertIn("expire in 15 minutes", self.sent[0]["text"])

    def test_nonsense_expiry_setting_falls_back_to_safe_values(self):
        for value, expected in (("abc", 60), ("0", 5), ("-4", 5), ("999999", 1440)):
            with mock.patch.dict(os.environ, {"PASSWORD_RESET_TTL_MINUTES": value}):
                self.assertEqual(password_reset.ttl_minutes(), expected, value)

    def test_access_log_redacts_the_token_in_reset_page_urls(self):
        import logging
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                   ("1.2.3.4:5", "GET", "/reset-password?token=SECRETvalue_123-x&a=b", "1.1", 200), None)
        self.assertTrue(password_reset.RedactTokens().filter(record))
        line = record.getMessage()
        self.assertNotIn("SECRETvalue", line)
        self.assertIn("/reset-password?token=[redacted]&a=b", line)
        filters = logging.getLogger("uvicorn.access").filters
        self.assertTrue(any(isinstance(f, password_reset.RedactTokens) for f in filters))

    def test_new_request_retires_older_links(self):
        first = self.request_link()
        with self.minutes_later(2):
            second = self.request_link()
        self.assertEqual(self.reset(first).status_code, 410)       # replaced by the newer link
        self.assertEqual(self.reset(second).status_code, 200)

    def test_requests_are_throttled_per_account(self):
        self.request_link()
        self.assertEqual(self.forgot().status_code, 200)            # same answer...
        self.assertEqual(len(self.sent), 1)                         # ...but no second email
        self.assertEqual(len(self.fake.token_rows()), 1)
        with self.minutes_later(2):
            self.request_link()                                     # allowed again after the cooldown
        self.assertEqual(len(self.sent), 2)

    def test_throttle_does_not_leak_through_the_response(self):
        first = self.forgot()
        second = self.forgot()
        self.assertEqual(first.content, second.content)

    def test_admin_account_is_not_resettable_and_looks_like_unknown_email(self):
        self.make_account("boss@example.com", role="admin")
        res = self.forgot("boss@example.com")
        self.assertEqual(res.json(), {"message": password_reset.GENERIC_MESSAGE})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.fake.token_rows(), [])

    def test_email_failure_still_gives_the_same_response(self):
        with mock.patch.object(password_reset.email_sender, "send",
                               return_value={"ok": False, "test_mode": False, "error": "smtp down"}):
            res = self.forgot()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"message": password_reset.GENERIC_MESSAGE})

    def test_database_error_still_gives_the_same_response(self):
        with mock.patch.object(db, "_get", side_effect=requests.ConnectionError("db down")):
            res = self.forgot()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"message": password_reset.GENERIC_MESSAGE})

    def test_works_with_the_real_email_service_in_test_mode(self):
        # No fake sender here: goes through src/services/email_sender.py, which in
        # test mode keeps the message in its outbox instead of sending it.
        email_sender = password_reset.email_sender
        before = len(email_sender.OUTBOX)
        with mock.patch.object(email_sender, "send", REAL_EMAIL_SEND), mock.patch.object(email_sender, "DRY_RUN", True):
            self.assertEqual(self.forgot().status_code, 200)
        self.assertEqual(len(email_sender.OUTBOX), before + 1)
        queued = email_sender.OUTBOX[-1]
        self.assertEqual(queued["to"], "jane@example.com")
        self.assertEqual(queued["subject"], "Staybot Password Reset")
        self.assertRegex(queued["text"], TOKEN_RE)

    def test_raw_token_and_passwords_are_never_printed_or_logged(self):
        import logging
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(old_level)))

        seen = []

        def failing_send(to, subject, text, html, unsubscribe_url=None):
            seen.append(text)
            return {"ok": False, "test_mode": False, "error": "smtp down"}

        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            token = self.request_link()                                   # success path
            self.reset(token, confirm="Mismatch1!")                       # rejected
            self.assertEqual(self.reset(token).status_code, 200)          # success path
            self.reset(token, password="Replayed-Pass1!")                 # replay -> refused
            with mock.patch.object(password_reset.email_sender, "send", failing_send), self.minutes_later(5):
                self.forgot()                                             # email failure path
            with mock.patch.object(db, "_get", side_effect=requests.ConnectionError("db down")):
                self.forgot()                                             # database failure path
        failed_token = TOKEN_RE.search(seen[0]).group(1)
        output = captured.getvalue()
        for secret in (token, failed_token, NEW_PASSWORD, "Replayed-Pass1!", "Mismatch1!", OLD_PASSWORD):
            self.assertNotIn(secret, output)


# ---------------------------------------------------------------------
# Reset Password
# ---------------------------------------------------------------------

class ResetPassword(ResetTestCase):

    def test_valid_token_sets_new_password(self):
        token = self.request_link()
        res = self.reset(token)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["status"], "ok")
        self.assertEqual(res.json()["message"], "Your password has been updated successfully.")
        self.assertTrue(self.password_ok("jane@example.com", NEW_PASSWORD))
        self.assertFalse(self.password_ok("jane@example.com", OLD_PASSWORD))

    def test_new_password_is_hashed_with_the_existing_scheme_and_a_fresh_salt(self):
        old = self.fake.account("jane@example.com")
        old_hash, old_salt = old["password_hash"], old["password_salt"]
        self.reset(self.request_link())
        row = self.fake.account("jane@example.com")
        self.assertNotEqual(row["password_salt"], old_salt)
        self.assertNotEqual(row["password_hash"], old_hash)
        self.assertEqual(row["password_hash"], accounts.hash_password(NEW_PASSWORD, row["password_salt"])[0])
        self.assertNotIn(NEW_PASSWORD, json.dumps(row))

    def test_reset_marks_the_token_used(self):
        self.reset(self.request_link())
        self.assertIsNotNone(self.fake.token_rows()[0]["used_at"])

    def test_token_cannot_be_reused(self):
        token = self.request_link()
        self.assertEqual(self.reset(token).status_code, 200)
        again = self.reset(token, password="AnotherPassw0rd!")
        self.assertEqual(again.status_code, 410)
        self.assertEqual(again.json()["detail"], password_reset.INVALID_LINK)
        self.assertTrue(self.password_ok("jane@example.com", NEW_PASSWORD))          # unchanged by the replay
        self.assertFalse(self.password_ok("jane@example.com", "AnotherPassw0rd!"))

    def test_invalid_token_is_refused_and_changes_nothing(self):
        self.request_link()
        before = copy.deepcopy(self.fake.tables)
        for bad in ("not-a-real-token", "x" * 43, "x" * 5000, "   "):
            res = self.reset(bad)
            self.assertEqual(res.status_code, 410, bad[:20])
            self.assertEqual(res.json()["detail"], password_reset.INVALID_LINK)
        self.assertEqual(self.fake.tables, before)

    def test_token_hash_value_is_not_accepted_as_a_token(self):
        # Someone who steals the database (hashes only) still can't reset a password.
        self.request_link()
        stored_hash = self.fake.token_rows()[0]["token_hash"]
        self.assertEqual(self.reset(stored_hash).status_code, 410)

    def test_expired_token_is_refused(self):
        token = self.request_link()
        with self.minutes_later(61):
            res = self.reset(token)
        self.assertEqual(res.status_code, 410)
        self.assertTrue(self.password_ok("jane@example.com", OLD_PASSWORD))
        self.assertIsNone(self.fake.token_rows()[0]["used_at"])

    def test_token_still_works_just_before_expiry(self):
        token = self.request_link()
        with self.minutes_later(59):
            self.assertEqual(self.reset(token).status_code, 200)

    def test_password_mismatch_is_refused_and_does_not_burn_the_link(self):
        token = self.request_link()
        res = self.reset(token, password=NEW_PASSWORD, confirm="Different1!")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["detail"], "Passwords do not match.")
        self.assertTrue(self.password_ok("jane@example.com", OLD_PASSWORD))
        self.assertEqual(self.reset(token).status_code, 200)                       # same link still works

    def test_weak_password_is_refused_and_does_not_burn_the_link(self):
        token = self.request_link()
        short = self.reset(token, password="short")
        self.assertEqual(short.status_code, 400)
        self.assertIn("at least 8", short.json()["detail"])
        too_long = self.reset(token, password="a" * 201)
        self.assertEqual(too_long.status_code, 400)
        self.assertEqual(self.reset(token).status_code, 200)

    def test_missing_fields_are_rejected_by_validation(self):
        self.assertEqual(self.client.post("/auth/reset-password", json={"token": "abc"}).status_code, 422)
        self.assertEqual(self.client.post("/auth/reset-password", json={}).status_code, 422)

    def test_reset_does_not_log_anyone_in(self):
        token = self.request_link()
        with mock.patch.object(accounts, "issue_session", side_effect=AssertionError("must not create a session")), \
             mock.patch.object(accounts, "set_session_cookie", side_effect=AssertionError("must not set a cookie")):
            res = self.reset(token)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("set-cookie", res.headers)
        self.assertNotIn(accounts.ISSUED_HEADER.lower(), res.headers)
        self.assertEqual(self.fake.tables["account_sessions"], [])
        self.assertNotIn("redirect_url", res.json())

    def test_reset_preserves_role_identity_and_other_fields(self):
        before = self.fake.account("jane@example.com")
        self.reset(self.request_link())
        after = self.fake.account("jane@example.com")
        for field in ("id", "name", "email", "role", "session_id", "investor_id", "education_seen", "screening_seen"):
            self.assertEqual(before[field], after[field], field)

    def test_reset_only_affects_the_requesting_account(self):
        other = self.make_account("other@example.com", "existing_investor")
        other_before = self.fake.account("other@example.com")
        self.reset(self.request_link())
        self.assertEqual(self.fake.account("other@example.com"), other_before)
        self.assertTrue(self.password_ok("other@example.com", OLD_PASSWORD))
        self.assertEqual(other["role"], "existing_investor")

    def test_reset_retires_other_outstanding_links_for_the_account(self):
        token = self.request_link()
        spare = password_reset.new_token()
        self.fake._post("password_reset_tokens", {
            "account_id": self.jane["id"], "token_hash": password_reset.hash_token(spare),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        self.assertEqual(self.reset(token).status_code, 200)
        self.assertEqual(self.reset(spare, password="YetAnother1!").status_code, 410)
        self.assertTrue(self.password_ok("jane@example.com", NEW_PASSWORD))

    def test_token_of_the_admin_account_never_works(self):
        admin = self.make_account("boss@example.com", role="admin")
        token = password_reset.new_token()
        self.fake._post("password_reset_tokens", {
            "account_id": admin["id"], "token_hash": password_reset.hash_token(token),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        self.assertEqual(self.reset(token).status_code, 410)
        self.assertTrue(self.password_ok("boss@example.com", OLD_PASSWORD))

    def test_losing_the_race_for_the_token_changes_nothing(self):
        # Two requests with the same link: the atomic claim matches no row for the loser.
        token = self.request_link()
        real_patch = self.fake._patch

        def lose_the_claim(path, body, params):
            if path == "password_reset_tokens" and body.get("used_at") and params.get("used_at") == "is.null" and "id" in params:
                return []                                   # someone else already spent it
            return real_patch(path, body, params)

        with mock.patch.object(db, "_patch", lose_the_claim):
            res = self.reset(token)
        self.assertEqual(res.status_code, 410)
        self.assertTrue(self.password_ok("jane@example.com", OLD_PASSWORD))

    def test_two_concurrent_resets_with_one_link_only_one_succeeds(self):
        import threading
        token = self.request_link()
        lock = threading.Lock()
        real_patch = self.fake._patch
        barrier = threading.Barrier(2)
        results = []

        def synchronised_get(path, params=None):
            rows = self.fake._get(path, params)
            if path == "password_reset_tokens" and params and "token_hash" in params:
                barrier.wait(timeout=5)                     # both requests have now seen an unused token
            return rows

        def serialised_patch(path, body, params):
            with lock:                                      # the database applies each UPDATE atomically
                return real_patch(path, body, params)

        def attempt(password):
            client = TestClient(main.app, follow_redirects=False)
            results.append(client.post("/auth/reset-password", json={
                "token": token, "password": password, "confirm_password": password}).status_code)

        with mock.patch.object(db, "_get", synchronised_get), mock.patch.object(db, "_patch", serialised_patch):
            threads = [threading.Thread(target=attempt, args=(p,)) for p in ("FirstAttempt1!", "SecondAttempt1!")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        self.assertEqual(sorted(results), [200, 410])
        winners = [p for p in ("FirstAttempt1!", "SecondAttempt1!") if self.password_ok("jane@example.com", p)]
        self.assertEqual(len(winners), 1)

    def test_failed_password_update_gives_the_link_back(self):
        token = self.request_link()
        real_patch = self.fake._patch

        def fail_account_update(path, body, params):
            if path == "accounts":
                raise requests.ConnectionError("db hiccup")
            return real_patch(path, body, params)

        with mock.patch.object(db, "_patch", fail_account_update):
            res = self.reset(token)
        self.assertEqual(res.status_code, 500)
        self.assertTrue(self.password_ok("jane@example.com", OLD_PASSWORD))
        self.assertEqual(self.reset(token).status_code, 200)                       # not burned by the failure

    def test_missing_reset_table_says_which_sql_to_run(self):
        err = requests.HTTPError("relation does not exist")
        err.response = mock.Mock(text='{"code":"PGRST205","message":"Could not find the table"}')
        with mock.patch.object(db, "_get", side_effect=err):
            res = self.reset("sometoken")
        self.assertEqual(res.status_code, 503)
        self.assertIn("supabase_password_reset.sql", res.json()["detail"])

    def test_existing_sessions_are_left_alone_by_default(self):
        self.fake._post("account_sessions", {"account_id": self.jane["id"], "token_hash": "abc",
                                             "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
        self.reset(self.request_link())
        self.assertEqual(len(self.fake.tables["account_sessions"]), 1)

    def test_sessions_can_optionally_be_revoked(self):
        self.fake._post("account_sessions", {"account_id": self.jane["id"], "token_hash": "abc",
                                             "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
        self.fake._post("account_sessions", {"account_id": "someone-else", "token_hash": "def",
                                             "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
        with mock.patch.dict(os.environ, {"PASSWORD_RESET_REVOKE_SESSIONS": "true"}):
            self.reset(self.request_link())
        self.assertEqual([s["account_id"] for s in self.fake.tables["account_sessions"]], ["someone-else"])


# ---------------------------------------------------------------------
# End to end through the real login endpoint
# ---------------------------------------------------------------------

class FullFlow(ResetTestCase):

    def test_forgot_email_link_reset_then_login(self):
        # 1-2: Forgot Password page, submit the email
        self.assertEqual(self.client.get("/forgot-password").status_code, 200)
        token = self.request_link()
        # 3: the emailed link opens the Reset Password page
        link_path = "/reset-password?token=" + token
        self.assertIn(BASE + link_path, self.sent[0]["text"])
        page = self.client.get(link_path)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Reset Password", page.text)
        # 4: choose a new password
        self.assertEqual(self.reset(token).status_code, 200)
        # nobody is logged in yet
        self.assertEqual(self.fake.tables["account_sessions"], [])
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        # 5: back to login - the new password works, the old one doesn't
        self.assertEqual(self.login("jane@example.com", OLD_PASSWORD).status_code, 401)
        ok = self.login("jane@example.com", NEW_PASSWORD)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["account"]["role"], "tenant")                     # role preserved
        self.assertEqual(len(self.fake.tables["account_sessions"]), 1)               # created by LOGIN, not by the reset

    def test_investor_roles_keep_their_role_through_a_reset(self):
        for role in ("new_investor", "existing_investor"):
            email = f"{role}@example.com"
            self.make_account(email, role)
            self.reset(self.request_link(email), password=NEW_PASSWORD)
            res = self.login(email, NEW_PASSWORD)
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(res.json()["account"]["role"], role)

    def test_existing_login_flow_is_unchanged(self):
        self.assertEqual(self.login("jane@example.com", OLD_PASSWORD).status_code, 200)
        self.assertEqual(self.login("jane@example.com", "wrong-password").status_code, 401)
        self.assertEqual(self.login("ghost@example.com", OLD_PASSWORD).status_code, 401)

    def test_customer_login_page_still_links_to_forgot_password(self):
        html = (ROOT / "src" / "static" / "login.html").read_text(encoding="utf-8")
        self.assertRegex(html, r'<a class="forgot" href="/forgot-password">')
        self.assertNotIn("team/login", html.lower())


# ---------------------------------------------------------------------
# Team / Staff authentication stays completely separate
# ---------------------------------------------------------------------

class TeamAuthIsolation(unittest.TestCase):

    TEAM_PW = "team-secret-pw"

    def setUp(self):
        self.client = TestClient(main.app, follow_redirects=False)
        patcher = mock.patch.object(team_auth, "ADMIN_PASSWORD", self.TEAM_PW)   # as on a real server
        patcher.start()
        self.addCleanup(patcher.stop)

    def basic(self, password):
        return {"Authorization": "Basic " + base64.b64encode(f"{team_auth.ADMIN_USERNAME}:{password}".encode()).decode()}

    def assert_team_prompt(self, res, path):
        self.assertEqual(res.status_code, 401, path)
        self.assertIn("Staybot team", res.headers.get("www-authenticate", ""), path)

    def test_forgot_password_page_opens_without_team_authentication(self):
        res = self.client.get("/forgot-password")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("www-authenticate", res.headers)
        self.assertIn("Forgot Password?", res.text)
        self.assertIn("Send Reset Link", res.text)

    def test_forgot_password_does_not_redirect_to_team_login(self):
        res = self.client.get("/forgot-password")
        self.assertNotIn(res.status_code, (301, 302, 303, 307, 308))
        self.assertNotIn("location", res.headers)
        visible = re.sub(r"<!--.*?-->", "", res.text, flags=re.S)             # developer comments aren't shown to anyone
        self.assertNotRegex(visible, r"(?i)team\s*(password|login)")

    def test_reset_password_page_opens_without_team_authentication(self):
        res = self.client.get("/reset-password?token=anything")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("www-authenticate", res.headers)
        self.assertIn("Confirm New Password", res.text)
        self.assertEqual(self.client.get("/reset-password").status_code, 200)       # even with no token (page explains)

    def test_pages_set_no_cookie_and_no_session(self):
        for path in ("/forgot-password", "/reset-password?token=abc"):
            res = self.client.get(path)
            self.assertNotIn("set-cookie", res.headers, path)
            self.assertNotIn(accounts.ISSUED_HEADER.lower(), res.headers, path)

    def test_pages_are_not_cacheable_or_leaky(self):
        for path in ("/forgot-password", "/reset-password?token=abc"):
            res = self.client.get(path)
            self.assertEqual(res.headers["cache-control"], "no-store", path)
            self.assertEqual(res.headers["referrer-policy"], "no-referrer", path)
            self.assertIn("noindex", res.headers["x-robots-tag"], path)

    def test_reset_api_endpoints_are_public_and_not_a_team_prompt(self):
        with mock.patch.object(db, "ENABLED", False):         # 503 "not configured" is fine; 401 Team prompt is not
            for path, body in (("/auth/forgot-password", {"email": "jane@example.com"}),
                               ("/auth/reset-password", {"token": "t", "password": "Password123", "confirm_password": "Password123"})):
                res = self.client.post(path, json=body)
                self.assertNotEqual(res.status_code, 401, path)
                self.assertNotIn("www-authenticate", res.headers, path)

    def test_team_pages_and_apis_are_still_protected(self):
        for path in ("/ui", "/leads", "/investors", "/deals", "/onboarding/cases", "/customers", "/openapi.json"):
            self.assert_team_prompt(self.client.get(path), path)

    def test_team_login_still_works_with_the_team_password(self):
        self.assertEqual(self.client.get("/openapi.json", headers=self.basic(self.TEAM_PW)).status_code, 200)
        self.assert_team_prompt(self.client.get("/openapi.json", headers=self.basic("wrong")), "wrong password")

    def test_team_credentials_are_not_a_way_to_reset_anything(self):
        # The reset endpoints neither require nor honour the Team password.
        with_creds = self.client.post("/auth/reset-password", headers=self.basic(self.TEAM_PW),
                                      json={"token": "nope", "password": "Password123", "confirm_password": "Password123"})
        without = self.client.post("/auth/reset-password",
                                   json={"token": "nope", "password": "Password123", "confirm_password": "Password123"})
        self.assertEqual(with_creds.status_code, without.status_code)

    def test_only_the_two_reset_pages_were_made_public(self):
        public = team_auth.PUBLIC_PATHS
        self.assertIn("/forgot-password", public)
        self.assertIn("/reset-password", public)
        for protected in ("/ui", "/team", "/leads", "/investors", "/deals", "/metrics", "/customers", "/admin"):
            self.assertNotIn(protected, public)
        self.assertNotIn("/reset", public)                                           # exact paths, no wildcard prefix
        self.assertEqual([p for p in team_auth.PUBLIC_PREFIXES if "password" in p or "reset" in p], [])

    def test_password_reset_module_never_touches_team_or_session_machinery(self):
        tree = ast.parse(inspect.getsource(password_reset))
        referenced = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                referenced.update(alias.name.split(".")[-1] for alias in node.names)
                if isinstance(node, ast.ImportFrom) and node.module:
                    referenced.update(node.module.split("."))
        for forbidden in ("team_auth", "ADMIN_PASSWORD", "ADMIN_USERNAME", "issue_session", "set_session_cookie",
                          "set_cookie", "current_account", "credentials_ok", "TeamAuthMiddleware"):
            self.assertNotIn(forbidden, referenced, f"password_reset.py must not use {forbidden}")


# ---------------------------------------------------------------------
# Frontend pages
# ---------------------------------------------------------------------

STATIC = ROOT / "src" / "static"


class FrontendPages(unittest.TestCase):

    def read(self, name):
        return (STATIC / name).read_text(encoding="utf-8")

    def test_forgot_password_page_matches_the_spec(self):
        html = self.read("forgot_password.html")
        for text in ("Forgot Password?", "Enter the email address associated with your Staybot account.",
                     "Send Reset Link", "Back to Login", "/auth/forgot-password",
                     "If an account exists for this email address, a password reset link has been sent."):
            self.assertIn(text, html)
        self.assertIn('href="/login"', html)
        self.assertNotIn("tab-session.js", html)            # no session machinery on this page

    def test_reset_password_page_matches_the_spec(self):
        html = self.read("reset_password.html")
        for text in ("Reset Password", "New Password", "Confirm New Password", "Password Reset Successful",
                     "Your password has been updated successfully.", "Back to Login", "/auth/reset-password",
                     'name="referrer" content="no-referrer"', "URLSearchParams(location.search).get(\"token\")"):
            self.assertIn(text, html)
        self.assertIn('href="/login"', html)
        self.assertNotIn("tab-session.js", html)
        self.assertNotIn("localStorage", html)              # the token/password are never persisted
        self.assertNotIn("sessionStorage", html)

    def test_pages_never_navigate_anywhere_but_login_or_forgot(self):
        for name in ("forgot_password.html", "reset_password.html"):
            html = self.read(name)
            self.assertNotRegex(html, r"(?i)/team|/admin|location\.href\s*=")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_inline_javascript_is_syntactically_valid(self):
        for name in ("forgot_password.html", "reset_password.html"):
            scripts = re.findall(r"<script>(.*?)</script>", self.read(name), re.S)
            self.assertTrue(scripts, name)
            for i, code in enumerate(scripts):
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
                    handle.write(code)
                try:
                    proc = subprocess.run(["node", "--check", handle.name], capture_output=True, text=True)
                finally:
                    os.unlink(handle.name)
                self.assertEqual(proc.returncode, 0, f"{name} script {i}: {proc.stderr}")


# ---------------------------------------------------------------------
# supabase_password_reset.sql against a real Postgres (opt-in)
# ---------------------------------------------------------------------

DSN = os.getenv("TENANCY_TEST_PG")
PSQL = os.getenv("TENANCY_TEST_PSQL") or shutil.which("psql") or "/opt/homebrew/opt/postgresql@17/bin/psql"
# Everything supabase_accounts.sql depends on, in order.
MIGRATIONS = ["supabase_schema.sql", "supabase_properties.sql", "supabase_owner_listings.sql", "supabase_onboarding.sql",
              "supabase_tenancy.sql", "supabase_lease_onboarding.sql", "supabase_mls_listings.sql", "supabase_deals.sql",
              "supabase_investors.sql", "supabase_investor_journeys.sql", "supabase_accounts.sql", "supabase_admin.sql"]


def run_sql(dbname, sql, expect_error=False):
    proc = subprocess.run(
        [PSQL, f"{DSN} dbname={dbname}", "-v", "ON_ERROR_STOP=1", "-X", "-q", "-A", "-t", "-F", "|"],
        input=sql, capture_output=True, text=True,
        env={**os.environ, "LC_ALL": "C", "PGOPTIONS": "-c client_min_messages=warning"})
    if expect_error:
        if proc.returncode == 0:
            raise AssertionError(f"expected an error from: {sql}\noutput: {proc.stdout}")
        return proc.stderr
    if proc.returncode != 0:
        raise AssertionError(f"SQL failed: {proc.stderr}\n{sql}")
    return [line.split("|") for line in proc.stdout.strip().splitlines() if line.strip()]


@unittest.skipUnless(DSN, "set TENANCY_TEST_PG to run database tests")
class PasswordResetMigration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = "pwreset_test_" + uuid.uuid4().hex[:8]
        run_sql("postgres", f"create database {cls.db}")
        run_sql(cls.db, """
            do $$ begin
              if not exists (select from pg_roles where rolname = 'anon') then create role anon nologin; end if;
              if not exists (select from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
              if not exists (select from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
            end $$;
            grant usage on schema public to anon, authenticated, service_role;
            alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
        """)
        for name in MIGRATIONS:
            run_sql(cls.db, (ROOT / name).read_text())
        migration = (ROOT / "supabase_password_reset.sql").read_text()
        run_sql(cls.db, migration)
        run_sql(cls.db, migration)          # running it again must be safe
        cls.account = run_sql(cls.db, """
            insert into accounts (name, email, password_hash, password_salt, role, session_id)
            values ('TEST Jane', 'test-jane@example.com', 'h', 's', 'tenant', 'acct-test-1') returning id""")[0][0]

    @classmethod
    def tearDownClass(cls):
        run_sql("postgres", f"drop database if exists {cls.db} with (force)")

    def token(self, token_hash, account=None, expires="now() + interval '1 hour'"):
        return run_sql(self.db, f"""insert into password_reset_tokens (account_id, token_hash, expires_at)
                                    values ('{account or self.account}', '{token_hash}', {expires}) returning id""")[0][0]

    def test_columns_and_defaults(self):
        cols = {r[0]: r[1:] for r in run_sql(self.db, """
            select column_name, data_type, is_nullable from information_schema.columns
            where table_name = 'password_reset_tokens'""")}
        self.assertEqual(set(cols), {"id", "account_id", "token_hash", "expires_at", "used_at", "created_at"})
        self.assertEqual(cols["used_at"][1], "YES")
        self.assertEqual(cols["token_hash"][1], "NO")
        self.assertNotIn("password", " ".join(cols))                    # no password column of any kind
        row = run_sql(self.db, f"select used_at is null, created_at is not null from password_reset_tokens where id = '{self.token('h-defaults')}'")[0]
        self.assertEqual(row, ["t", "t"])

    def test_token_hash_is_unique(self):
        self.token("h-unique")
        err = run_sql(self.db, f"insert into password_reset_tokens (account_id, token_hash, expires_at) "
                               f"values ('{self.account}', 'h-unique', now())", expect_error=True)
        self.assertIn("duplicate key", err)

    def test_token_must_belong_to_a_real_account(self):
        err = run_sql(self.db, f"insert into password_reset_tokens (account_id, token_hash, expires_at) "
                               f"values ('{uuid.uuid4()}', 'h-orphan', now())", expect_error=True)
        self.assertIn("foreign key", err)

    def test_deleting_an_account_deletes_its_tokens(self):
        acct = run_sql(self.db, """insert into accounts (name, email, password_hash, password_salt, role, session_id)
            values ('TEST Gone', 'test-gone@example.com', 'h', 's', 'tenant', 'acct-test-gone') returning id""")[0][0]
        self.token("h-cascade", account=acct)
        run_sql(self.db, f"delete from accounts where id = '{acct}'")
        self.assertEqual(run_sql(self.db, "select count(*) from password_reset_tokens where token_hash = 'h-cascade'")[0][0], "0")

    def test_single_use_claim_is_atomic(self):
        tid = self.token("h-claim")
        claim = f"update password_reset_tokens set used_at = now() where id = '{tid}' and used_at is null returning id"
        self.assertEqual(len(run_sql(self.db, claim)), 1)               # first spend wins
        self.assertEqual(run_sql(self.db, claim), [])                   # replay matches nothing

    def test_public_roles_cannot_touch_the_table_but_the_backend_can(self):
        privileges = run_sql(self.db, """
            select has_table_privilege('anon', 'password_reset_tokens', 'select'),
                   has_table_privilege('authenticated', 'password_reset_tokens', 'select'),
                   has_table_privilege('anon', 'password_reset_tokens', 'insert'),
                   has_table_privilege('authenticated', 'password_reset_tokens', 'insert'),
                   has_table_privilege('service_role', 'password_reset_tokens', 'select'),
                   has_table_privilege('service_role', 'password_reset_tokens', 'insert')""")[0]
        self.assertEqual(privileges, ["f", "f", "f", "f", "t", "t"])
        self.assertEqual(run_sql(self.db, "select relrowsecurity from pg_class where relname = 'password_reset_tokens'")[0][0], "t")
        self.assertIn("permission denied", run_sql(self.db, "set role anon; select * from password_reset_tokens", expect_error=True))


if __name__ == "__main__":
    unittest.main()
