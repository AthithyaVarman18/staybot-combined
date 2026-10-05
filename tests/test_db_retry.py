"""A dropped Supabase connection ("Connection aborted / RemoteDisconnected")
is retried instead of failing the user's action - without ever saving a row
twice (src/services/db.py: _send / _post)."""

import unittest
from unittest import mock

import requests

from src.services import db

DROPPED = requests.exceptions.ConnectionError("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")


def response(status, payload):
    r = mock.Mock(status_code=status)
    r.json.return_value = payload
    r.raise_for_status.side_effect = None if status < 400 else requests.exceptions.HTTPError(f"{status}")
    return r


class Retry(unittest.TestCase):

    def setUp(self):
        sleep = mock.patch("time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_get_is_retried_after_a_dropped_connection(self):
        with mock.patch.object(db.requests, "request", side_effect=[DROPPED, response(200, [{"id": "h1"}])]) as req:
            self.assertEqual(db._get("properties", {"id": "eq.h1"}), [{"id": "h1"}])
        self.assertEqual(req.call_count, 2)

    def test_gives_up_after_the_last_retry(self):
        with mock.patch.object(db.requests, "request", side_effect=[DROPPED, DROPPED, DROPPED]) as req:
            with self.assertRaises(requests.exceptions.ConnectionError):
                db._get("properties")
        self.assertEqual(req.call_count, 3)

    def test_listing_insert_with_its_own_id_is_retried(self):
        row = {"id": "owner-garner-abc123", "title": "3 bed house"}
        with mock.patch.object(db.requests, "request", side_effect=[DROPPED, response(201, [row])]):
            self.assertEqual(db._post("properties", row), row)

    def test_retry_that_hits_the_row_already_saved_returns_it(self):
        # The first attempt reached the database before the connection dropped.
        row = {"id": "owner-garner-abc123", "title": "3 bed house"}
        with mock.patch.object(db.requests, "request",
                               side_effect=[DROPPED, response(409, {"message": "duplicate key"}), response(200, [row])]):
            self.assertEqual(db._post("properties", row), row)

    def test_a_real_duplicate_id_without_a_retry_still_fails(self):
        row = {"id": "owner-garner-abc123"}
        with mock.patch.object(db.requests, "request", side_effect=[response(409, {"message": "duplicate key"})]):
            with self.assertRaises(requests.exceptions.HTTPError):
                db._post("properties", row)

    def test_insert_without_its_own_id_is_not_retried(self):
        # The database makes the id - a retry could save the row twice.
        with mock.patch.object(db.requests, "request", side_effect=[DROPPED, response(201, [{"id": 1}])]) as req:
            with self.assertRaises(requests.exceptions.ConnectionError):
                db._post("rental_messages", {"body": "hi"})
        self.assertEqual(req.call_count, 1)


if __name__ == "__main__":
    unittest.main()
