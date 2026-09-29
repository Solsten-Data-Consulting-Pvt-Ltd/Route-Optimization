"""Scan-time Places candidates stored for doubtful pins (geocode_candidates).

No Google, BigQuery or Firestore access: every external call is patched.
"""

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

import unittest
from unittest.mock import MagicMock, patch

from app.db import firestore as firestore_mod
from app.schemas import ConfirmedLocation
from app.services import geocoding
from app.services import save as save_mod
from app.services.geocoding import places_search_classified
from tests.test_address_resolution_pipeline import ADDRESS, _Doc, pin, run_save
from tests.test_geocode_resolution import BASE, FAR_2KM, _resp, place

ADDR = "Asha, 12 Kodathi Main Road, Bengaluru 560035"
CANDS = [
    {"name": "A", "formatted_address": "a addr", "latitude": 12.88, "longitude": 77.73,
     "pincode": "560035", "place_id": "a", "in_pincode": True},
    {"name": "B", "formatted_address": "b addr", "latitude": 12.90, "longitude": 77.73,
     "pincode": "560035", "place_id": "b", "in_pincode": True},
]


class CandidateSummaryTests(unittest.TestCase):
    def test_flag_on_attaches_every_located_candidate(self):
        with patch.object(geocoding.requests, "post",
                          return_value=_resp([place("a", *BASE, "560035"),
                                              place("b", *FAR_2KM, "560035"),
                                              place("c", 0, 0, "560102", located=False)])):
            result, _e, _c, resolution, _s = places_search_classified(ADDR, use_v2=True)
        self.assertEqual(resolution, "MULTI_CANDIDATE")
        self.assertEqual([c["place_id"] for c in result["candidates"]], ["a", "b"])
        self.assertTrue(all(c["in_pincode"] for c in result["candidates"]))
        self.assertEqual(result["candidates"][0]["latitude"], BASE[0])

    def test_flag_off_attaches_nothing(self):
        with patch.object(geocoding.requests, "post",
                          return_value=_resp([place("a", *BASE, "560035")])):
            result, _e, _c = geocoding.places_search_address(ADDR)
        self.assertNotIn("candidates", result)


class PipelineCandidateTests(unittest.TestCase):
    def _run(self, **kw):
        with patch.object(save_mod, "set_routing_candidates") as setc:
            run_save([_Doc("C1")], **kw)
        return setc.call_args.args[0]

    def test_auto_multi_candidate_stores_the_list(self):
        stored = self._run(use_v2=True,
                           classified=(pin(candidates=CANDS), None, None, "MULTI_CANDIDATE", "places"))
        self.assertEqual(stored, {"C1": CANDS})

    def test_confident_clears_the_list(self):
        stored = self._run(use_v2=True,
                           classified=(pin(candidates=CANDS), None, None, "CONFIDENT", "places"))
        self.assertEqual(stored, {"C1": None})

    def test_flag_off_never_stores(self):
        stored = self._run(use_v2=False,
                           classified=(pin(candidates=CANDS), None, None, "MULTI_CANDIDATE", "places"))
        self.assertEqual(stored, {"C1": None})

    def test_accepted_preview_pin_keeps_the_sent_list(self):
        confirmed = {"C1": ConfirmedLocation(latitude=12.88, longitude=77.73,
                                             resolution="MULTI_CANDIDATE", source="preview",
                                             candidates=CANDS + [{"latitude": "bad"}])}
        stored = self._run(use_v2=True, confirmed=confirmed)
        self.assertEqual([c["place_id"] for c in stored["C1"]], ["a", "b"])

    def test_corrected_pin_clears_the_list(self):
        confirmed = {"C1": ConfirmedLocation(latitude=12.88, longitude=77.73, corrected=True,
                                             overriddenBy="admin-1", source="admin",
                                             resolution="KNOWN_GOOD", candidates=CANDS)}
        stored = self._run(use_v2=True, confirmed=confirmed)
        self.assertEqual(stored, {"C1": None})


class PreviewCandidateTests(unittest.TestCase):
    def test_preview_returns_candidates_only_for_doubtful_pins(self):
        with patch.object(save_mod, "_use_v2", return_value=True), \
             patch.object(save_mod, "_geocode",
                          return_value=(pin(candidates=CANDS), None, None, "miss",
                                        ("MULTI_CANDIDATE", "places"))):
            out = save_mod.preview_geocode("Asha", ADDRESS)
        self.assertEqual(out["candidates"], CANDS)
        with patch.object(save_mod, "_use_v2", return_value=True), \
             patch.object(save_mod, "_geocode",
                          return_value=(pin(candidates=CANDS), None, None, "miss",
                                        ("CONFIDENT", "places"))):
            out = save_mod.preview_geocode("Asha", ADDRESS)
        self.assertEqual(out["candidates"], [])


class FirestoreWriteTests(unittest.TestCase):
    def test_writes_merge_and_clears_with_none(self):
        client = MagicMock()
        batch = client.batch.return_value
        with patch.object(firestore_mod, "get_fs_client", return_value=client):
            count = firestore_mod.set_routing_candidates({"C1": CANDS, "C2": None})
        self.assertEqual(count, 2)
        payloads = [c.args[1] for c in batch.set.call_args_list]
        self.assertEqual(payloads, [{"geocode_candidates": CANDS}, {"geocode_candidates": None}])
        self.assertTrue(all(c.kwargs == {"merge": True} for c in batch.set.call_args_list))


if __name__ == "__main__":
    unittest.main()
