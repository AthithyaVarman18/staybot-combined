import unittest
from unittest.mock import patch

from src.services import requirements


class RequirementsValidationTests(unittest.TestCase):
    def test_tenant_budget_range_is_validated(self):
        with self.assertRaises(ValueError):
            requirements.RequirementPayload(budget_min=2500, budget_max=1500)

    def test_completion_does_not_count_optional_blank_as_filled(self):
        c = requirements._completion("tenant", {"city_area": "Cary", "budget_max": 2500})
        self.assertEqual(c["percentage"], 20)
        self.assertIn("Bedrooms", c["missing"])

    def test_owner_profile_has_seller_fields(self):
        c = requirements._completion("owner", {"property_address": "1 Main St", "property_type": "house"})
        self.assertGreater(c["percentage"], 0)
        self.assertIn("Selling timeline", c["missing"])


class RequirementMatchingTests(unittest.TestCase):
    ROWS = [
        {
            "id": "a", "ref": "A", "title": "Cary 3BR", "area": "Cary", "city": "Cary",
            "location": "West Cary, NC", "listing_type": "rent", "property_type": "house",
            "bedrooms": 3, "bathrooms": 2, "rent": 2000, "sale_price": None,
            "pets_allowed": True, "parking": True, "furnished": "unfurnished", "amenities": ["garage", "yard"],
        },
        {
            "id": "b", "ref": "B", "title": "Raleigh 2BR", "area": "Raleigh", "city": "Raleigh",
            "location": "Raleigh, NC", "listing_type": "rent", "property_type": "apartment",
            "bedrooms": 2, "bathrooms": 1, "rent": 1500, "sale_price": None,
            "pets_allowed": True, "parking": False, "furnished": "unfurnished", "amenities": [],
        },
        {
            "id": "c", "ref": "C", "title": "Cary 4BR Sale", "area": "Cary", "city": "Cary",
            "location": "Cary, NC", "listing_type": "sale", "property_type": "house",
            "bedrooms": 4, "bathrooms": 3, "rent": None, "sale_price": 525000,
            "pets_allowed": True, "parking": True, "furnished": "unfurnished", "amenities": ["garage"],
        },
    ]

    @patch("src.services.requirements.properties.all_properties")
    def test_tenant_matches_before_llm_using_structured_filters(self, all_properties):
        all_properties.return_value = (self.ROWS, "sample")
        result = requirements.match_properties("tenant", {
            "city_area": "Cary", "property_type": "house", "bedrooms": 3,
            "bathrooms": 2, "budget_max": 2200, "required_features": ["Parking"],
        })
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["matches"][0]["id"], "a")
        self.assertEqual(result["matches"][0]["match_score"], 100)

    @patch("src.services.requirements.properties.all_properties")
    def test_investor_only_matches_sale_listings(self, all_properties):
        all_properties.return_value = (self.ROWS, "sample")
        result = requirements.match_properties("existing_investor", {
            "city_area": "Cary", "target_property_type": "house", "bedrooms": 3,
            "budget_max": 600000,
        })
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["matches"][0]["id"], "c")

    @patch("src.services.requirements.properties.all_properties")
    def test_no_result_does_not_invent_properties(self, all_properties):
        all_properties.return_value = (self.ROWS, "sample")
        result = requirements.match_properties("tenant", {"city_area": "Durham", "budget_max": 1000})
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["total"], 0)

    @patch("src.services.requirements.properties.all_properties")
    def test_feature_requirement_is_real_data_only(self, all_properties):
        all_properties.return_value = (self.ROWS, "sample")
        result = requirements.match_properties("tenant", {
            "city_area": "Cary", "required_features": ["Pool"],
        })
        self.assertEqual(result["total"], 0)


class RequirementContextTests(unittest.TestCase):
    def test_context_note_tells_ai_not_to_repeat_known_questions(self):
        row = {"role": "tenant", "profile_type": "tenant", "requirements_json": {
            "city_area": "Cary", "budget_max": 2500, "bedrooms": 3,
        }}
        note = requirements.context_note(row)
        self.assertIn("KNOWN USER PREFERENCES", note)
        self.assertIn("Do not ask again", note)
        self.assertIn("Cary", note)


if __name__ == "__main__":
    unittest.main()

class RequirementUpdateTests(unittest.TestCase):
    def test_update_request_is_only_a_proposal(self):
        row = {"role": "new_investor", "profile_type": "new_investor", "requirements_json": {"budget_max": 500000}}
        proposal = requirements.detect_update_request("Increase my budget to $550K", row)
        self.assertEqual(proposal["patch"]["budget_max"], 550000)
        self.assertEqual(row["requirements_json"]["budget_max"], 500000)


