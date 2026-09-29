"""classify_places(), places_search_classified() and the feature-flag reader.

No Google or Firestore access: requests.post and the Firestore client are
patched.
"""

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.db import feature_flags
from app.services import geocoding
from app.services.geocoding import classify_places, places_search_classified


def place(pid, lat, lng, pincode=None, located=True):
    p = {"id": pid, "formattedAddress": f"{pid} addr", "types": [],
         "addressComponents": ([{"types": ["postal_code"], "longText": pincode}] if pincode else [])}
    if located:
        p["location"] = {"latitude": lat, "longitude": lng}
    return p


BASE = (12.8801, 77.7302)
NEAR_100M = (12.8810, 77.7302)     # ~100 m north
NEAR_250M = (12.8801, 77.7325)     # ~250 m east
FAR_2KM = (12.8981, 77.7302)       # ~2 km north


class ClassifyPlacesTests(unittest.TestCase):
    def test_no_places_is_zero_results(self):
        self.assertEqual(classify_places([], "560035", True), (None, "ZERO_RESULTS"))

    def test_one_in_pincode_is_confident(self):
        ps = [place("a", *BASE, "560035")]
        self.assertEqual(classify_places(ps, "560035", True), (ps[0], "CONFIDENT"))

    def test_three_in_pincode_within_300m_is_confident(self):
        ps = [place("a", *BASE, "560035"), place("b", *NEAR_100M, "560035"),
              place("c", *NEAR_250M, "560035")]
        self.assertEqual(classify_places(ps, "560035", True), (ps[0], "CONFIDENT"))

    def test_two_in_pincode_2km_apart_is_multi_and_picks_first_in_pincode(self):
        ps = [place("out", *BASE, "560037"), place("a", *BASE, "560035"),
              place("b", *FAR_2KM, "560035")]
        picked, resolution = classify_places(ps, "560035", True)
        self.assertEqual((picked["id"], resolution), ("a", "MULTI_CANDIDATE"))

    def test_out_of_pincode_candidates_are_ignored_for_spread(self):
        ps = [place("a", *BASE, "560035"), place("far-out", *FAR_2KM, "560037")]
        self.assertEqual(classify_places(ps, "560035", True)[1], "CONFIDENT")

    def test_none_in_pincode_is_mismatch_with_first(self):
        ps = [place("a", *BASE, "560037"), place("b", *FAR_2KM, "560099")]
        self.assertEqual(classify_places(ps, "560035", True), (ps[0], "PINCODE_MISMATCH"))

    def test_no_pincode_in_address_uses_all_candidates(self):
        close = [place("a", *BASE, "560037"), place("b", *NEAR_100M, "560035")]
        far = [place("a", *BASE, "560037"), place("b", *FAR_2KM, "560035")]
        self.assertEqual(classify_places(close, None, True)[1], "CONFIDENT")
        self.assertEqual(classify_places(far, None, True)[1], "MULTI_CANDIDATE")

    def test_candidates_without_location_are_skipped(self):
        ps = [place("x", 0, 0, "560035", located=False), place("a", *BASE, "560035")]
        self.assertEqual(classify_places(ps, "560035", True)[0]["id"], "a")

    def test_flag_off_is_todays_pick(self):
        ps = [place("out", *BASE, "560037"), place("a", *FAR_2KM, "560035")]
        self.assertEqual(classify_places(ps, "560035", False), (ps[0], "PINCODE_MISMATCH"))
        ps = [place("a", *BASE, "560035"), place("b", *FAR_2KM, "560035")]
        self.assertEqual(classify_places(ps, "560035", False), (ps[0], "CONFIDENT"))


def _resp(places):
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"places": places}
    return r


class PlacesSearchClassifiedTests(unittest.TestCase):
    ADDR = "Asha, 12 Kodathi Main Road, Bengaluru 560035"

    def test_flag_off_sends_todays_payload_and_legacy_contract(self):
        with patch.object(geocoding.requests, "post",
                          return_value=_resp([place("a", *BASE, "560035"),
                                              place("b", *FAR_2KM, "560035")])) as post:
            result, err, code = geocoding.places_search_address(self.ADDR)
        self.assertNotIn("pageSize", post.call_args.kwargs["json"])
        self.assertEqual((result["place_id"], err, code), ("a", None, None))
        self.assertTrue(result["pincode_match"])

    def test_flag_on_asks_for_5_and_classifies(self):
        with patch.object(geocoding.requests, "post",
                          return_value=_resp([place("a", *BASE, "560035"),
                                              place("b", *FAR_2KM, "560035")])) as post:
            result, err, code, resolution, source = places_search_classified(self.ADDR, use_v2=True)
        self.assertEqual(post.call_args.kwargs["json"]["pageSize"], 5)
        self.assertEqual((result["place_id"], resolution, source), ("a", "MULTI_CANDIDATE", "places"))

    def test_zero_results_retries_once_with_short_address(self):
        with patch.object(geocoding.requests, "post",
                          side_effect=[_resp([]), _resp([place("a", *BASE, "560035")])]) as post:
            result, _e, _c, resolution, source = places_search_classified(
                self.ADDR, use_v2=True, retry_address="Kodathi, Bengaluru, 560035")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["json"]["textQuery"], "Kodathi, Bengaluru, 560035")
        self.assertEqual((result["place_id"], resolution, source), ("a", "CONFIDENT", "places_retry"))

    def test_retry_also_empty_is_zero_results(self):
        with patch.object(geocoding.requests, "post", side_effect=[_resp([]), _resp([])]):
            out = places_search_classified(self.ADDR, use_v2=True, retry_address="Kodathi")
        self.assertEqual((out[0], out[2], out[3], out[4]),
                         (None, "ADDRESS_NOT_FOUND", "ZERO_RESULTS", None))

    def test_no_retry_with_flag_off_or_service_error(self):
        with patch.object(geocoding.requests, "post", return_value=_resp([])) as post:
            out = places_search_classified(self.ADDR, use_v2=False, retry_address="Kodathi")
        self.assertEqual((post.call_count, out[3]), (1, "ZERO_RESULTS"))
        with patch.object(geocoding.requests, "post",
                          side_effect=geocoding.requests.ConnectionError("down")) as post, \
             patch.object(geocoding.time, "sleep"):
            out = places_search_classified(self.ADDR, use_v2=True, retry_address="Kodathi")
        self.assertEqual((post.call_count, out[3]), (3, "SERVICE_ERROR"))

    def test_found_without_coordinates_is_zero_results(self):
        with patch.object(geocoding.requests, "post",
                          return_value=_resp([place("x", 0, 0, located=False)])):
            out = places_search_classified(self.ADDR, use_v2=False)
        self.assertEqual((out[1], out[3]), ("Place was found but no coordinates were returned.",
                                            "ZERO_RESULTS"))

    def test_missing_address(self):
        self.assertEqual(places_search_classified("  ")[3], "MISSING_ADDRESS")


class _Coll:
    def __init__(self, docs=None, error=None):
        self.docs, self.error, self.calls = docs or {}, error, 0

    def stream(self):
        self.calls += 1
        if self.error:
            raise self.error
        return [SimpleNamespace(id=k, to_dict=lambda v=v: v) for k, v in self.docs.items()]


class FeatureFlagTests(unittest.TestCase):
    def _with(self, coll):
        feature_flags.reset_cache()
        self.addCleanup(feature_flags.reset_cache)
        return patch.object(feature_flags, "get_fs_client",
                            return_value=SimpleNamespace(collection=lambda _n: coll))

    def test_enabled_true_only(self):
        coll = _Coll({"geocodeResolutionV2": {"enabled": True}, "other": {"enabled": "yes"}})
        with self._with(coll):
            self.assertTrue(feature_flags.geocode_resolution_v2())
            self.assertFalse(feature_flags.is_enabled("other"))
            self.assertFalse(feature_flags.is_enabled("missing"))

    def test_read_error_counts_as_off(self):
        with self._with(_Coll(error=RuntimeError("firestore down"))):
            self.assertFalse(feature_flags.geocode_resolution_v2())

    def test_values_cached_for_ttl(self):
        coll = _Coll({"geocodeResolutionV2": {"enabled": True}})
        with self._with(coll):
            feature_flags.is_enabled("geocodeResolutionV2")
            feature_flags.is_enabled("geocodeResolutionV2")
        self.assertEqual(coll.calls, 1)


if __name__ == "__main__":
    unittest.main()
