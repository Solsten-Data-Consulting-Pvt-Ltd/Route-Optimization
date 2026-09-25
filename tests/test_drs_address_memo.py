"""Tests for the DRS address memo (same-place pin reuse within one DRS).

Uses real parcels from a Kodathi DRS (22-09-2026):
  * Likhitha C  - 2 parcels, identical printed labels, phone 8792767027
  * Kummary Srinivasulu - 3 parcels: handwritten, printed, printed with a
    "JetMax || Kidscare || Pulsecare || Spectra" brand line

No Google, BigQuery or Firestore access: Firestore is replaced by an
in-memory fake that understands set(merge=True) and ArrayUnion.
"""

import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-key")
os.environ.setdefault("BQ_PROJECT", "test-project")
os.environ.setdefault("BQ_DATASET", "test_dataset")
os.environ.setdefault("BQ_TABLE", "test_table")
os.environ.setdefault("BQ_STRUCTURED_TABLE", "test_structured_table")
os.environ.setdefault("FIRESTORE_PROJECT", "test-project")

from google.cloud import firestore
from google.cloud.firestore_v1.transforms import ArrayUnion

from app.db import drs_memo
from app.schemas import ConfirmedLocation
from app.services import save as save_mod
from app.services.address_match import (
    consignment_match_info,
    door_numbers,
    drs_match,
    is_precise,
    match_info_from_address,
    match_key,
)

# --- Real label text ---------------------------------------------------------
LIKHITHA_FULL = (
    "PMBJK14211-PRADHANMANTRI BHARTIYA JANAUSHADHI KENDRA(PMBJK14211) "
    "NO 140/1 SHOP NO 1 1ST FLOOR KODATHI GATE KPDATHI VILLAGE MAIN ROAD "
    "BENGALORE PINCODE 8792767027 BANGALORE-560035"
)
LIKHITHA_OCR = "Kodathi Village Main Road, Kodathi Gate, Bangalore, Karnataka 560035, India"
KUMMARY_HAND = ("#31, 2nd Flore Doctor Narayanaswamy layout, Opp Ayyappa Swamy Temple, "
                "Kodathi village, Sarjapura road, Banglore - 560035")
KUMMARY_PRINT = ("#31,2nd Flore Doctor Narayanaswamy Layout,Opp Ayyappa Swamy Temple,"
                 "Kodathi Village,Sarjapura Road,Banglore Banglore - 560035")
KUMMARY_BRAND = "JetMax || Kidscare || Pulsecare || Spectra " + KUMMARY_PRINT
KUMMARY_OCR = "Doctor Narayanaswamy Layout, Kodathi, Bengaluru, Karnataka 560035, India"

COMP = {"city": "Bengaluru", "locality": "Kodathi", "postal_code": "560035",
        "premise": "", "sub_premise": ""}


def receiver(full, ocr, phone=None, name="X"):
    return {"name": name, "address": ocr, "fullAddress": full,
            "phone": phone, "addressComponent": dict(COMP)}


LIKHITHA_1 = receiver(LIKHITHA_FULL, LIKHITHA_OCR, "8792767027", "LIKHITHA C")
LIKHITHA_2 = receiver(LIKHITHA_FULL, LIKHITHA_OCR, "8792767027", "LIKHITHA C")
KUMMARY_1 = receiver(KUMMARY_HAND, KUMMARY_OCR, "8088697830", "Kummary Srinivasulu")
KUMMARY_2 = receiver(KUMMARY_PRINT, KUMMARY_OCR, "8088697830", "Kummary Srinivasulu")
KUMMARY_3 = receiver(KUMMARY_BRAND, KUMMARY_OCR, None, "Kummary Srinivasulu")

info = consignment_match_info


# --- Pure matching ------------------------------------------------------------
class MatchKeyTests(unittest.TestCase):
    def test_spellings_punctuation_phone_and_pincode_are_normalised(self):
        self.assertEqual(match_key(KUMMARY_HAND), match_key(KUMMARY_PRINT))
        key = match_key(LIKHITHA_FULL)
        self.assertNotIn("8792767027", key)
        self.assertNotIn("560035", key)
        self.assertIn("140/1", key)
        self.assertIn("bengaluru", key)

    def test_door_numbers_ignore_ordinals(self):
        self.assertEqual(door_numbers(match_key(KUMMARY_HAND)), {"31"})
        self.assertEqual(door_numbers(match_key(LIKHITHA_FULL)), {"140/1", "1"})

    def test_ocr_road_level_address_is_not_precise(self):
        self.assertFalse(is_precise(match_key(LIKHITHA_OCR), ["kodathi"]))
        self.assertTrue(is_precise(match_key(LIKHITHA_FULL), ["kodathi"]))


class ConsignmentMatchInfoTests(unittest.TestCase):
    def test_prefers_full_address_and_reads_receiver_phone(self):
        i = info(LIKHITHA_1)
        self.assertEqual(i["match_address"], LIKHITHA_FULL)
        self.assertEqual(i["phones"], ["8792767027"])
        self.assertEqual(i["pincode"], "560035")

    def test_phone_only_from_receiver_phone_never_from_text(self):
        r = dict(LIKHITHA_1, phone=None)
        self.assertEqual(info(r)["phones"], [])   # 8792767027 is in the text, ignored

    def test_phone_as_list_and_with_country_code(self):
        r = dict(LIKHITHA_1, phone=["+91 87927 67027", "junk"])
        self.assertEqual(info(r)["phones"], ["8792767027"])

    def test_falls_back_to_formatted_address(self):
        r = dict(LIKHITHA_1, fullAddress="")
        self.assertEqual(info(r)["match_address"], LIKHITHA_OCR)

    def test_premise_numbers_count_as_door_numbers(self):
        r = receiver(LIKHITHA_OCR, LIKHITHA_OCR)
        r["addressComponent"]["premise"] = "140/1"
        self.assertEqual(door_numbers(info(r)["match_key"]), {"140/1"})


class DrsMatchTests(unittest.TestCase):
    def assertMatch(self, a, b, reason=None):
        got = drs_match(info(a), info(b))
        self.assertIsNotNone(got)
        if reason:
            self.assertEqual(got, reason)

    def assertNoMatch(self, a, b):
        self.assertIsNone(drs_match(info(a), info(b)))

    def test_likhitha_two_labels(self):
        self.assertMatch(LIKHITHA_1, LIKHITHA_2, "phone")

    def test_likhitha_ocr_only_vs_full_label_matches_on_phone(self):
        ocr_only = receiver("", LIKHITHA_OCR, "8792767027")
        self.assertMatch(ocr_only, LIKHITHA_1, "phone")

    def test_kummary_all_three(self):
        self.assertMatch(KUMMARY_1, KUMMARY_2)
        self.assertMatch(KUMMARY_1, KUMMARY_3, "contained")   # no phone on #3
        self.assertMatch(KUMMARY_2, KUMMARY_3, "contained")

    def test_likhitha_vs_kummary(self):
        self.assertNoMatch(LIKHITHA_1, KUMMARY_1)

    def test_same_road_level_ocr_text_different_person(self):
        a = receiver("", LIKHITHA_OCR, "8792767027")
        b = receiver("", LIKHITHA_OCR, "9845012345")
        self.assertNoMatch(a, b)

    def test_road_level_without_phone(self):
        self.assertNoMatch(receiver("", LIKHITHA_OCR), receiver("", LIKHITHA_OCR))

    def test_different_house_same_layout(self):
        decoy = receiver(KUMMARY_HAND.replace("#31", "#32"), KUMMARY_OCR)
        self.assertNoMatch(KUMMARY_3, decoy)

    def test_house_number_vs_no_house_number(self):
        decoy = receiver("Kodathi Village, Sarjapura Road, Bangalore 560035", KUMMARY_OCR)
        self.assertNoMatch(KUMMARY_3, decoy)

    def test_different_pincode_blocks_even_same_phone(self):
        other = receiver(LIKHITHA_FULL.replace("560035", "560037"), LIKHITHA_OCR, "8792767027")
        other["addressComponent"]["postal_code"] = "560037"
        self.assertNoMatch(LIKHITHA_1, other)

    def test_same_phone_different_door_numbers(self):
        home = receiver("No 12, 3rd Cross, Kodathi Village, Bangalore 560035", LIKHITHA_OCR,
                        "8792767027")
        self.assertNoMatch(LIKHITHA_1, home)


# --- In-memory Firestore for the memo ----------------------------------------
def _resolve(value, existing):
    if isinstance(value, ArrayUnion):
        out = list(existing or [])
        out += [v for v in value.values if v not in out]
        return out
    if value is firestore.SERVER_TIMESTAMP:
        return "ts"
    return value


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        elif isinstance(v, dict):
            dst[k] = {}
            _merge(dst[k], v)
        else:
            dst[k] = _resolve(v, dst.get(k))


class FakeMemoStore:
    def __init__(self):
        self.docs = {}
        self.reads = 0

    def doc(self, drs_id):
        store = self

        class _Ref:
            def get(self_inner):
                store.reads += 1
                data = store.docs.get(drs_id)
                return SimpleNamespace(exists=data is not None,
                                       to_dict=lambda: copy.deepcopy(data))

            def set(self_inner, payload, merge=False):
                assert merge
                _merge(store.docs.setdefault(drs_id, {}), payload)

        return _Ref()


PLACES = {
    LIKHITHA_OCR: {"latitude": 12.8801, "longitude": 77.7302, "pincode": "560035",
                   "locality": "Kodathi", "formatted_address": "Kodathi Gate", "types": []},
    KUMMARY_OCR: {"latitude": 12.8902, "longitude": 77.7105, "pincode": "560035",
                  "locality": "Kodathi", "formatted_address": "Narayanaswamy Layout", "types": []},
}


def fake_places(address):
    for key, value in PLACES.items():
        if key in address:
            return dict(value), None, None
    return None, "not found", "ZERO"


class _Doc:
    def __init__(self, cid, recv, drs="DRS-KODATHI"):
        self.id = cid
        self._data = {"consignmentId": cid, "drsNo": "D1", "drsId": drs,
                      "driverNumericId": 7, "receiver": recv}

    def to_dict(self):
        return self._data


class GeocodeOrderTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeMemoStore()
        self.patches = [
            patch.object(drs_memo, "_doc", side_effect=self.store.doc),
            patch.object(save_mod, "save_to_cache"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _geocode(self, recv, cid, cache=(None, "miss"), drs="DRS-KODATHI"):
        with patch.object(save_mod, "get_cached_geocode_with_outcome", return_value=cache), \
             patch.object(save_mod, "places_search_address", side_effect=fake_places) as places:
            out = save_mod._geocode(
                f"{recv['name']}, {recv['address']}", lookup_address=recv["address"],
                drs_id=drs, match_info=info(recv), consignment_id=cid)
        return out, places.call_count

    def test_second_parcel_reuses_first_without_places(self):
        (r1, *_), calls1 = self._geocode(LIKHITHA_1, "C1")
        (r2, _, _, outcome), calls2 = self._geocode(LIKHITHA_2, "C2")
        self.assertEqual((calls1, calls2), (1, 0))
        self.assertEqual(outcome, "drs_hit")
        self.assertEqual((r2["latitude"], r2["longitude"]), (r1["latitude"], r1["longitude"]))
        entry = next(iter(self.store.docs["DRS-KODATHI"]["entries"].values()))
        self.assertEqual(entry["consignment_ids"], ["C1", "C2"])

    def test_verified_cache_beats_memo_api_pin(self):
        self._geocode(LIKHITHA_1, "C1")
        verified = {"latitude": 1.0, "longitude": 2.0, "types": []}
        (r, _, _, outcome), calls = self._geocode(LIKHITHA_2, "C2", cache=(verified, "exact_hit"))
        self.assertEqual((outcome, calls, r["latitude"]), ("exact_hit", 0, 1.0))

    def test_executive_correction_today_beats_verified_cache(self):
        drs_memo.remember("DRS-KODATHI", info(LIKHITHA_1), {"latitude": 9.0, "longitude": 9.0},
                          drs_memo.SOURCE_EXEC_CORRECTED, consignment_id="C1")
        verified = {"latitude": 1.0, "longitude": 2.0, "types": []}
        (r, _, _, outcome), _ = self._geocode(LIKHITHA_2, "C2", cache=(verified, "exact_hit"))
        self.assertEqual((outcome, r["latitude"]), ("drs_hit", 9.0))

    def test_api_pin_never_overwrites_executive_correction(self):
        i = info(LIKHITHA_1)
        drs_memo.remember("DRS-KODATHI", i, {"latitude": 9.0, "longitude": 9.0},
                          drs_memo.SOURCE_EXEC_CORRECTED)
        drs_memo.remember("DRS-KODATHI", i, {"latitude": 1.0, "longitude": 1.0},
                          drs_memo.SOURCE_API, consignment_id="C9")
        entry = self.store.docs["DRS-KODATHI"]["entries"][i["entry_id"]]
        self.assertEqual((entry["latitude"], entry["source"]), (9.0, "exec_corrected"))
        self.assertEqual(entry["consignment_ids"], ["C9"])

    def test_memo_is_per_drs(self):
        self._geocode(LIKHITHA_1, "C1", drs="DRS-A")
        _, calls = self._geocode(LIKHITHA_2, "C2", drs="DRS-B")
        self.assertEqual(calls, 1)

    def test_no_drs_behaves_as_before(self):
        with patch.object(save_mod, "get_cached_geocode_with_outcome", return_value=(None, "miss")), \
             patch.object(save_mod, "places_search_address", side_effect=fake_places) as places:
            out = save_mod._geocode(LIKHITHA_OCR, lookup_address=LIKHITHA_OCR)
        self.assertEqual((out[3], places.call_count, self.store.reads), ("miss", 1, 0))

    def test_memo_failure_falls_back_to_places(self):
        with patch.object(drs_memo, "load_entries", side_effect=RuntimeError("down")), \
             patch.object(save_mod, "get_cached_geocode_with_outcome", return_value=(None, "miss")), \
             patch.object(save_mod, "places_search_address", side_effect=fake_places) as places:
            out = save_mod._geocode(LIKHITHA_OCR, lookup_address=LIKHITHA_OCR,
                                    drs_id="D", match_info=info(LIKHITHA_1))
        self.assertEqual((out[0]["latitude"], places.call_count), (12.8801, 1))

    def test_new_spelling_gets_grouped_cache_entry(self):
        variant = dict(KUMMARY_3, address="Kodathi, " + KUMMARY_OCR)
        self._geocode(KUMMARY_1, "C1")
        save_mod.save_to_cache.reset_mock()
        self._geocode(variant, "C2")
        kwargs = save_mod.save_to_cache.call_args.kwargs
        self.assertEqual(kwargs["source"], "drs_memo")
        self.assertTrue(kwargs["drs_memo_group"].startswith("DRS-KODATHI:"))


class PipelineScenarioTests(unittest.TestCase):
    """The five real parcels saved in scan order through the full pipeline."""

    def test_five_parcels_two_places_calls(self):
        store = FakeMemoStore()
        docs = [_Doc("L1", LIKHITHA_1), _Doc("K1", KUMMARY_1), _Doc("L2", LIKHITHA_2),
                _Doc("K2", KUMMARY_2), _Doc("K3", KUMMARY_3)]
        with patch.object(drs_memo, "_doc", side_effect=store.doc), \
             patch.object(save_mod, "get_consignments_by_id", return_value=(docs, [])), \
             patch.object(save_mod, "get_cached_geocode_with_outcome", return_value=(None, "miss")), \
             patch.object(save_mod, "places_search_address", side_effect=fake_places) as places, \
             patch.object(save_mod, "save_to_cache"), \
             patch.object(save_mod, "merge_routing_rows") as merge, \
             patch.object(save_mod, "fetch_rows_by_consignment_ids", return_value=[]), \
             patch.object(save_mod, "upsert_consignments_routing"), \
             patch.object(save_mod, "write_drs_cache_metrics"):
            result = save_mod.save_consignments_pipeline([d.id for d in docs])

        self.assertEqual(places.call_count, 2)
        rows = {r["consignmentId"]: r for r in merge.call_args.args[0]}
        pin = lambda c: (rows[c]["latitude"], rows[c]["longitude"])
        self.assertEqual(pin("L1"), pin("L2"))
        self.assertEqual(pin("K1"), pin("K2"))
        self.assertEqual(pin("K1"), pin("K3"))
        self.assertNotEqual(pin("L1"), pin("K1"))
        m = result["cache_metrics"][0]
        self.assertEqual((m["drsHits"], m["misses"], m["apiCalls"]), (3, 2, 2))

    def test_confirmed_correction_propagates_to_siblings(self):
        store = FakeMemoStore()
        docs = [_Doc("L1", LIKHITHA_1), _Doc("L2", LIKHITHA_2)]
        conf = {"L1": ConfirmedLocation(latitude=12.5, longitude=77.5,
                                        corrected=True, overriddenBy="EXEC1")}
        with patch.object(drs_memo, "_doc", side_effect=store.doc), \
             patch.object(save_mod, "get_consignments_by_id", return_value=(docs, [])), \
             patch.object(save_mod, "get_cached_geocode_with_outcome",
                          return_value=({"latitude": 1.0, "longitude": 1.0, "types": []}, "exact_hit")), \
             patch.object(save_mod, "places_search_address", side_effect=fake_places) as places, \
             patch.object(save_mod, "save_to_cache"), \
             patch.object(save_mod, "merge_routing_rows") as merge, \
             patch.object(save_mod, "fetch_rows_by_consignment_ids", return_value=[]), \
             patch.object(save_mod, "upsert_consignments_routing"), \
             patch.object(save_mod, "write_drs_cache_metrics"):
            save_mod.save_consignments_pipeline(["L1", "L2"], confirmed_locations=conf)
        rows = {r["consignmentId"]: r for r in merge.call_args.args[0]}
        self.assertEqual(rows["L2"]["latitude"], 12.5)
        self.assertEqual(places.call_count, 0)


class PreviewContextTests(unittest.TestCase):
    def test_consignment_id_supplies_phone_and_drs(self):
        with patch.object(save_mod, "get_consignments_by_id",
                          return_value=([_Doc("L1", LIKHITHA_1)], [])):
            drs, i = save_mod._preview_match_context(LIKHITHA_OCR, None, "L1")
        self.assertEqual((drs, i["phones"]), ("DRS-KODATHI", ["8792767027"]))

    def test_drs_only_uses_scanned_text(self):
        drs, i = save_mod._preview_match_context(KUMMARY_HAND, "D9", None)
        self.assertEqual((drs, i["match_key"]), ("D9", match_info_from_address(KUMMARY_HAND)["match_key"]))

    def test_neither_skips_memo(self):
        self.assertEqual(save_mod._preview_match_context(KUMMARY_HAND, None, None), (None, None))


if __name__ == "__main__":
    unittest.main()
