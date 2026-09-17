"""Sorting pipeline — port of the hermes-sort Cloud Function."""

import logging
from collections import defaultdict

from app.db.bigquery import (
    fetch_active_rows_for_drs,
    fetch_assigned_rows_for_drs,
    write_group_assignments,
)
from app.db.firestore import get_drs_starting_point, upsert_routing_from_sorting
from app.services.tsp import solve_route_order

logger = logging.getLogger(__name__)

DELIVERED_STATUS_CODE = "DE"


def compute_groups_and_sequence(rows, drs_no, start_meta):
    """Solves the route order with OR-Tools, then assigns:
      - geohash_group_id: rows are still clustered by locality geohash
      - planned_sequence_order: the row's position in the overall solved route
      - planned_inside_cluster_sequence: position within its own cluster

    Delivered consignments (statusCode == "DE") are not re-optimized; they hold
    on to the sequence slot they already occupy, and the pending stops are laid
    out into whatever slots remain.

    actual_* fields are initialized to match planned_* — they represent what
    actually happened on the ground and are expected to diverge over time
    (e.g. a driver skips a stop), but start out identical to the plan.
    """
    total_slots = len(rows)

    delivered_rows = [r for r in rows if getattr(r, "statusCode", None) == DELIVERED_STATUS_CODE]
    pending_rows = [r for r in rows if getattr(r, "statusCode", None) != DELIVERED_STATUS_CODE]

    occupied_slots = set()
    for r in delivered_rows:
        slot = getattr(r, "actual_sequence_order", None)
        if slot:
            occupied_slots.add(slot)
        else:
            logger.warning(
                "Delivered consignment %s has no existing sequence order; "
                "it will not hold a fixed slot.", r.consignmentId
            )

    usable_pending = [
        r for r in pending_rows
        if r.latitude is not None and r.longitude is not None and r.geohash_exact_loc
    ]
    skipped = len(pending_rows) - len(usable_pending)
    if skipped:
        logger.warning(
            "Skipping %d pending row(s) missing lat/lon/geohash for DRS %s.", skipped, drs_no
        )

    if not usable_pending:
        return []

    start_lat = start_meta.get("latitude") if start_meta else None
    start_lon = start_meta.get("longitude") if start_meta else None
    start_addr = start_meta.get("starting_address") if start_meta else "UNKNOWN"

    if not start_lat or not start_lon:
        logger.warning(
            "DRS %s has no starting coordinates in drs_starting_point. Using first stop as hub.",
            drs_no,
        )
        start_lat = usable_pending[0].latitude
        start_lon = usable_pending[0].longitude
        start_addr = start_addr or "UNKNOWN"

    ordered_pending = solve_route_order(start_lat, start_lon, usable_pending)

    # Slots not held by a delivered parcel, in ascending order.
    remaining_slots = [s for s in range(1, total_slots + 1) if s not in occupied_slots]

    if len(remaining_slots) != len(ordered_pending):
        logger.warning(
            "DRS %s: %d remaining slot(s) but %d pending row(s) to place — "
            "numbering may not land exactly on 1..%d.",
            drs_no, len(remaining_slots), len(ordered_pending), total_slots,
        )

    inside_cluster_counts = defaultdict(int)
    updates = []

    for slot, row in zip(remaining_slots, ordered_pending):
        group_id = f"{drs_no}_{row.geohash_locality_loc}"
        inside_cluster_counts[group_id] += 1
        cluster_seq = inside_cluster_counts[group_id]

        updates.append({
            "sorting_id": row.sorting_id,
            "geohash_group_id": group_id,
            "starting_address": str(start_addr or "UNKNOWN"),
            "starting_latitude": float(start_lat),
            "starting_longitude": float(start_lon),
            "planned_inside_cluster_sequence": cluster_seq,
            "planned_sequence_order": slot,
            "actual_inside_cluster_sequence": cluster_seq,
            "actual_sequence_order": slot,
        })

    return updates


def run_sorting_pipeline(drs_no):
    start_meta = get_drs_starting_point(drs_no)
    rows = fetch_active_rows_for_drs(drs_no)

    if not rows:
        return {
            "optimized_count": 0,
            "message": f"No active consignments found in BQ for DRS {drs_no}.",
        }

    updates = compute_groups_and_sequence(rows, drs_no, start_meta or {})

    if updates:
        write_group_assignments(updates)
        upsert_routing_from_sorting(fetch_assigned_rows_for_drs(drs_no))

    return {"optimized_count": len(updates)}
