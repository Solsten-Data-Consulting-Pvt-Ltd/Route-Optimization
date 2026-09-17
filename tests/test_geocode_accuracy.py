import unittest

from app.services.address import (
    build_geocode_candidates,
    extract_society,
    is_plus_code,
    name_looks_like_place,
)
from app.services.geocoding import _society_supported, is_nav_grade


class AddressCandidateTests(unittest.TestCase):
    def test_does_not_lead_with_person_name(self):
        parsed = build_geocode_candidates(
            "Somashekar",
            "166/10, Babureddy Building, Sarjapura Road, Kodathi Gate, Mulluru, Bengaluru, Karnataka, 560035",
        )
        self.assertTrue(all("somashekar" not in c.lower() for c in parsed["candidates"]))
        self.assertEqual(parsed["pincode"], "560035")
        self.assertIn("Babureddy", parsed["society"])

    def test_strips_student_name_noise(self):
        parsed = build_geocode_candidates(
            "Sanaya Singhania, Grade 9C, Student ID: 029006827, Girls International House (GIH)",
            "The International School Bangalore, NAFL Valley, Whitefield - Sarjapur Road, Near Dommasandra Circle, Bangalore, Karnataka 562125",
        )
        joined = " | ".join(parsed["candidates"]).lower()
        self.assertNotIn("sanaya", joined)
        self.assertNotIn("029006827", joined)
        self.assertIn("school", joined)
        self.assertEqual(parsed["pincode"], "562125")

    def test_extracts_society_names(self):
        self.assertIn("Assetz", extract_society("B1057, Assetz 63 Degree East, Kodathi, 560035"))
        self.assertIn("Trifecta", extract_society("101 Trifecta Beuno, Carmelram Post, Kodathi, 560035"))
        self.assertIn("Anaaya", extract_society("Plot No 42, Anaaya Bliss Layout, Kodathi Village, 560035"))
        self.assertIn("Adarsh", extract_society("Villa 65, Adarsh Sanctuary, Kodathi, 560035"))

    def test_place_like_receiver_name(self):
        self.assertTrue(name_looks_like_place("Twachaa Pharmacy"))
        self.assertFalse(name_looks_like_place("Somashekar"))
        parsed = build_geocode_candidates(
            "Twachaa Pharmacy",
            "Shop No 32, R K Complex, 2nd Floor, Opp APL Hotel, Domasandra Circle, 562125",
        )
        self.assertTrue(any("Twachaa Pharmacy" in c for c in parsed["candidates"]))

    def test_plus_code_detection(self):
        self.assertTrue(is_plus_code("WM5W+26G, Gear School Rd, Doddakannelli, Bengaluru, Karnataka 560035, India"))
        self.assertFalse(is_plus_code("Assetz 63 Degree East, Kodathi, Karnataka 560035, India"))


class NavGradeTests(unittest.TestCase):
    def test_rejects_plus_code_rooftop(self):
        parsed = {
            "location_type": "ROOFTOP",
            "partial_match": True,
            "is_plus_code": True,
            "pincode": "560035",
            "formatted_address": "WM5W+26G, Gear School Rd, Doddakannelli, Bengaluru, Karnataka 560035, India",
        }
        self.assertFalse(is_nav_grade(parsed, expected_pincode="560035", society="Anaaya Bliss Layout"))

    def test_accepts_society_partial_match(self):
        parsed = {
            "location_type": "ROOFTOP",
            "partial_match": True,
            "is_plus_code": False,
            "pincode": "560035",
            "formatted_address": "Assetz 63 Degree East, off, Sarajapura Rd, Kodathi, Karnataka 560035, India",
        }
        self.assertTrue(is_nav_grade(parsed, expected_pincode="560035", society="Assetz 63 Degree East"))
        self.assertTrue(_society_supported(parsed, "Assetz 63 Degree East"))

    def test_rejects_wrong_pin(self):
        parsed = {
            "location_type": "ROOFTOP",
            "partial_match": False,
            "is_plus_code": False,
            "pincode": "560068",
            "formatted_address": "Some building, Bengaluru, Karnataka 560068, India",
        }
        self.assertFalse(is_nav_grade(parsed, expected_pincode="560035", society="Some building"))


if __name__ == "__main__":
    unittest.main()
