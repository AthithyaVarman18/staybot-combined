import unittest
from unittest.mock import patch
from src.services import viewings as v, db

class ViewingRegressionTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(db, "ENABLED", True),
            patch.object(db, "find_open_viewing", return_value=None),
            patch.object(db, "create_viewing", return_value={"id":"test", "status":"requested"}),
            patch.object(db, "update_viewing", return_value={"id":"test", "status":"requested"})]
        self.enabled, self.lookup, self.create, self.update = [p.start() for p in self.patches]
        for p in self.patches: self.addCleanup(p.stop)
    def request(self):
        return {"intent":"schedule_viewing", "response":"Your request is saved.",
            "viewing_request":{"wants_viewing":True,"date_text":"tomorrow","time":"10:00"}}
    def run_request(self, **kwargs):
        kwargs.setdefault("session_id", "test-session")
        r=self.request(); v.handle_viewing_request(r, **kwargs); return r
    def assert_failure(self, r):
        self.assertFalse(r["viewing"]["saved"])
        self.assertIn("couldn't verify", r["response"])
        self.assertNotIn("Your request is saved", r["response"])
        self.assertIsNone(r["next_question"])
    def test_create_acknowledged(self):
        r=self.run_request(property_id="test")
        self.assertTrue(r["viewing"]["saved"])
        self.assertEqual(r["viewing"]["status"],"requested")
    def test_empty_create_acknowledgements(self):
        for value in [None,{},[],{"status":"requested"}]:
            with self.subTest(value=value):
                self.create.return_value=value
                self.assert_failure(self.run_request(property_id="test"))
    def test_create_exception(self):
        self.create.side_effect=RuntimeError("simulated")
        self.assert_failure(self.run_request(property_id="test"))
    def test_database_disabled(self):
        with patch.object(db,"ENABLED",False): self.assert_failure(self.run_request(property_id="test"))
        self.create.assert_not_called()
    def test_invalid_positions_do_not_save(self):
        for value in [0,-1,1.5,True,False,"0","-1","1.5",3,None]:
            with self.subTest(value=value):
                r=self.request();r["viewing_request"]["listing_position"]=value
                v.handle_viewing_request(r,shown_listings=[{"id":"first"},{"id":"second"}])
                self.assertIn("property",r["viewing"]["missing"])
        self.create.assert_not_called()
    def test_valid_positions(self):
        for value in [1,"1",2,"2"]:
            r=self.request();r["viewing_request"]["listing_position"]=value
            v.handle_viewing_request(r,shown_listings=[{"id":"first"},{"id":"second"}])
            self.assertEqual(r["viewing"]["property_id"],"first" if int(value)==1 else "second")
    def existing(self):
        self.lookup.return_value={"id":"test","status":"confirmed","property_id":"test",
            "viewing_date":v.resolve_date("tomorrow",None).isoformat(),"viewing_time":"11:00"}
    def test_empty_update_acknowledgement(self):
        self.existing();self.update.return_value=None
        self.assert_failure(self.run_request(property_id="test",session_id="test"))
    def test_update_exception(self):
        self.existing();self.update.side_effect=RuntimeError("simulated")
        self.assert_failure(self.run_request(property_id="test",session_id="test"))
    def test_reschedule_needs_confirmation(self):
        self.existing();r=self.run_request(property_id="test",session_id="test")
        self.assertEqual(self.update.call_args.args[1]["status"],"requested")
        self.assertEqual(r["viewing"]["action"],"rescheduled")
    def test_missing_time(self):
        r=self.request();r["viewing_request"].pop("time");v.handle_viewing_request(r,property_id="test")
        self.assertIn("time",r["viewing"]["missing"]);self.create.assert_not_called()
    def test_outside_hours(self):
        r=self.request();r["viewing_request"]["time"]="23:00";v.handle_viewing_request(r,property_id="test")
        self.assertIn("problem",r["viewing"]);self.create.assert_not_called()


class AnonymousViewingTests(unittest.TestCase):
    def test_no_conversation_never_saves(self):
        with patch.object(db, "ENABLED", True), \
             patch.object(db, "create_viewing") as create, \
             patch.object(db, "find_open_viewing") as lookup:
            r = {"intent": "schedule_viewing", "response": "Saturday at 10 AM works.",
                 "viewing_request": {"wants_viewing": True, "date_text": "tomorrow", "time": "10:00"}}
            v.handle_viewing_request(r, property_id="omr-3bhk")
            create.assert_not_called()
            lookup.assert_not_called()
            self.assertFalse(r["viewing"]["saved"])
            self.assertEqual(r["response"], "Saturday at 10 AM works.")


if __name__ == "__main__":
    unittest.main()
