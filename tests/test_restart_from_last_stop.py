"""Re-optimize after the driver has started plans from his last completed stop.

DHRD10238 (18 Nov): a mid-route re-optimize planned the remaining stops from
the hub, so stops near the hub (KUR4001452422, CJB524699341) jumped to #7/#8,
ahead of #9 where the driver was already standing; he had to skip them.
"""

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from app.services import sorting

HUB = {"latitude": 12.8864, "longitude": 77.6382, "starting_address": "Hub"}
T0 = datetime(2026, 11, 18, 7, 0, tzinfo=timezone.utc)


def row(cid, lat, lon, status="In_Transit", slot=0):
    return SimpleNamespace(
        sorting_id=f"s-{cid}", consignmentId=cid, status=status,
        latitude=lat, longitude=lon, geohash_exact_loc=f"g-{cid}",
        geohash_locality_loc="loc", actual_sequence_order=slot,
    )


def stops():
    # West (near the hub) to east along one line; the driver has already
    # finished the two eastern-most-but-one stops.
    return [
        row("DONE1", 12.890, 77.690, "Delivered", slot=1),
        row("DONE2", 12.890, 77.700, "Delivered", slot=2),
        row("WEST", 12.890, 77.660),   # near the hub, behind the driver
        row("EAST1", 12.890, 77.705),  # just ahead of the driver
        row("EAST2", 12.890, 77.715),
    ]


def order(updates):
    """Remaining (not finished) stops in their new order. Finished stops are
    not renumbered, so they are not in `updates`."""
    return [u["sorting_id"][2:] for u in sorted(updates, key=lambda u: u["planned_sequence_order"])]


class PickRestartPoint(unittest.TestCase):
    def test_latest_completed_stop_wins(self):
        times = {"DONE1": T0, "DONE2": T0 + timedelta(minutes=20)}
        point = sorting.pick_restart_point(stops(), times)
        self.assertEqual(point["consignmentId"], "DONE2")
        self.assertEqual((point["latitude"], point["longitude"]), (12.890, 77.700))

    def test_not_delivered_counts_as_completed(self):
        rows = stops() + [row("ND", 12.891, 77.71, "Not_Delivered", slot=3)]
        times = {"DONE1": T0, "DONE2": T0, "ND": T0 + timedelta(minutes=5)}
        self.assertEqual(sorting.pick_restart_point(rows, times)["consignmentId"], "ND")

    def test_skipped_and_pending_never_count(self):
        rows = [row("SK", 12.9, 77.7, "Skipped"), row("P", 12.9, 77.71)]
        self.assertIsNone(sorting.pick_restart_point(rows, {"SK": T0, "P": T0}))

    def test_no_time_or_no_pin_means_hub(self):
        rows = stops()
        self.assertIsNone(sorting.pick_restart_point(rows, {}))
        rows[0].latitude = None
        self.assertIsNone(sorting.pick_restart_point(rows, {"DONE1": T0}))


class PlansFromLastCompletedStop(unittest.TestCase):
    def test_hub_start_puts_the_stop_behind_the_driver_first(self):
        # Today's behaviour, kept when nothing is finished: from the hub the
        # western stop is first.
        self.assertEqual(order(sorting.compute_groups_and_sequence(stops(), "D1", HUB))[0], "WEST")

    def test_restart_point_puts_the_nearest_stop_ahead_first(self):
        restart = {"latitude": 12.890, "longitude": 77.700, "consignmentId": "DONE2"}
        updates = sorting.compute_groups_and_sequence(stops(), "D1", HUB, restart_point=restart)
        self.assertEqual(order(updates)[0], "EAST1")
        # Finished stops keep slots 1 and 2; the rest follow from 3.
        self.assertEqual(sorted(u["planned_sequence_order"] for u in updates), [3, 4, 5])

    def test_stored_starting_point_stays_the_hub(self):
        restart = {"latitude": 12.890, "longitude": 77.700, "consignmentId": "DONE2"}
        updates = sorting.compute_groups_and_sequence(stops(), "D1", HUB, restart_point=restart)
        self.assertTrue(all(u["starting_latitude"] == HUB["latitude"] for u in updates))
        self.assertTrue(all(u["starting_address"] == "Hub" for u in updates))


class PipelineReadsCompletionTimes(unittest.TestCase):
    def _run(self, times=None, times_error=None):
        written = {}
        with mock.patch.object(sorting, "get_drs_starting_point", return_value=HUB), \
             mock.patch.object(sorting, "deactivate_duplicate_routing_rows"), \
             mock.patch.object(sorting, "fetch_active_rows_for_drs", return_value=stops()), \
             mock.patch.object(sorting, "fetch_assigned_rows_for_drs", return_value=[]), \
             mock.patch.object(sorting, "upsert_routing_from_sorting"), \
             mock.patch.object(sorting, "write_group_assignments",
                               side_effect=lambda u: written.setdefault("u", u)), \
             mock.patch.object(sorting, "get_completion_times",
                               side_effect=times_error, return_value=times) as get_times:
            out = sorting.run_sorting_pipeline("D1")
        return out, written.get("u", []), get_times

    def test_uses_the_last_completed_stop(self):
        out, updates, get_times = self._run(times={"DONE1": T0, "DONE2": T0 + timedelta(minutes=9)})
        self.assertEqual(sorted(get_times.call_args[0][0]), ["DONE1", "DONE2"])
        self.assertEqual(out["planned_from"], "last_completed:DONE2")
        self.assertEqual(order(updates)[0], "EAST1")

    def test_firestore_failure_falls_back_to_the_hub(self):
        out, updates, _ = self._run(times_error=RuntimeError("firestore down"))
        self.assertEqual(out["planned_from"], "hub")
        self.assertEqual(order(updates)[0], "WEST")

    def test_nothing_finished_does_not_read_firestore(self):
        rows = [row("A", 12.89, 77.66), row("B", 12.89, 77.70)]
        with mock.patch.object(sorting, "get_completion_times") as get_times:
            self.assertIsNone(sorting._restart_point_for(rows, "D1"))
        get_times.assert_not_called()


if __name__ == "__main__":
    unittest.main()
