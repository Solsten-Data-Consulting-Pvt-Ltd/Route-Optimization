"""Sorting pipeline — port of the hermes-sort Cloud Function."""

import logging
from collections import defaultdict

from app.db import feature_flags
from app.db.routing_runs import record_routing_run

from app.db.bigquery import (
    deactivate_duplicate_routing_rows,
    fetch_active_rows_for_drs,
    fetch_assigned_rows_for_drs,
    write_group_assignments,
)
from app.db.firestore import (
    get_completion_times,
    get_drs_starting_point,
    upsert_routing_from_sorting,
)
from app.services.tsp import solve_route_order

logger = logging.getLogger(__name__)

LOCKED_STATUSES = {"Delivered", "Not_Delivered"}  # driver already visited these — do not renumber


def _road_distance_enabled():
    try:
        return feature_flags.road_distance_routing()
    except Exception:
        logger.exception("roadDistanceRouting flag check failed; using straight lines")
        return False


def _road_matrix(start_lat, start_lon, stops, stats_out):
    """Road-distance table over [start] + stops, or None to use straight
    lines. Never raises: any failure means straight lines, as before."""
    try:
        from app.services.road_matrix import build_distance_matrix

        points = [(float(start_lat), float(start_lon))] + [
            (float(r.latitude), float(r.longitude)) for r in stops]
        matrix, stats = build_distance_matrix(points)
        stats_out.update(stats)
        stats_out["distance_source"] = "road" if stats["road_pairs"] else "straight_line"
        return matrix if stats["road_pairs"] else None
    except Exception:
        logger.exception("Road distance matrix failed; using straight lines")
        stats_out["distance_source"] = "straight_line"
        return None


def pick_restart_point(rows, completed_at):
    """Where a re-optimize after the driver has started should plan from: the
    pin of the stop he finished most recently (Delivered / Not_Delivered,
    latest completion time). None when nothing is finished yet, or no
    finished stop has both a pin and a known time - then the hub is used,
    as before. Ties on time break by consignment id (repeatable)."""
    best = None
    for r in rows:
        if getattr(r, "status", None) not in LOCKED_STATUSES:
            continue
        when = completed_at.get(r.consignmentId)
        if when is None or r.latitude is None or r.longitude is None:
            continue
        key = (when, str(r.consignmentId))
        if best is None or key > best[0]:
            best = (key, r)
    if best is None:
        return None
    r = best[1]
    return {"latitude": float(r.latitude), "longitude": float(r.longitude),
            "consignmentId": r.consignmentId}


def _restart_point_for(rows, drs_no):
    """Reads completion times for the finished stops and picks the restart
    point. Never raises: any failure means the hub, as before."""
    done_ids = [r.consignmentId for r in rows if getattr(r, "status", None) in LOCKED_STATUSES]
    if not done_ids:
        return None
    try:
        return pick_restart_point(rows, get_completion_times(done_ids))
    except Exception:
        logger.exception("Could not read completion times for DRS %s; planning from the hub", drs_no)
        return None


def compute_groups_and_sequence(rows, drs_no, start_meta, use_road=False, stats_out=None,
                                restart_point=None):
    """Solves the route order with OR-Tools, then assigns:
      - geohash_group_id: rows are still clustered by locality geohash
      - planned_sequence_order: the row's position in the overall solved route
      - planned_inside_cluster_sequence: position within its own cluster

    Consignments already visited by the driver (status "Delivered" or
    "Not_Delivered") are not re-optimized; they hold on to the sequence slot
    they already occupy, and the pending stops (In_Transit / Out_For_Delivery /
    not yet attempted) are laid out into whatever slots remain.

    actual_* fields are initialized to match planned_* — they represent what
    actually happened on the ground and are expected to diverge over time
    (e.g. a driver skips a stop), but start out identical to the plan.
    """
    total_slots = len(rows)

    delivered_rows = [r for r in rows if getattr(r, "status", None) in LOCKED_STATUSES]
    pending_rows = [r for r in rows if getattr(r, "status", None) not in LOCKED_STATUSES]

    occupied_slots = set()
    for r in delivered_rows:
        slot = getattr(r, "actual_sequence_order", None)
        if slot:
            occupied_slots.add(slot)
        else:
            logger.warning(
                "Locked consignment %s has no existing sequence order; "
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

    # Re-optimize after the driver has started: plan the remaining stops from
    # the stop he finished last, not from the hub (otherwise stops near the
    # hub jump ahead of where he already is - DHRD10238, 18 Nov). The stored
    # starting_* fields below stay the DRS's hub.
    solve_lat, solve_lon = start_lat, start_lon
    if restart_point:
        solve_lat, solve_lon = restart_point["latitude"], restart_point["longitude"]
        logger.info("DRS %s: planning remaining stops from last completed stop %s",
                    drs_no, restart_point.get("consignmentId"))
    if stats_out is not None:
        stats_out["planned_from"] = (
            f"last_completed:{restart_point.get('consignmentId')}" if restart_point else "hub")

    # Road distances (featureFlags/roadDistanceRouting): the 11 -> 12 -> 13
    # case, where 12 looks close in a straight line but is far by road.
    matrix = None
    if use_road:
        matrix = _road_matrix(solve_lat, solve_lon, usable_pending,
                              stats_out if stats_out is not None else {})
    ordered_pending = solve_route_order(solve_lat, solve_lon, usable_pending,
                                        distance_matrix_m=matrix)

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
    # Heal a DRS that already has duplicate rows for one consignment (they each
    # claim a stop number). Best-effort: the fetch below ignores duplicates too.
    try:
        deactivate_duplicate_routing_rows(drs_no=drs_no)
    except Exception:
        logger.exception("Failed to deactivate duplicate routing rows for DRS %s", drs_no)
    rows = fetch_active_rows_for_drs(drs_no)

    if not rows:
        return {
            "optimized_count": 0,
            "message": f"No active consignments found in BQ for DRS {drs_no}.",
        }

    use_road = _road_distance_enabled()
    road_stats = {}
    restart_point = _restart_point_for(rows, drs_no)
    updates = compute_groups_and_sequence(rows, drs_no, start_meta or {},
                                          use_road=use_road, stats_out=road_stats,
                                          restart_point=restart_point)

    if updates:
        write_group_assignments(updates)
        upsert_routing_from_sorting(fetch_assigned_rows_for_drs(drs_no))

    result = {"optimized_count": len(updates),
              "planned_from": road_stats.get("planned_from", "hub")}
    if use_road:
        result["distance_source"] = road_stats.get("distance_source", "straight_line")
        result["road_distance"] = {k: road_stats[k] for k in (
            "mode", "points", "road_pairs", "estimated_pairs", "detour_factor",
            "from_cache", "from_api", "api_elements", "api_errors", "rate_limited")
            if k in road_stats}
    # Cost/cache log, one doc per run (app/db/routing_runs.py). Never raises.
    record_routing_run(drs_no, result, road_stats)
    return result
