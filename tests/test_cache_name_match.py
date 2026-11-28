"""Building-name match against verified cache entries (geocache._name_lookup).

No Firestore: `_verified_entries` and the exact/fuzzy lookups are patched.
"""

import tests  # noqa: F401 - environment for app module imports
import unittest
from unittest import mock

from app.db import geocache


def entry(doc_id, raw, lat, lon, pincode=None, **extra):
    data = {
        "address_raw": raw,
        "address_normalized": geocache.normalize_address(raw),
        "latitude": lat,
        "longitude": lon,
        "verified": True,
    }
    if pincode:
        data["pincode"] = pincode
    data.update(extra)
    return doc_id, data


TOWER_C = entry(
    "godrej-c",
    "Tower C, Godrej Park Retreat, Chikkakkannahali, Hadosiddapura, Carmelaram, "
    "Kodathi, JHC, Bengaluru, Karnataka 560035, India",
    12.89225, 77.70545,
)


def lookup(entries, names, pin=None, landmark=None):
    with mock.patch.object(geocache, "_verified_entries", return_value=entries):
        return geocache._name_lookup(names, pin, landmark)


class NameLookupTests(unittest.TestCase):
    def test_same_building_matches_without_tower_or_area_names(self):
        hit = lookup(
            [TOWER_C],
            ["Godrej Park Retreat", "Kodathi Wipro"],
            landmark="Near Kodathi Wipro",
        )
        self.assertEqual(hit[0], "godrej-c")

    def test_tower_and_flat_in_the_name_do_not_matter(self):
        hit = lookup([TOWER_C], ["Tower F Godrej Park Retreat"])
        self.assertEqual(hit[0], "godrej-c")

    def test_one_word_name_never_matches(self):
        self.assertIsNone(lookup([TOWER_C], ["Godrej"]))

    def test_every_name_word_must_be_present(self):
        self.assertIsNone(lookup([TOWER_C], ["Godrej Park Meadows"]))

    def test_east_is_not_west(self):
        east = entry("oc-east", "Sharavathi Century 22 Apartment, Owners Court East Layout, "
                     "Kasavanahalli, Bengaluru 560035", 12.9, 77.7)
        self.assertIsNone(lookup([east], ["Owners Court West"]))
        self.assertEqual(lookup([east], ["Owners Court East"])[0], "oc-east")

    def test_a_name_that_is_only_the_landmark_is_not_a_building(self):
        wipro = entry("wipro", "Wipro Kodathi Campus, Kodathi Wipro Gate, Bengaluru 560035", 12.9, 77.7)
        self.assertIsNone(lookup([wipro], ["Kodathi Wipro"], landmark="Near Kodathi Wipro"))

    def test_name_pointing_at_far_apart_places_is_not_used(self):
        a = entry("a", "Bren Imperia, Shubh Enclave, Harlur Road, Bengaluru", 12.9072, 77.6721)
        b = entry("b", "SJR Watermark, Shubh Enclave, 1st Main Road, Bengaluru", 12.9138, 77.6708)
        far = entry("c", "Shubh Enclave, Ambalipura, 9th Main Road, Bengaluru", 12.9080, 77.6790)
        self.assertIsNone(lookup([a, b, far], ["Shubh Enclave"]))

    def test_entries_of_one_complex_within_the_spread_are_used(self):
        a = entry("t1", "Tower A, Godrej Park Retreat, Kodathi, Bengaluru", 12.89220, 77.70540)
        b = entry("t2", "Tower B, Godrej Park Retreat, Kodathi, Bengaluru", 12.89260, 77.70580)
        hit = lookup([a, b], ["Godrej Park Retreat"])
        self.assertIn(hit[0], {"t1", "t2"})

    def test_pincode_on_both_sides_must_agree(self):
        wrong = entry("g", "Godrej Park Retreat, Kodathi, Bengaluru 560103", 12.89, 77.70)
        self.assertIsNone(lookup([wrong], ["Godrej Park Retreat"], pin="560035"))
        self.assertEqual(lookup([wrong], ["Godrej Park Retreat"], pin="560103")[0], "g")

    def test_label_without_pincode_is_not_blocked(self):
        self.assertEqual(lookup([TOWER_C], ["Godrej Park Retreat"], pin=None)[0], "godrej-c")

    def test_entry_without_coordinates_is_skipped(self):
        bad = entry("bad", "Godrej Park Retreat, Kodathi", None, None)
        self.assertIsNone(lookup([bad], ["Godrej Park Retreat"]))

    def test_no_names_no_match(self):
        self.assertIsNone(lookup([TOWER_C], []))
        self.assertIsNone(lookup([TOWER_C], None))


class LookupWiringTests(unittest.TestCase):
    ADDRESS = "Godrej Park Retreat, Sarjapura Road, Kodathi Wipro, Bangalore, India"
    NAMES = ["Godrej Park Retreat", "Kodathi Wipro"]

    def _run(self, **kwargs):
        collection = mock.MagicMock()
        with mock.patch.object(geocache, "_verified_entries", return_value=[TOWER_C]), \
                mock.patch.object(geocache, "_exact_lookup", return_value=None), \
                mock.patch.object(geocache, "_fuzzy_lookup", return_value=None), \
                mock.patch.object(geocache, "_collection", return_value=collection), \
                mock.patch.object(geocache, "_record_variant") as record_variant:
            res = geocache.get_cached_geocode_with_outcome(
                self.ADDRESS, lookup_address=self.ADDRESS, include_cache_id=True,
                match_names=self.NAMES, landmark="Near Kodathi Wipro", **kwargs)
        return res, record_variant

    def test_flag_off_is_a_miss(self):
        res, _rv = self._run()
        result, outcome = res[0], res[1]   # a miss is a 2-tuple
        self.assertIsNone(result)
        self.assertEqual(outcome, "miss")

    def test_flag_on_hits_as_fuzzy_and_records_no_variant(self):
        (result, outcome, doc_id), record_variant = self._run(name_match=True)
        self.assertEqual((outcome, doc_id), ("fuzzy_hit", "godrej-c"))
        self.assertEqual(result["latitude"], 12.89225)
        record_variant.assert_not_called()

    def test_existing_fuzzy_hit_still_records_its_variant(self):
        collection = mock.MagicMock()
        with mock.patch.object(geocache, "_exact_lookup", return_value=None), \
                mock.patch.object(geocache, "_fuzzy_lookup", return_value=TOWER_C), \
                mock.patch.object(geocache, "_collection", return_value=collection), \
                mock.patch.object(geocache, "_record_variant") as record_variant:
            geocache.get_cached_geocode_with_outcome(
                self.ADDRESS, lookup_address=self.ADDRESS, name_match=True,
                match_names=self.NAMES)
        record_variant.assert_called_once()


if __name__ == "__main__":
    unittest.main()
