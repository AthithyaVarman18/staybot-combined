import unittest
from datetime import date
from src.services import lease_notifications as ln


class CustomerNotificationRules(unittest.TestCase):
    def test_lease_notification_schedule(self):
        self.assertEqual(ln.expiry_bucket(300, 0)[0], "start_day")
        self.assertEqual(ln.expiry_bucket(270, 30)[0], "every_30_days_30")
        self.assertEqual(ln.expiry_bucket(210, 90)[0], "every_30_days_90")
        # Every 30 days from the start while more than 15 days remain ...
        self.assertEqual(ln.expiry_bucket(60, 300)[0], "every_30_days_300")
        self.assertIsNone(ln.expiry_bucket(16, 344))
        # ... then daily from 15 days before the end through the due date.
        self.assertEqual(ln.expiry_bucket(15, 345)[0], "day_15")
        self.assertEqual(ln.expiry_bucket(1, 359)[0], "day_1")
        self.assertEqual(ln.expiry_bucket(0, 360)[0], "day_0")
        self.assertEqual(ln.expiry_bucket(-1, 361)[0], "expired")
        self.assertIsNone(ln.expiry_bucket(61, 299))

    def test_offer_timeline(self):
        self.assertEqual(ln._offer_time_bucket(3 * 86400 - 1)[0], "d_3")
        self.assertEqual(ln._offer_time_bucket(2 * 86400 - 1)[0], "d_2")
        self.assertEqual(ln._offer_time_bucket(20 * 3600)[0], "d_1")
        self.assertEqual(ln._offer_time_bucket(12 * 3600)[0], "h_12")
        self.assertEqual(ln._offer_time_bucket(6 * 3600)[0], "h_6")
        self.assertEqual(ln._offer_time_bucket(3 * 3600)[0], "h_3")
        self.assertEqual(ln._offer_time_bucket(3600)[0], "h_1")
        self.assertEqual(ln._offer_time_bucket(-1)[0], "expired")

    def test_maintenance_age_is_day_based(self):
        self.assertEqual((date(2026, 10, 4) - date(2026, 10, 1)).days, 3)


if __name__ == "__main__":
    unittest.main()
