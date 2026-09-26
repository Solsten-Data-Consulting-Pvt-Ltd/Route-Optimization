"""Tests for POST /geocode/preview and the confirmedLocations save bypass.

No Google, BigQuery or Firestore access: every external call is patched.
"""

import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-key")
os.environ.setdefault("BQ_PROJECT", "test-project")
os.environ.setdefault("BQ_DATASET", "test_dataset")
os.environ.setdefault("BQ_TABLE", "test_table")
os.environ.setdefault("BQ_STRUCTURED_TABLE", "test_structured_table")
os.environ.setdefault("FIRESTORE_PROJECT", "test-project")

from pydantic import ValidationError

from app.db import bigquery as bq_mod
from app.db.bigquery import MERGE_SQL, _row_to_struct_param
from app.db.firestore import _base_routing_doc, _save_routing_doc
from app.schemas import ConfirmedLocation, SaveConsignmentsRequest
from app.services import save as save_mod

PLACES_RESULT = {
    "latitude": 12.97, "longitude": 77.59,
    "formatted_address": "MG Road, Bengaluru 560001",
    "locality": "Ashok Nagar", "area": "MG Road", "pincode": "560001",
    "place_id": "abc123", "types": [],
}


class _Doc:
    def __init__(self, cid):
        self.id = cid
        self._data = {
            "consignmentId": cid, "drsNo": "D1", "drsId": "DRS1",
            "driverNumericId": 7,
            "receiver": {"name": "Asha", "address": "12 MG Road,\nBengaluru 560001"},
        }

    def to_dict(self):
        return self._data


class PreviewGeocodeTests(unittest.TestCase):
    def test_success_returns_point_and_normalises_like_save(self):
        with patch.object(save_mod, "_geocode",
                          return_value=(PLACES_RESULT, None, None, "miss")) as g:
            out = save_mod.preview_geocode("Asha", "12 MG Road,\nBengaluru 560001")
        self.assertEqual(out["status"], "success")
        self.assertEqual((out["latitude"], out["longitude"]), (12.97, 77.59))
        self.assertEqual(out["formatted_address"], "MG Road, Bengaluru 560001")
        self.assertIsNone(out["exception_flag"])
        # Same normalisation the save pipeline applies -> same cache key.
        expected_lookup = save_mod.normalize_to_single_line("12 MG Road,\nBengaluru 560001")
        self.assertEqual(g.call_args.kwargs["lookup_address"], expected_lookup)

    def test_unresolvable_address_fails_without_raising(self):
        with patch.object(save_mod, "_geocode",
                          return_value=(None, "Address could not be found.", "ZERO", "miss")):
            out = save_mod.preview_geocode(None, "nowhere at all")
        self.assertEqual(out, {"status": "failed", "error": "Address could not be found."})

    def test_blank_address_fails_without_geocoding(self):
        with patch.object(save_mod, "_geocode") as g:
            out = save_mod.preview_geocode("Asha", "   ")
        self.assertEqual(out["status"], "failed")
        g.assert_not_called()

    def test_preview_never_writes_routing_stores(self):
        with patch.object(save_mod, "_geocode",
                          return_value=(PLACES_RESULT, None, None, "miss")), \
             patch.object(save_mod, "merge_routing_rows") as merge, \
             patch.object(save_mod, "upsert_consignments_routing") as upsert:
            save_mod.preview_geocode("Asha", "12 MG Road")
        merge.assert_not_called()
        upsert.assert_not_called()


class SaveBypassTests(unittest.TestCase):
    def _run(self, confirmed_locations=None, ids=("C1",)):
        with patch.object(save_mod, "get_consignments_by_id",
                          return_value=([_Doc(c) for c in ids], [])), \
             patch.object(save_mod, "_geocode",
                          return_value=(PLACES_RESULT, None, None, "miss")) as g, \
             patch.object(save_mod, "merge_routing_rows") as merge, \
             patch.object(save_mod, "fetch_rows_by_consignment_drs_pairs", return_value=[]), \
             patch.object(save_mod, "deactivate_stale_routing_rows"), \
             patch.object(save_mod, "upsert_consignments_routing"), \
             patch.object(save_mod, "write_drs_cache_metrics"), \
             patch.object(save_mod.drs_memo, "remember"):
            result = save_mod.save_consignments_pipeline(
                list(ids), confirmed_locations=confirmed_locations)
        rows = {r["consignmentId"]: r for r in merge.call_args.args[0]}
        return result, rows, g

    def test_no_confirmed_locations_behaves_as_before(self):
        result, rows, g = self._run()
        g.assert_called_once()
        row = rows["C1"]
        self.assertEqual((row["latitude"], row["longitude"]), (12.97, 77.59))
        self.assertEqual(row["locality"], "Ashok Nagar")
        self.assertFalse(row["locationOverridden"])
        self.assertIsNone(row["overriddenBy"])
        self.assertIsNone(row["overriddenAt"])
        m = result["cache_metrics"][0]
        self.assertEqual((m["cacheLookups"], m["misses"], m["apiCalls"]), (1, 1, 1))

    def test_bypass_corrected_true_skips_geocode_and_sets_tracking(self):
        conf = {"C1": ConfirmedLocation(latitude=13.0, longitude=77.7,
                                        formatted_address="Pin dropped",
                                        corrected=True, overriddenBy="EXEC42")}
        result, rows, g = self._run(conf)
        g.assert_not_called()
        row = rows["C1"]
        self.assertEqual((row["latitude"], row["longitude"]), (13.0, 77.7))
        self.assertEqual(row["formatted_address"], "Pin dropped")
        self.assertTrue(row["locationOverridden"])
        self.assertEqual(row["overriddenBy"], "EXEC42")
        self.assertIsInstance(row["overriddenAt"], datetime)
        self.assertIsNotNone(row["overriddenAt"].tzinfo)
        self.assertEqual(row["geohash_exact_loc"][:4], save_mod.pgh.encode(13.0, 77.7, precision=8)[:4])
        # No cache lookup / API call happened, so none is counted.
        m = result["cache_metrics"][0]
        self.assertEqual((m["cacheLookups"], m["apiCalls"], m["cacheErrors"]), (0, 0, 0))
        self.assertEqual(m["consignmentsProcessed"], 1)

    def test_bypass_corrected_false_keeps_tracking_null(self):
        conf = {"C1": ConfirmedLocation(latitude=13.0, longitude=77.7)}
        _, rows, g = self._run(conf)
        g.assert_not_called()
        row = rows["C1"]
        self.assertFalse(row["locationOverridden"])
        self.assertIsNone(row["overriddenBy"])
        self.assertIsNone(row["overriddenAt"])

    def test_confirmed_row_without_metadata_degrades_gracefully(self):
        conf = {"C1": ConfirmedLocation(latitude=13.0, longitude=77.7)}
        result, rows, _ = self._run(conf)
        row = rows["C1"]
        self.assertEqual(result["saved"], 1)
        self.assertEqual((row["locality"], row["area"]), ("UNKNOWN", "UNKNOWN"))
        self.assertIsNone(row["pincode"])
        self.assertIsNone(row["place_id"])
        self.assertEqual(row["geocode_status"], "success")

    def test_mixed_batch_only_bypasses_confirmed_ids(self):
        conf = {"C2": ConfirmedLocation(latitude=13.0, longitude=77.7)}
        _, rows, g = self._run(conf, ids=("C1", "C2"))
        self.assertEqual(g.call_count, 1)
        self.assertEqual(rows["C1"]["latitude"], 12.97)
        self.assertEqual(rows["C2"]["latitude"], 13.0)


class SchemaTests(unittest.TestCase):
    def test_corrected_requires_overridden_by(self):
        with self.assertRaises(ValidationError):
            ConfirmedLocation(latitude=1, longitude=1, corrected=True)

    def test_rejects_out_of_range_coordinates(self):
        with self.assertRaises(ValidationError):
            ConfirmedLocation(latitude=91, longitude=1)

    def test_request_parses_confirmed_locations_map(self):
        req = SaveConsignmentsRequest(consignmentIds=["C1"], confirmedLocations={
            "C1": {"latitude": 1.5, "longitude": 2.5, "corrected": True, "overriddenBy": "E1"}})
        self.assertIsInstance(req.confirmedLocations["C1"], ConfirmedLocation)


class PersistenceTests(unittest.TestCase):
    def test_bigquery_struct_and_merge_carry_new_fields(self):
        row = save_mod.build_row("D1", "DRS1", 7, "C1", "a", "n", "n, a", 1.0, 2.0,
                                 None, None, "tdr1wxyz", None, None, False,
                                 location_overridden=True, overridden_by="E1",
                                 overridden_at=datetime(2026, 9, 23))
        values = _row_to_struct_param(row).struct_values
        self.assertIs(values["locationOverridden"], True)
        self.assertEqual(values["overriddenBy"], "E1")
        self.assertEqual(values["overriddenAt"], datetime(2026, 9, 23))
        for col in ("locationOverridden", "overriddenBy", "overriddenAt"):
            self.assertIn(f"T.{col} = S.{col}", MERGE_SQL)
            self.assertIn(f"S.{col}", MERGE_SQL.split("VALUES", 1)[1])

    def test_save_doc_tolerates_rows_without_new_columns(self):
        # _save_routing_doc (post-save Firestore sync) is the sole owner of
        # the location/override fields — see
        # test_sort_doc_never_writes_location_or_override_fields below for
        # why the sort pipeline must never see these keys at all.
        fields = ["sorting_id", "drsNo", "drsId", "driverNumericId", "consignmentId",
                  "receiverAddress", "receiverName", "starting_address", "starting_latitude",
                  "starting_longitude", "latitude", "longitude", "locality", "area",
                  "geohash_locality_loc", "geohash_building_loc", "geohash_exact_loc",
                  "pincode", "geohash_group_id", "planned_inside_cluster_sequence",
                  "planned_sequence_order", "actual_inside_cluster_sequence",
                  "actual_sequence_order", "is_commercial", "is_active", "exception_flag"]
        old_row = SimpleNamespace(**{f: None for f in fields})
        doc = _save_routing_doc(old_row)
        self.assertEqual((doc["locationOverridden"], doc["overriddenBy"], doc["overriddenAt"]),
                         (False, None, None))
        new_row = SimpleNamespace(**{f: None for f in fields}, locationOverridden=True,
                                  overriddenBy="E1", overriddenAt=datetime(2026, 9, 23, 5, 0))
        doc = _save_routing_doc(new_row)
        self.assertEqual(doc["overriddenAt"], "2026-09-23T05:00:00")

    def test_sort_doc_never_writes_location_or_override_fields(self):
        # Regression test: run_sorting_pipeline (Optimize Route) re-syncs
        # Firestore from whatever BigQuery currently holds for the DRS, with
        # no idea whether a manual pin correction landed more recently than
        # that BigQuery row. Previously _base_routing_doc (which
        # upsert_routing_from_sorting/the sort pipeline writes with
        # merge=True) always included latitude/longitude/locationOverridden/
        # overriddenBy/overriddenAt/plannedLatitude/plannedLongitude, so
        # every Optimize Route run silently overwrote — and could revert —
        # a correction the save pipeline had already applied. These fields
        # must never appear in the sort pipeline's doc at all: a merge write
        # only ever touches the keys present in it.
        fields = ["sorting_id", "drsNo", "drsId", "driverNumericId", "consignmentId",
                  "receiverAddress", "receiverName", "starting_address", "starting_latitude",
                  "starting_longitude", "latitude", "longitude", "locality", "area",
                  "geohash_locality_loc", "geohash_building_loc", "geohash_exact_loc",
                  "pincode", "geohash_group_id", "planned_inside_cluster_sequence",
                  "planned_sequence_order", "actual_inside_cluster_sequence",
                  "actual_sequence_order", "is_commercial", "is_active", "exception_flag",
                  "planned_latitude", "planned_longitude", "locationOverridden",
                  "overriddenBy", "overriddenAt"]
        row = SimpleNamespace(**{f: None for f in fields})
        row.latitude = 12.899810224435797
        row.longitude = 77.71012834805742
        row.locationOverridden = True
        row.overriddenBy = "E1"
        row.overriddenAt = datetime(2026, 9, 23, 5, 0)
        row.planned_latitude = 12.8825751
        row.planned_longitude = 77.6040827

        doc = _base_routing_doc(row)

        for forbidden in ("latitude", "longitude", "locationOverridden", "overriddenBy",
                          "overriddenAt", "plannedLatitude", "plannedLongitude"):
            self.assertNotIn(
                forbidden, doc,
                f"_base_routing_doc must not write {forbidden!r} — the sort "
                "pipeline has no idea whether a save-pipeline correction is "
                "more recent than the BigQuery row it read, so writing this "
                "key (even via a merge write) can silently revert one.",
            )


class ScopedFetchAndCleanupTests(unittest.TestCase):
    """Regression coverage for the cross-DRS row-collision bug: a
    consignmentId reassigned across DRSs (release-and-redeliver, or repeated
    test reassignment) accumulates one BigQuery row per DRS it has ever been
    on, because the MERGE keys on (consignmentId, drsNo). The old
    fetch_rows_by_consignment_ids read back EVERY row for a consignmentId
    regardless of drsNo, and since Firestore holds only one document per
    consignmentId, whichever row an unordered SELECT * happened to return
    last would silently overwrite it — including a stale, unrelated DRS's
    latitude/longitude/plannedLatitude. These tests pin down the fix:
    fetch_rows_by_consignment_drs_pairs scopes to exactly the pairs given,
    deactivate_stale_routing_rows only ever targets a DIFFERENT drsNo for
    the same consignmentId, and the save pipeline passes only the
    current save's own pairs to both."""

    def test_fetch_scopes_query_to_given_pairs_and_dedupes(self):
        with patch.object(bq_mod, "get_bq_client") as get_client:
            fake_client = get_client.return_value
            fake_client.query.return_value.result.return_value = ["ROW"]
            result = bq_mod.fetch_rows_by_consignment_drs_pairs(
                [("C1", "DRS_B"), ("C1", "DRS_B")]  # duplicate on purpose
            )

        self.assertEqual(result, ["ROW"])
        query_text = fake_client.query.call_args.args[0]
        self.assertIn("EXISTS", query_text)
        self.assertIn("p.consignmentId = T.consignmentId AND p.drsNo = T.drsNo", query_text)
        job_config = fake_client.query.call_args.kwargs["job_config"]
        pairs_param = job_config.query_parameters[0]
        self.assertEqual(pairs_param.array_type, "STRUCT")
        self.assertEqual(len(pairs_param.values), 1, "duplicate pair must be deduped")

    def test_fetch_empty_pairs_returns_empty_without_querying_bigquery(self):
        with patch.object(bq_mod, "get_bq_client") as get_client:
            result = bq_mod.fetch_rows_by_consignment_drs_pairs([])
        self.assertEqual(result, [])
        get_client.assert_not_called()

    def test_deactivate_targets_only_a_different_drs_for_the_same_consignment(self):
        with patch.object(bq_mod, "get_bq_client") as get_client:
            fake_client = get_client.return_value
            bq_mod.deactivate_stale_routing_rows([("C1", "DRS_B")])

        query_text = fake_client.query.call_args.args[0]
        self.assertIn("SET is_active = FALSE", query_text)
        self.assertIn("p.consignmentId = T.consignmentId AND p.drsNo != T.drsNo", query_text)
        fake_client.query.return_value.result.assert_called_once()

    def test_deactivate_empty_pairs_is_a_noop(self):
        with patch.object(bq_mod, "get_bq_client") as get_client:
            bq_mod.deactivate_stale_routing_rows([])
        get_client.assert_not_called()

    def test_save_pipeline_scopes_readback_and_cleanup_to_the_drs_just_saved(self):
        """End-to-end: even if BigQuery holds rows for this consignmentId on
        several DRSs, the save pipeline must only ever pass the DRS it just
        wrote to (from the consignment doc's own drsNo, "D1" per _Doc above)
        to both the read-back and the cleanup — never the whole history."""
        conf = {"C1": ConfirmedLocation(latitude=13.0, longitude=77.7,
                                        corrected=True, overriddenBy="EXEC1")}
        with patch.object(save_mod, "get_consignments_by_id",
                          return_value=([_Doc("C1")], [])), \
             patch.object(save_mod, "merge_routing_rows"), \
             patch.object(save_mod, "fetch_rows_by_consignment_drs_pairs",
                          return_value=[]) as fetch, \
             patch.object(save_mod, "deactivate_stale_routing_rows") as deactivate, \
             patch.object(save_mod, "upsert_consignments_routing"), \
             patch.object(save_mod, "write_drs_cache_metrics"):
            save_mod.save_consignments_pipeline(["C1"], confirmed_locations=conf)

        fetch.assert_called_once_with([("C1", "D1")])
        deactivate.assert_called_once_with([("C1", "D1")])


class RouterTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers import geocode, save as save_router
        app = FastAPI()
        app.include_router(geocode.router)
        app.include_router(save_router.router)
        self.client = TestClient(app)

    def test_preview_endpoint_failure_is_200_not_500(self):
        with patch("app.routers.geocode.preview_geocode", side_effect=RuntimeError("boom")):
            r = self.client.post("/geocode/preview", json={"receiverAddress": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "failed", "error": "boom"})

    def test_save_endpoint_passes_confirmed_locations_through(self):
        with patch("app.routers.save.save_consignments_pipeline",
                   return_value={"failed": 0}) as p:
            r = self.client.post("/save-consignments", json={
                "consignmentIds": ["C1"],
                "confirmedLocations": {"C1": {"latitude": 1, "longitude": 2}}})
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(p.call_args.kwargs["confirmed_locations"]["C1"], ConfirmedLocation)


if __name__ == "__main__":
    unittest.main()
