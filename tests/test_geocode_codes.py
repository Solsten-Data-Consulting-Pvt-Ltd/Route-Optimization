"""Formal codes: every ERR_* maps, every resolution has a message, status_for."""

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

import unittest

from app.services import geocode_codes as codes
from app.services import geocoding


class GeocodeCodesTests(unittest.TestCase):
    def test_every_err_code_maps_to_a_resolution(self):
        for err in (geocoding.ERR_MISSING_ADDRESS, geocoding.ERR_ADDRESS_NOT_FOUND,
                    geocoding.ERR_SERVICE_UNREACHABLE, geocoding.ERR_API_ERROR):
            self.assertIn(codes.ERR_TO_RESOLUTION[err], codes.RESOLUTIONS)
        self.assertEqual(codes.resolution_for_error("ADDRESS_NOT_FOUND"), codes.ZERO_RESULTS)
        self.assertEqual(codes.resolution_for_error("SOMETHING_NEW"), codes.SERVICE_ERROR)

    def test_not_found_in_firestore_is_not_an_address_state(self):
        self.assertNotIn(geocoding.ERR_NOT_FOUND_IN_FIRESTORE, codes.ERR_TO_RESOLUTION)

    def test_every_non_good_resolution_has_a_message(self):
        for r in codes.RESOLUTIONS - {codes.KNOWN_GOOD, codes.CONFIDENT}:
            self.assertIn(r, codes.MESSAGES)
        self.assertIsNone(codes.message_for(codes.CONFIDENT))
        self.assertEqual(codes.message_for(codes.PINCODE_MISMATCH, found="560037", given="560035"),
                         "Map result is in pincode 560037; address says 560035.")

    def test_status_for_flag_on_and_off(self):
        pin = {"latitude": 1.0, "longitude": 2.0}
        for r in codes.RESOLUTIONS:
            self.assertEqual(codes.status_for(None, r, True), "failed")
            self.assertEqual(codes.status_for(None, r, False), "failed")
        for r in (codes.KNOWN_GOOD, codes.CONFIDENT):
            self.assertEqual(codes.status_for(pin, r, True), "success")
        for r in codes.REVIEW_RESOLUTIONS:
            self.assertEqual(codes.status_for(pin, r, True), "needs_review")
            self.assertEqual(codes.status_for(pin, r, False), "success")  # dark launch

    def test_sources_match_the_3pl_contract(self):
        self.assertEqual(codes.SOURCES, {
            "confirmed", "preview", "admin", "memo_corrected", "memo",
            "cache_exact", "cache_fuzzy", "places", "places_retry"})


if __name__ == "__main__":
    unittest.main()
