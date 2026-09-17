import unittest

from app.services.address import (
    build_geocode_address,
    clean_address_for_geocoding,
    extract_pincode,
    normalize_to_single_line,
)


class AddressTests(unittest.TestCase):
    def test_normalizes_whitespace_without_losing_address_parts(self):
        address = "  166/10, Babureddy Building\nSarjapura Road\tBengaluru  "
        self.assertEqual(
            normalize_to_single_line(address),
            "166/10, Babureddy Building Sarjapura Road Bengaluru",
        )

    def test_extracts_six_digit_pincode(self):
        self.assertEqual(extract_pincode("Kodathi, Bengaluru, Karnataka 560035"), "560035")
        self.assertIsNone(extract_pincode("Kodathi, Bengaluru"))

    def test_builds_geocode_address_without_duplicate_name(self):
        address = "Twachaa Pharmacy, Domasandra Circle, 562125"
        self.assertEqual(build_geocode_address("Twachaa Pharmacy", address), address)
        self.assertEqual(
            build_geocode_address("Twachaa Pharmacy", "Domasandra Circle, 562125"),
            "Twachaa Pharmacy, Domasandra Circle, 562125",
        )

    def test_cleans_obvious_separator_noise(self):
        self.assertEqual(
            clean_address_for_geocoding(" A,, B ;C\nD "),
            "A, B; C D",
        )


if __name__ == "__main__":
    unittest.main()
