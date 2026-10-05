import unittest

from src.services.fact_check import check_reply


SUNLIT = {"id": "omr-3bhk", "title": "Sunlit 3BHK near OMR IT corridor", "bedrooms": 3, "rent": 35000,
          "deposit": 200000, "pets_allowed": True, "parking": True, "furnished": "semi-furnished",
          "amenities": ["gym", "swimming pool", "power backup", "24x7 security"], "listing_type": "rent"}
ADYAR = {"id": "adyar-2bhk", "title": "Cozy 2BHK by the river", "bedrooms": 2, "rent": 28000,
         "deposit": 150000, "pets_allowed": False, "parking": True, "furnished": "fully furnished",
         "amenities": ["lift", "power backup"], "listing_type": "rent"}
VELACHERY = {"id": "velachery-1bhk", "title": "Modern 1BHK, walk to metro", "bedrooms": 1, "rent": 16000,
             "parking": False, "furnished": "unfurnished", "amenities": ["near metro station"], "listing_type": "rent"}
NAVALUR = {"id": "navalur-3bhk", "title": "Family 3BHK in a gated community", "bedrooms": 3, "rent": 29000,
           "deposit": 150000, "parking": True, "furnished": "unfurnished",
           "amenities": ["gated community", "children's play area", "power backup"], "listing_type": "rent"}


def issues(reply, ctx=None, shown=None, said=None, req=None):
    return [i["type"] for i in check_reply(reply, ctx, shown, said or [], req or {})]


class CorrectRepliesPass(unittest.TestCase):

    def test_true_statements(self):
        cases = [
            ("Yes, pets are allowed in this property. When are you looking to move in?", SUNLIT),
            ("The deposit is ₹2,00,000 and the rent is ₹35,000 per month.", SUNLIT),
            ("The rent is 35k and the deposit is 2 lakh.", SUNLIT),
            ("Yes, it has a swimming pool and a gym.", SUNLIT),
            ("It is semi-furnished, with parking available.", SUNLIT),
            ("I can request Saturday at 10 AM. The team will confirm availability.", SUNLIT),
            ("Unfortunately, pets are not allowed in this flat.", ADYAR),
            ("It's fully furnished and the security deposit is ₹1,50,000.", ADYAR),
            ("The listing doesn't mention a swimming pool, so I'd need to check.", ADYAR),
            ("I need to check whether pets are allowed here.", VELACHERY),
            ("Unfortunately, there is no parking with this flat.", VELACHERY),
            ("Nice carpet area and it's unfurnished.", VELACHERY),
            ("You can call me on +919876543210 at 17:00 on 2026-10-01.", SUNLIT),
        ]
        for reply, ctx in cases:
            with self.subTest(reply=reply):
                self.assertEqual(issues(reply, ctx), [])

    def test_customer_budget_can_be_repeated(self):
        self.assertEqual(
            issues("Got it, a 3BHK in OMR under ₹40,000.", SUNLIT, said=["I need a 3BHK in OMR under 40k"]), [])
        self.assertEqual(issues("Your budget of ₹45,000 works for this.", SUNLIT, req={"budget": "45k"}), [])

    def test_general_search_named_listing(self):
        shown = [NAVALUR, SUNLIT]
        self.assertEqual(issues("The deposit for the second property (Sunlit 3BHK near OMR IT corridor) is ₹200,000.", shown=shown), [])
        self.assertEqual(issues("Whether pets are allowed at Family 3BHK in a gated community needs to be checked.", shown=shown), [])


class WrongRepliesAreCaught(unittest.TestCase):

    def test_wrong_facts(self):
        cases = [
            ("The rent is ₹30,000 per month.", SUNLIT, "money"),
            ("The deposit is just 1 lakh.", SUNLIT, "money"),
            ("Sorry, pets are not allowed here.", SUNLIT, "pets"),
            ("Yes, pets are welcome!", ADYAR, "pets"),
            ("Pets are allowed in this flat.", VELACHERY, "pets"),
            ("Parking is available for one car.", VELACHERY, "parking"),
            ("It comes fully furnished.", SUNLIT, "furnishing"),
            ("Yes, it has a clubhouse and a swimming pool.", ADYAR, "amenity"),
            ("This 2BHK is a great choice.", SUNLIT, "rooms"),
        ]
        for reply, ctx, kind in cases:
            with self.subTest(reply=reply):
                self.assertIn(kind, issues(reply, ctx))

    def test_general_search_wrong_price_for_named_listing(self):
        self.assertIn("money", issues("Sunlit 3BHK near OMR IT corridor is only ₹25,000.", shown=[NAVALUR, SUNLIT]))


if __name__ == "__main__":
    unittest.main()
