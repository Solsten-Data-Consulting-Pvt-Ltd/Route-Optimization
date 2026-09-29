"""Address resolution through the save pipeline, the preview, the DRS memo
and the verified cache (geocode_status / geocode_resolution / geocode_source).

No Google, BigQuery or Firestore access: every external call is patched.
"""

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

import unittest
from unittest.mock import patch

from app.db import drs_memo
from app.schemas import ConfirmedLocation, GeocodePreviewRequest
from app.services import save as save_mod
from tests.test_drs_address_memo import LIKHITHA_1, LIKHITHA_2, FakeMemoStore, info

ADDRESS = "12 Kodathi Main Road, Bengaluru 560035"


def pin(pincode="560035", pincode_match=True, **kw):
    return {"latitude": 12.88, "longitude": 77.73, "pincode": pincode,
            "pincode_match": pincode_match, "locality": "Kodathi", "types": [],
            "formatted_address": "Kodathi", "place_id": "p1", **kw}


class _Doc:
    def __init__(self, cid, address=ADDRESS, name="Asha", components=None):
        self.id = cid
        receiver = {"name": name, "address": address}
        if components:
            receiver["addressComponent"] = components
        self._data = {"consignmentId": cid, "drsNo": "D1", "drsId": "DRS1",
                      "driverNumericId": 7, "receiver": receiver}

    def to_dict(self):
        return self._data


def run_save(docs, *, use_v2, confirmed=None, cache=(None, "miss"),
             classified=None, legacy=None, reconcile=None):
    """Run save_consignments_pipeline; returns (result, rows by id, mocks)."""
    classified = classified or (pin(), None, None, "CONFIDENT", "places")
    legacy = legacy or (classified[0], classified[1], classified[2])
    reconcile = reconcile or (lambda _drs, _i, result, *_a, **_k: (result, None))
    with patch.object(save_mod, "_use_v2", return_value=use_v2), \
         patch.object(save_mod, "get_consignments_by_id", return_value=(docs, [])), \
         patch.object(save_mod, "get_cached_geocode_with_outcome", return_value=cache), \
         patch.object(save_mod, "places_search_classified", return_value=classified) as pc, \
         patch.object(save_mod, "places_search_address", return_value=legacy) as pa, \
         patch.object(save_mod.drs_memo, "load_entries", return_value={}), \
         patch.object(save_mod.drs_memo, "remember"), \
         patch.object(save_mod.drs_memo, "reconcile", side_effect=reconcile), \
         patch.object(save_mod, "save_to_cache"), \
         patch.object(save_mod, "merge_routing_rows") as merge, \
         patch.object(save_mod, "deactivate_stale_routing_rows"), \
         patch.object(save_mod, "fetch_rows_by_consignment_drs_pairs", return_value=[]), \
         patch.object(save_mod, "upsert_consignments_routing"), \
         patch.object(save_mod, "write_drs_cache_metrics"):
        result = save_mod.save_consignments_pipeline([d.id for d in docs],
                                                     confirmed_locations=confirmed)
    rows = {r["consignmentId"]: r for r in merge.call_args.args[0]}
    return result, rows, {"classified": pc, "legacy": pa}


def triple(row):
    return row["geocode_status"], row["geocode_resolution"], row["geocode_source"]


class PipelineStatusTests(unittest.TestCase):
    def test_flag_on_multi_candidate_is_needs_review_but_routed(self):
        _, rows, m = run_save([_Doc("C1")], use_v2=True,
                              classified=(pin(), None, None, "MULTI_CANDIDATE", "places"))
        row = rows["C1"]
        self.assertEqual(triple(row), ("needs_review", "MULTI_CANDIDATE", "places"))
        self.assertEqual(row["geocode_error"], "Several possible locations found.")
        self.assertEqual((row["latitude"], row["longitude"]), (12.88, 77.73))
        self.assertIsNotNone(row["geohash_exact_loc"])
        m["legacy"].assert_not_called()

    def test_flag_on_pincode_mismatch_message_and_flag(self):
        _, rows, _ = run_save([_Doc("C1")], use_v2=True,
                              classified=(pin("560037", False), None, None,
                                          "PINCODE_MISMATCH", "places"))
        row = rows["C1"]
        self.assertEqual(triple(row), ("needs_review", "PINCODE_MISMATCH", "places"))
        self.assertEqual(row["geocode_error"], "Map result is in pincode 560037; address says 560035.")
        self.assertEqual(row["exception_flag"], "PINCODE_MISMATCH")

    def test_flag_off_is_todays_call_and_status_but_codes_filled(self):
        _, rows, m = run_save([_Doc("C1")], use_v2=False,
                              legacy=(pin("560037", False), None, None))
        row = rows["C1"]
        self.assertEqual(triple(row), ("success", "PINCODE_MISMATCH", "places"))
        self.assertIsNone(row["geocode_error"])
        self.assertEqual(row["exception_flag"], "PINCODE_MISMATCH")   # as today
        m["classified"].assert_not_called()

    def test_flag_on_retry_address_comes_from_components(self):
        comp = {"premise": "", "locality": "Kodathi", "city": "Bengaluru", "postal_code": "560035"}
        _, rows, m = run_save([_Doc("C1", components=comp)], use_v2=True,
                              classified=(pin(), None, None, "CONFIDENT", "places_retry"))
        self.assertEqual(m["classified"].call_args.kwargs["retry_address"],
                         "Kodathi, Bengaluru, 560035")
        self.assertEqual(triple(rows["C1"]), ("success", "CONFIDENT", "places_retry"))

    def test_zero_results_row_flag_on_and_off(self):
        fail = (None, "Address could not be found. Please check and correct the address.",
                "ADDRESS_NOT_FOUND", "ZERO_RESULTS", None)
        result, rows, _ = run_save([_Doc("C1")], use_v2=True, classified=fail)
        self.assertEqual(triple(rows["C1"]), ("failed", "ZERO_RESULTS", None))
        self.assertEqual(rows["C1"]["geocode_error"], "Address not found on the map.")
        self.assertEqual(result["failures"][0]["reason"], fail[1])   # 3PL string unchanged
        _, rows, _ = run_save([_Doc("C1")], use_v2=False, legacy=fail[:3])
        self.assertEqual(triple(rows["C1"]), ("failed", "ZERO_RESULTS", None))
        self.assertEqual(rows["C1"]["geocode_error"], fail[1])        # today's text, flag off

    def test_service_error_row(self):
        fail = (None, "Places service unreachable. Please try again.",
                "GEOCODING_SERVICE_UNREACHABLE", "SERVICE_ERROR", None)
        _, rows, _ = run_save([_Doc("C1")], use_v2=True, classified=fail)
        self.assertEqual(triple(rows["C1"]), ("failed", "SERVICE_ERROR", None))
        self.assertEqual(rows["C1"]["geocode_error"], "Map lookup unavailable — retry or place the pin.")

    def test_missing_address_row(self):
        _, rows, _ = run_save([_Doc("C1", address="", name="")], use_v2=True)
        self.assertEqual(triple(rows["C1"]), ("failed", "MISSING_ADDRESS", None))
        self.assertEqual(rows["C1"]["geocode_error"], "No receiver address on this consignment.")


class CacheTierTests(unittest.TestCase):
    def test_verified_exact_and_fuzzy_hits_are_known_good(self):
        _, rows, _ = run_save([_Doc("C1")], use_v2=True, cache=(pin(), "exact_hit"))
        self.assertEqual(triple(rows["C1"]), ("success", "KNOWN_GOOD", "cache_exact"))
        _, rows, _ = run_save([_Doc("C1")], use_v2=True, cache=(pin(), "fuzzy_hit"))
        self.assertEqual(triple(rows["C1"]), ("success", "KNOWN_GOOD", "cache_fuzzy"))

    def test_verified_hit_in_another_pincode_is_mismatch(self):
        _, rows, _ = run_save([_Doc("C1")], use_v2=True,
                              cache=(pin("560037", False), "exact_hit"))
        self.assertEqual(triple(rows["C1"]), ("needs_review", "PINCODE_MISMATCH", "cache_exact"))


class ConfirmedLocationTests(unittest.TestCase):
    def conf(self, **kw):
        return {"C1": ConfirmedLocation(latitude=12.9, longitude=77.7, **kw)}

    def test_accepted_multi_candidate_stays_needs_review(self):
        _, rows, m = run_save([_Doc("C1")], use_v2=True,
                              confirmed=self.conf(resolution="MULTI_CANDIDATE", source="preview",
                                                  pincode="560035"))
        row = rows["C1"]
        self.assertEqual(triple(row), ("needs_review", "MULTI_CANDIDATE", "preview"))
        self.assertEqual(row["pincode"], "560035")
        m["classified"].assert_not_called()

    def test_accepted_pincode_mismatch_sets_exception_flag(self):
        _, rows, _ = run_save([_Doc("C1")], use_v2=True,
                              confirmed=self.conf(resolution="PINCODE_MISMATCH", pincode="560037"))
        row = rows["C1"]
        self.assertEqual(triple(row), ("needs_review", "PINCODE_MISMATCH", "preview"))
        self.assertEqual(row["exception_flag"], "PINCODE_MISMATCH")
        self.assertEqual(row["geocode_error"], "Map result is in pincode 560037; address says 560035.")

    def test_accepted_without_resolution_is_confident(self):   # older app builds
        _, rows, _ = run_save([_Doc("C1")], use_v2=True, confirmed=self.conf())
        self.assertEqual(triple(rows["C1"]), ("success", "CONFIDENT", "preview"))

    def test_corrected_is_known_good_confirmed_or_admin(self):
        _, rows, _ = run_save([_Doc("C1")], use_v2=True,
                              confirmed=self.conf(corrected=True, overriddenBy="EXEC1",
                                                  resolution="MULTI_CANDIDATE"))
        self.assertEqual(triple(rows["C1"]), ("success", "KNOWN_GOOD", "confirmed"))
        _, rows, _ = run_save([_Doc("C1")], use_v2=True,
                              confirmed=self.conf(corrected=True, overriddenBy="ADMIN1",
                                                  source="admin"))
        self.assertEqual(triple(rows["C1"]), ("success", "KNOWN_GOOD", "admin"))

    def test_reconcile_swapping_in_memo_pin_is_known_good(self):
        stronger = pin()
        _, rows, _ = run_save([_Doc("C1")], use_v2=True,
                              confirmed=self.conf(resolution="MULTI_CANDIDATE"),
                              reconcile=lambda *_a, **_k: (stronger, "entry1"))
        self.assertEqual(triple(rows["C1"]), ("success", "KNOWN_GOOD", "preview"))
        self.assertEqual(rows["C1"]["latitude"], 12.88)

    def test_flag_off_accepted_multi_keeps_todays_status_and_row(self):
        _, rows, _ = run_save([_Doc("C1")], use_v2=False,
                              confirmed=self.conf(resolution="MULTI_CANDIDATE", pincode="560035"))
        row = rows["C1"]
        self.assertEqual(triple(row), ("success", "MULTI_CANDIDATE", "preview"))
        self.assertIsNone(row["pincode"])          # pincode fill is flag-gated
        self.assertIsNone(row["exception_flag"])

    def test_invalid_or_failed_resolution_sent_falls_back_to_confident(self):
        for bad in ("ZERO_RESULTS", "NONSENSE"):
            _, rows, _ = run_save([_Doc("C1")], use_v2=True, confirmed=self.conf(resolution=bad))
            self.assertEqual(rows["C1"]["geocode_resolution"], "CONFIDENT")


class MemoTierTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeMemoStore()
        p = patch.object(drs_memo, "_doc", side_effect=self.store.doc)
        p.start()
        self.addCleanup(p.stop)

    def _geocode(self, recv):
        with patch.object(save_mod, "get_cached_geocode_with_outcome", return_value=(None, "miss")), \
             patch.object(save_mod, "save_to_cache"), \
             patch.object(save_mod, "places_search_address") as places:
            out = save_mod._geocode(f"{recv['name']}, {recv['address']}",
                                    lookup_address=recv["address"], drs_id="DRS-K",
                                    match_info=info(recv), consignment_id="C2")
        places.assert_not_called()
        return out

    def test_exec_corrected_memo_hit(self):
        drs_memo.remember("DRS-K", info(LIKHITHA_1), pin(), drs_memo.SOURCE_EXEC_CORRECTED)
        self.assertEqual(self._geocode(LIKHITHA_2)[4], ("KNOWN_GOOD", "memo_corrected"))

    def test_api_memo_hit_is_confident(self):
        drs_memo.remember("DRS-K", info(LIKHITHA_1), pin(), drs_memo.SOURCE_API)
        self.assertEqual(self._geocode(LIKHITHA_2)[4], ("CONFIDENT", "memo"))

    def test_accepted_or_cache_memo_hit_is_known_good(self):
        drs_memo.remember("DRS-K", info(LIKHITHA_1), pin(), drs_memo.SOURCE_GLOBAL_CACHE)
        self.assertEqual(self._geocode(LIKHITHA_2)[4], ("KNOWN_GOOD", "memo"))


class PreviewTests(unittest.TestCase):
    def test_success_returns_resolution_source_pincode(self):
        with patch.object(save_mod, "_use_v2", return_value=True), \
             patch.object(save_mod, "_geocode",
                          return_value=(pin(), None, None, "miss", ("MULTI_CANDIDATE", "places"))):
            out = save_mod.preview_geocode("Asha", ADDRESS)
        self.assertEqual(out["status"], "success")      # doubtful stays success in preview
        self.assertEqual((out["resolution"], out["source"], out["pincode"]),
                         ("MULTI_CANDIDATE", "places", "560035"))

    def test_zero_results_and_service_error_fail_with_resolution(self):
        for code, res in (("ADDRESS_NOT_FOUND", "ZERO_RESULTS"),
                          ("GEOCODING_SERVICE_UNREACHABLE", "SERVICE_ERROR")):
            with patch.object(save_mod, "_geocode", return_value=(None, "x", code, "miss", (res, None))):
                out = save_mod.preview_geocode("Asha", ADDRESS)
            self.assertEqual((out["status"], out["resolution"]), ("failed", res))

    def test_retry_uses_request_components_only_with_flag_on(self):
        comp = {"locality": "Kodathi", "city": "Bengaluru", "postal_code": "560035"}
        for flag, expected in ((True, "Kodathi, Bengaluru, 560035"), (False, None)):
            with patch.object(save_mod, "_use_v2", return_value=flag), \
                 patch.object(save_mod, "_geocode",
                              return_value=(pin(), None, None, "miss", ("CONFIDENT", "places"))) as g:
                save_mod.preview_geocode("Asha", ADDRESS, address_components=comp)
            self.assertEqual(g.call_args.kwargs["retry_address"], expected)
            self.assertEqual(g.call_args.kwargs["use_v2"], flag)

    def test_missing_address(self):
        self.assertEqual(save_mod.preview_geocode("Asha", " ")["resolution"], "MISSING_ADDRESS")

    def test_request_schemas_are_backward_compatible(self):
        self.assertIsNone(GeocodePreviewRequest(receiverAddress="x").addressComponents)
        c = ConfirmedLocation(latitude=1, longitude=1)
        self.assertEqual((c.resolution, c.source, c.pincode), (None, None, None))


if __name__ == "__main__":
    unittest.main()
