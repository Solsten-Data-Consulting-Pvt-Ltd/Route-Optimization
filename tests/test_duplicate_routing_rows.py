"""Duplicate active routing rows must not inflate a DRS's stop numbers.

Overlapping saves of one consignment can each INSERT a row (BigQuery has no
unique constraint). The optimizer numbered every row as its own stop, so a DRS
with 2 parcels came out as stops 2 and 4 (DHRD10227). These tests pin the three
guards: the reads keep one row per consignment, the MERGE path deactivates
extra rows, and the sort pipeline heals a DRS before optimizing it.
"""

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

import unittest
from types import SimpleNamespace
from unittest import mock

from app.db import bigquery as bq
from app.services import sorting


class _Job:
    def result(self):
        return []


class _FakeClient:
    def __init__(self):
        self.calls = []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return _Job()


def _params(job_config):
    return {p.name: p for p in job_config.query_parameters}


class ReadsKeepOneRowPerConsignment(unittest.TestCase):
    def _sql_for(self, fn):
        client = _FakeClient()
        with mock.patch.object(bq, "get_bq_client", return_value=client):
            fn("DRS1")
        return client.calls[0][0]

    def test_active_rows_are_deduped_by_consignment_keeping_the_earliest(self):
        sql = self._sql_for(bq.fetch_active_rows_for_drs)
        self.assertIn("QUALIFY ROW_NUMBER() OVER (", sql)
        self.assertIn("PARTITION BY C.consignmentId ORDER BY C.created_at, C.sorting_id", sql)
        # QUALIFY must come after the filters and before the final ORDER BY.
        self.assertLess(sql.index("C.drsNo = @drs_no"), sql.index("QUALIFY"))
        self.assertLess(sql.index("QUALIFY"), sql.index("ORDER BY C.planned_sequence_order"))

    def test_assigned_rows_are_deduped_preferring_the_active_row(self):
        sql = self._sql_for(bq.fetch_assigned_rows_for_drs)
        self.assertIn(
            "PARTITION BY consignmentId ORDER BY is_active DESC, updated_at DESC, sorting_id",
            sql,
        )


class DeactivateDuplicates(unittest.TestCase):
    def test_pairs_scope_keeps_the_earliest_row_and_deactivates_the_rest(self):
        client = _FakeClient()
        with mock.patch.object(bq, "get_bq_client", return_value=client):
            bq.deactivate_duplicate_routing_rows(pairs=[("C1", "DRS1"), ("C1", "DRS1"), ("C2", "DRS1")])
        sql, cfg = client.calls[0]
        self.assertIn("UPDATE", sql)
        self.assertIn("SET is_active = FALSE", sql)
        self.assertIn("PARTITION BY D.consignmentId, D.drsNo", sql)
        self.assertIn("ORDER BY D.created_at, D.sorting_id", sql)  # earliest row ranks 1 and is kept
        self.assertIn("WHERE rn > 1", sql)
        self.assertEqual(list(_params(cfg)), ["pairs"])
        # the same pair listed twice is only sent once
        self.assertEqual(len(_params(cfg)["pairs"].values), 2)

    def test_drs_scope_uses_the_drs_number(self):
        client = _FakeClient()
        with mock.patch.object(bq, "get_bq_client", return_value=client):
            bq.deactivate_duplicate_routing_rows(drs_no="DHRD10227")
        sql, cfg = client.calls[0]
        self.assertIn("T.drsNo = @drs_no", sql)
        self.assertIn("D.drsNo = @drs_no", sql)
        self.assertEqual(_params(cfg)["drs_no"].value, "DHRD10227")

    def test_nothing_to_do_runs_no_query(self):
        client = _FakeClient()
        with mock.patch.object(bq, "get_bq_client", return_value=client):
            bq.deactivate_duplicate_routing_rows()
            bq.deactivate_duplicate_routing_rows(pairs=[])
            bq.deactivate_duplicate_routing_rows(pairs=[(None, "DRS1")])
        self.assertEqual(client.calls, [])


class MergeRunsTheCleanup(unittest.TestCase):
    ROWS = [{"consignmentId": "C1", "drsNo": "DRS1"}, {"consignmentId": "C2", "drsNo": "DRS1"}]

    def test_cleanup_runs_after_the_merge_for_the_merged_pairs(self):
        order = []
        client = _FakeClient()
        client.query = lambda sql, job_config=None: (order.append("merge"), _Job())[1]
        with mock.patch.object(bq, "get_bq_client", return_value=client), \
             mock.patch.object(bq, "_row_to_struct_param", return_value=object()), \
             mock.patch.object(bq.bigquery, "ArrayQueryParameter", return_value=object()), \
             mock.patch.object(bq.bigquery, "QueryJobConfig", return_value=object()), \
             mock.patch.object(bq, "deactivate_duplicate_routing_rows",
                               side_effect=lambda **kw: order.append(("cleanup", kw))) as cleanup:
            bq.merge_routing_rows(self.ROWS)
        self.assertEqual(order[0], "merge")
        self.assertEqual(order[-1][0], "cleanup")
        cleanup.assert_called_once_with(pairs=[("C1", "DRS1"), ("C2", "DRS1")])

    def test_a_failing_cleanup_does_not_fail_the_save(self):
        client = _FakeClient()
        with mock.patch.object(bq, "get_bq_client", return_value=client), \
             mock.patch.object(bq, "_row_to_struct_param", return_value=object()), \
             mock.patch.object(bq.bigquery, "ArrayQueryParameter", return_value=object()), \
             mock.patch.object(bq.bigquery, "QueryJobConfig", return_value=object()), \
             mock.patch.object(bq, "deactivate_duplicate_routing_rows", side_effect=RuntimeError("boom")):
            bq.merge_routing_rows(self.ROWS)  # must not raise


class SortPipelineHealsFirst(unittest.TestCase):
    def test_duplicates_are_deactivated_before_the_rows_are_read(self):
        order = []
        with mock.patch.object(sorting, "get_drs_starting_point", return_value={}), \
             mock.patch.object(sorting, "deactivate_duplicate_routing_rows",
                               side_effect=lambda **kw: order.append(("dedupe", kw))), \
             mock.patch.object(sorting, "fetch_active_rows_for_drs",
                               side_effect=lambda d: (order.append("fetch"), [])[1]):
            out = sorting.run_sorting_pipeline("DHRD10227")
        self.assertEqual(order, [("dedupe", {"drs_no": "DHRD10227"}), "fetch"])
        self.assertEqual(out["optimized_count"], 0)

    def test_a_failing_cleanup_does_not_stop_optimizing(self):
        with mock.patch.object(sorting, "get_drs_starting_point", return_value={}), \
             mock.patch.object(sorting, "deactivate_duplicate_routing_rows", side_effect=RuntimeError("boom")), \
             mock.patch.object(sorting, "fetch_active_rows_for_drs", return_value=[]) as fetch:
            sorting.run_sorting_pipeline("DHRD10227")
        fetch.assert_called_once_with("DHRD10227")


class StopNumbersFollowTheRealParcelCount(unittest.TestCase):
    def _row(self, cid, lat, lon):
        return SimpleNamespace(
            sorting_id=f"s-{cid}", consignmentId=cid, status="In_Transit",
            latitude=lat, longitude=lon, geohash_exact_loc=f"g-{cid}",
            geohash_locality_loc="loc", actual_sequence_order=0,
        )

    def test_two_parcels_are_numbered_1_and_2(self):
        updates = sorting.compute_groups_and_sequence(
            [self._row("A", 12.97, 77.59), self._row("B", 12.98, 77.60)], "DRS1", {}
        )
        self.assertEqual(sorted(u["planned_sequence_order"] for u in updates), [1, 2])

    def test_duplicate_rows_are_what_produced_the_gaps(self):
        # Documents the failure this change prevents: 4 rows for 2 parcels
        # consume 4 slots, so the last row of each parcel lands on 2 and 4.
        rows = [self._row(c, la, lo) for c, la, lo in
                [("A", 12.97, 77.59), ("A", 12.97, 77.59), ("B", 12.98, 77.60), ("B", 12.98, 77.60)]]
        for i, r in enumerate(rows):
            r.sorting_id = f"s{i}"
        updates = sorting.compute_groups_and_sequence(rows, "DRS1", {})
        self.assertEqual(sorted(u["planned_sequence_order"] for u in updates), [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
