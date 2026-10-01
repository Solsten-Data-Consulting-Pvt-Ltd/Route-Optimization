import os
import unittest

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test")

from app.db.geocache import normalize_address  # noqa: E402
from app.services.address_match import match_key  # noqa: E402


class CompanySpellingTests(unittest.TestCase):
    def test_cache_key_same_for_legal_suffix_variants(self):
        a = normalize_address("Shahi Exports Pvt. Ltd., Unit 17, Bengaluru 560068")
        b = normalize_address("SHAHI EXPORT [P] LIMITED, Unit 17, Bengaluru 560068")
        c = normalize_address("Shahi Exports Private Limited, Unit 17, Bengaluru 560068")
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(a, "shahi export pvt ltd unit 17 bengaluru")

    def test_match_key_ignores_legal_form_and_plurals(self):
        self.assertEqual(
            match_key("SHAHI EXPORT [P] LTD, Unit 17, Hosur Road"),
            match_key("Shahi Exports Pvt. Ltd., Unit 17, Hosur Road"),
        )

    def test_different_companies_still_differ(self):
        self.assertNotEqual(match_key("Alpha Exports Pvt Ltd, Unit 17"),
                            match_key("Beta Exports Pvt Ltd, Unit 17"))


if __name__ == "__main__":
    unittest.main()
