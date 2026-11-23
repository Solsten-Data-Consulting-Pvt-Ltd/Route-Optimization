"""Routing run log (Firestore `routing_runs`): one record per optimize."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  env vars + offline stubs

from app.db import routing_runs
from app.services import road_matrix, sorting
from tests.test_road_distance import HUB, Patched, STOPS


class BuildRoutingRunTests(unittest.TestCase):
    def test_document_has_the_cost_and_cache_numbers(self):
        now = datetime(2026, 11, 20, 3, 54, tzinfo=timezone.utc)
        doc = routing_runs.build_routing_run(
            "DHRD10239",
            {"optimized_count": 19, "planned_from": "hub", "distance_source": "road"},
            {"mode": "full", "points": 20, "road_pairs": 380, "estimated_pairs": 0,
             "from_cache": 0, "from_api": 380, "api_elements": 380, "api_errors": 0},
            now=now,
        )
        self.assertEqual(doc["drs_no"], "DHRD10239")
        self.assertEqual(doc["pairs_needed"], 380)
        self.assertEqual((doc["from_api"], doc["from_cache"]), (380, 0))
        self.assertEqual(doc["distance_source"], "road")
        self.assertEqual((doc["expire_at"] - now).days, 90)

    def test_straight_line_runs_are_recorded_too(self):
        doc = routing_runs.build_routing_run("D1", {"optimized_count": 3}, {})
        self.assertEqual(doc["distance_source"], "straight_line")
        self.assertEqual(doc["pairs_needed"], 0)
        self.assertNotIn("from_api", doc)


class RecordRoutingRunTests(unittest.TestCase):
    def test_writes_one_document(self):
        client = mock.MagicMock()
        with mock.patch("app.db.clients.get_fs_client", return_value=client):
            self.assertTrue(routing_runs.record_routing_run("D1", {"optimized_count": 2}, {"points": 3}))
        client.collection.assert_called_once_with("routing_runs")
        doc = client.collection.return_value.add.call_args[0][0]
        self.assertEqual((doc["drs_no"], doc["pairs_needed"]), ("D1", 6))

    def test_a_failed_write_never_raises(self):
        with mock.patch("app.db.clients.get_fs_client", side_effect=RuntimeError("firestore down")):
            self.assertFalse(routing_runs.record_routing_run("D1", {}, {}))


class PipelineRecordsEachRun(unittest.TestCase):
    def test_sort_pipeline_records_the_run(self):
        row = SimpleNamespace(consignmentId="A")
        with mock.patch.object(sorting, "get_drs_starting_point", return_value={}), \
             mock.patch.object(sorting, "deactivate_duplicate_routing_rows"), \
             mock.patch.object(sorting, "fetch_active_rows_for_drs", return_value=[row]), \
             mock.patch.object(sorting, "_road_distance_enabled", return_value=False), \
             mock.patch.object(sorting, "_restart_point_for", return_value=None), \
             mock.patch.object(sorting, "compute_groups_and_sequence", return_value=[]), \
             mock.patch.object(sorting, "record_routing_run") as record:
            out = sorting.run_sorting_pipeline("DHRD10240")
        record.assert_called_once()
        self.assertEqual(record.call_args[0][0], "DHRD10240")
        self.assertIs(record.call_args[0][1], out)

    def test_road_matrix_reports_how_many_points(self):
        with Patched():
            _, stats = road_matrix.build_distance_matrix([HUB, STOPS["S10"], STOPS["S11"]])
        self.assertEqual(stats["points"], 3)


if __name__ == "__main__":
    unittest.main()
