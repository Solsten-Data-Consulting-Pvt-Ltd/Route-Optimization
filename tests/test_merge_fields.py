"""The two new routing columns reach BigQuery and the save-owned Firestore doc only."""

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

import unittest
from types import SimpleNamespace

from app.db.bigquery import MERGE_SQL, _row_to_struct_param
from app.db.firestore import _base_routing_doc, _save_routing_doc
from app.services.save import build_row


def _row(**kw):
    return build_row("D1", "DRS1", 7, "C1", "12 MG Road", "Asha", "Asha, 12 MG Road",
                     12.97, 77.59, "Ashok Nagar", "MG Road", "tdr1y7", "560001", None, False,
                     **kw)


class MergeFieldTests(unittest.TestCase):
    def test_struct_param_includes_both(self):
        struct = _row_to_struct_param(_row(geocode_resolution="MULTI_CANDIDATE",
                                           geocode_source="places"))
        values = struct.struct_values
        self.assertEqual(values["geocode_resolution"], "MULTI_CANDIDATE")
        self.assertEqual(values["geocode_source"], "places")
        self.assertEqual(struct.struct_types["geocode_resolution"], "STRING")

    def test_struct_param_tolerates_rows_without_the_keys(self):
        row = _row()
        row.pop("geocode_resolution"), row.pop("geocode_source")
        self.assertIsNone(_row_to_struct_param(row).struct_values["geocode_source"])

    def test_merge_updates_and_inserts_both(self):
        for col in ("geocode_resolution", "geocode_source"):
            self.assertIn(f"T.{col} = S.{col}", MERGE_SQL)
            self.assertIn(f"S.{col}", MERGE_SQL.split("VALUES")[1])
            self.assertIn(col, MERGE_SQL.split("INSERT (")[1].split(")")[0])

    def test_save_doc_writes_them_and_sort_doc_does_not(self):
        bq_row = SimpleNamespace(**{k: None for k in (
            "sorting_id drsNo drsId driverNumericId consignmentId receiverAddress receiverName "
            "starting_address starting_latitude starting_longitude locality area "
            "geohash_locality_loc geohash_building_loc geohash_exact_loc pincode geohash_group_id "
            "planned_inside_cluster_sequence planned_sequence_order actual_inside_cluster_sequence "
            "actual_sequence_order is_commercial is_active exception_flag latitude longitude").split()},
            geocode_resolution="PINCODE_MISMATCH", geocode_source="preview")
        saved = _save_routing_doc(bq_row)
        self.assertEqual((saved["geocode_resolution"], saved["geocode_source"]),
                         ("PINCODE_MISMATCH", "preview"))
        base = _base_routing_doc(bq_row)
        self.assertNotIn("geocode_resolution", base)
        self.assertNotIn("geocode_source", base)

    def test_save_doc_safe_on_rows_before_the_columns_exist(self):
        bq_row = SimpleNamespace(**{k: None for k in (
            "sorting_id drsNo drsId driverNumericId consignmentId receiverAddress receiverName "
            "starting_address starting_latitude starting_longitude locality area "
            "geohash_locality_loc geohash_building_loc geohash_exact_loc pincode geohash_group_id "
            "planned_inside_cluster_sequence planned_sequence_order actual_inside_cluster_sequence "
            "actual_sequence_order is_commercial is_active exception_flag latitude longitude").split()})
        self.assertIsNone(_save_routing_doc(bq_row)["geocode_resolution"])


if __name__ == "__main__":
    unittest.main()
