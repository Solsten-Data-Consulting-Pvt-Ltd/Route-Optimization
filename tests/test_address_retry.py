"""build_retry_address(): the one shortened query before ZERO_RESULTS."""

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

import unittest

from app.services.address import build_retry_address


class RetryAddressTests(unittest.TestCase):
    def test_components_based(self):
        comp = {"premise": "Prestige Lakeside", "sub_locality": "Varthur",
                "locality": "Bengaluru East", "city": "Bengaluru", "postal_code": "560087"}
        self.assertEqual(build_retry_address("Flat 3B, anything", comp),
                         "Prestige Lakeside, Varthur, Bengaluru East, Bengaluru, 560087")

    def test_components_skip_blanks(self):
        comp = {"premise": "", "locality": "Kodathi", "city": "Bengaluru", "postal_code": "560035"}
        self.assertEqual(build_retry_address("x", comp), "Kodathi, Bengaluru, 560035")

    def test_strips_flat_floor_wing(self):
        self.assertEqual(
            build_retry_address("Flat 3B, 2nd Floor, Wing C, Prestige Lakeside, Varthur Road, Bengaluru 560087"),
            "Prestige Lakeside, Varthur Road, Bengaluru 560087")

    def test_strips_hash_door_and_house_numbers(self):
        self.assertEqual(
            build_retry_address("#31, Doctor Narayanaswamy Layout, Kodathi, Bengaluru 560035"),
            "Doctor Narayanaswamy Layout, Kodathi, Bengaluru 560035")
        self.assertEqual(build_retry_address("Room 4, House No 12, MG Road, Bengaluru"),
                         "MG Road, Bengaluru")

    def test_none_when_nothing_changes(self):
        self.assertIsNone(build_retry_address("Kodathi Village Main Road, Bengaluru 560035"))
        self.assertIsNone(build_retry_address(""))
        self.assertIsNone(build_retry_address("Flat 3", None))   # strips to nothing


if __name__ == "__main__":
    unittest.main()
