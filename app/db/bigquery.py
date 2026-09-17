"""BigQuery reads and writes for consignments_routing."""

import logging
from datetime import datetime, timezone

from google.cloud import bigquery

from app.config import (
    GEOCODING_SOURCE,
    MAX_ROWS_PER_MERGE,
    MAX_ROWS_PER_UPDATE,
    STRUCTURED_TABLE_REF,
    TABLE_REF,
)
from app.db.clients import get_bq_client

logger = logging.getLogger(__name__)


def _row_to_struct_param(row):
    return bigquery.StructQueryParameter(
        None,
        bigquery.ScalarQueryParameter("consignmentId", "STRING", row["consignmentId"]),
        bigquery.ScalarQueryParameter("drsNo", "STRING", row["drsNo"]),
        bigquery.ScalarQueryParameter("drsId", "STRING", row["drsId"]),
        bigquery.ScalarQueryParameter("driverNumericId", "STRING", row["driverNumericId"]),
        bigquery.ScalarQueryParameter("receiverAddress", "STRING", row["receiverAddress"]),
        bigquery.ScalarQueryParameter("receiverName", "STRING", row["receiverName"]),
        bigquery.ScalarQueryParameter("geocode_address", "STRING", row["geocode_address"]),
        bigquery.ScalarQueryParameter("starting_address", "STRING", row["starting_address"]),
        bigquery.ScalarQueryParameter("starting_latitude", "FLOAT64", row["starting_latitude"]),
        bigquery.ScalarQueryParameter("starting_longitude", "FLOAT64", row["starting_longitude"]),
        bigquery.ScalarQueryParameter("latitude", "FLOAT64", row["latitude"]),
        bigquery.ScalarQueryParameter("longitude", "FLOAT64", row["longitude"]),
        bigquery.ScalarQueryParameter("locality", "STRING", row["locality"]),
        bigquery.ScalarQueryParameter("area", "STRING", row["area"]),
        bigquery.ScalarQueryParameter("geohash_locality_loc", "STRING", row["geohash_locality_loc"]),
        bigquery.ScalarQueryParameter("geohash_building_loc", "STRING", row["geohash_building_loc"]),
        bigquery.ScalarQueryParameter("geohash_exact_loc", "STRING", row["geohash_exact_loc"]),
        bigquery.ScalarQueryParameter("pincode", "STRING", row["pincode"]),
        bigquery.ScalarQueryParameter("exception_flag", "STRING", row["exception_flag"]),
        bigquery.ScalarQueryParameter("is_commercial", "BOOL", row["is_commercial"]),
        bigquery.ScalarQueryParameter("formatted_address", "STRING", row["formatted_address"]),
        bigquery.ScalarQueryParameter("place_id", "STRING", row["place_id"]),
        bigquery.ScalarQueryParameter("location_type", "STRING", row["location_type"]),
        bigquery.ScalarQueryParameter("street_number", "STRING", row["street_number"]),
        bigquery.ScalarQueryParameter("route_name", "STRING", row["route_name"]),
        bigquery.ScalarQueryParameter("district", "STRING", row["district"]),
        bigquery.ScalarQueryParameter("state", "STRING", row["state"]),
        bigquery.ScalarQueryParameter("country_code", "STRING", row["country_code"]),
        bigquery.ScalarQueryParameter("geocode_status", "STRING", row["geocode_status"]),
        bigquery.ScalarQueryParameter("geocode_error", "STRING", row["geocode_error"]),
    )


# MATCHED (consignment already in BQ): updates address/geocode fields only.
# sorting_id, geohash_group_id and the planned_*/actual_* sequence fields are
# left untouched so an already-routed consignment doesn't get silently
# un-grouped by a later re-save.
# NOT MATCHED (new consignment): inserts a fresh row with a generated
# sorting_id and geohash_group_id = 'UNASSIGNED', ready for run_sorting.
MERGE_SQL = f"""
    MERGE `{TABLE_REF}` T
    USING UNNEST(@rows) S
    ON T.consignmentId = S.consignmentId
    AND T.drsNo = S.drsNo
    WHEN MATCHED THEN UPDATE SET
        T.receiverAddress = S.receiverAddress,
        T.receiverName = S.receiverName,
        T.geocode_address = S.geocode_address,
        T.starting_address = S.starting_address,
        T.starting_latitude = S.starting_latitude,
        T.starting_longitude = S.starting_longitude,
        T.latitude = S.latitude,
        T.longitude = S.longitude,
        T.locality = S.locality,
        T.area = S.area,
        T.geohash_locality_loc = S.geohash_locality_loc,
        T.geohash_building_loc = S.geohash_building_loc,
        T.geohash_exact_loc = S.geohash_exact_loc,
        T.pincode = S.pincode,
        T.exception_flag = S.exception_flag,
        T.is_commercial = S.is_commercial,
        T.formatted_address = S.formatted_address,
        T.place_id = S.place_id,
        T.location_type = S.location_type,
        T.street_number = S.street_number,
        T.route_name = S.route_name,
        T.district = S.district,
        T.state = S.state,
        T.country_code = S.country_code,
        T.geocode_status = S.geocode_status,
        T.geocode_error = S.geocode_error,
        T.updated_at = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (
        sorting_id, drsNo, drsId, driverNumericId, consignmentId,
        receiverAddress, receiverName, geocode_address, starting_address,
        starting_latitude, starting_longitude, latitude, longitude, locality, area,
        geohash_locality_loc, geohash_building_loc, geohash_exact_loc, pincode,
        geohash_group_id, planned_inside_cluster_sequence, planned_sequence_order,
        actual_inside_cluster_sequence, actual_sequence_order,
        geocoding_source, exception_flag, is_commercial, is_active,
        formatted_address, place_id, location_type,
        street_number, route_name, district, state, country_code,
        geocode_status, geocode_error, created_at, updated_at
    ) VALUES (
        GENERATE_UUID(), S.drsNo, S.drsId, S.driverNumericId, S.consignmentId,
        S.receiverAddress, S.receiverName, S.geocode_address, S.starting_address,
        S.starting_latitude, S.starting_longitude, S.latitude, S.longitude, S.locality, S.area,
        S.geohash_locality_loc, S.geohash_building_loc, S.geohash_exact_loc, S.pincode,
        'UNASSIGNED', 0, 0, 0, 0,
        '{GEOCODING_SOURCE}', S.exception_flag, S.is_commercial, TRUE,
        S.formatted_address, S.place_id, S.location_type,
        S.street_number, S.route_name, S.district, S.state, S.country_code,
        S.geocode_status, S.geocode_error, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP()
    )
"""


def merge_routing_rows(rows):
    if not rows:
        return

    bq_client = get_bq_client()
    for i in range(0, len(rows), MAX_ROWS_PER_MERGE):
        batch = rows[i:i + MAX_ROWS_PER_MERGE]
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter(
                    "rows", "STRUCT", [_row_to_struct_param(r) for r in batch]
                ),
            ]
        )
        logger.info("Executing MERGE for batch of %d rows", len(batch))
        try:
            bq_client.query(MERGE_SQL, job_config=job_config).result()
        except Exception:
            logger.exception("BigQuery MERGE failed")
            logger.error("Failed batch consignmentIds: %s", [r["consignmentId"] for r in batch])
            raise


def fetch_rows_by_consignment_ids(consignment_ids):
    if not consignment_ids:
        return []

    query = f"SELECT * FROM `{TABLE_REF}` WHERE consignmentId IN UNNEST(@ids)"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", consignment_ids)]
    )
    return list(get_bq_client().query(query, job_config=job_config).result())


def fetch_active_rows_for_drs(drs_no):
    """Fetch every active consignment for this DRS — the whole DRS gets
    re-optimized on each run, not just newly-added/unassigned rows. Joined to
    the structured export so the delivery status is available to the sorter."""
    query = f"""
        SELECT
            C.sorting_id,
            C.consignmentId,
            C.drsNo,
            S.status,
            S.statusCode,
            C.pincode,
            C.latitude,
            C.longitude,
            C.geohash_locality_loc,
            C.geohash_building_loc,
            C.geohash_exact_loc,
            C.planned_sequence_order
        FROM `{TABLE_REF}` AS C
        JOIN `{STRUCTURED_TABLE_REF}` AS S
            ON C.drsNo = S.drsNo
            AND C.consignmentId = S.consignmentId
        WHERE C.is_active IS TRUE
          AND C.drsNo = @drs_no
        ORDER BY C.planned_sequence_order;
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("drs_no", "STRING", drs_no)]
    )
    rows = list(get_bq_client().query(query, job_config=job_config).result())
    logger.info("Found %d active consignment(s) for DRS %s.", len(rows), drs_no)
    return rows


def fetch_assigned_rows_for_drs(drs_no):
    query = f"""
        SELECT * FROM `{TABLE_REF}`
        WHERE geohash_group_id != 'UNASSIGNED' AND drsNo = @drs_no
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("drs_no", "STRING", drs_no)]
    )
    return list(get_bq_client().query(query, job_config=job_config).result())


def write_group_assignments(updates):
    """Writes the solved route back onto the existing rows (matched by
    sorting_id, which already exists from the save pipeline — this is a plain
    UPDATE, not an upsert, since these rows are guaranteed to exist)."""
    if not updates:
        return

    now = datetime.now(timezone.utc).isoformat()
    bq_client = get_bq_client()

    for i in range(0, len(updates), MAX_ROWS_PER_UPDATE):
        batch = updates[i:i + MAX_ROWS_PER_UPDATE]
        geohash_cases = " ".join(f"WHEN sorting_id = '{u['sorting_id']}' THEN '{u['geohash_group_id']}'" for u in batch)
        address_cases = " ".join(f"WHEN sorting_id = '{u['sorting_id']}' THEN '{u['starting_address']}'" for u in batch)
        start_lat_cases = " ".join(f"WHEN sorting_id = '{u['sorting_id']}' THEN {u['starting_latitude']}" for u in batch)
        start_lon_cases = " ".join(f"WHEN sorting_id = '{u['sorting_id']}' THEN {u['starting_longitude']}" for u in batch)
        planned_cluster_cases = " ".join(f"WHEN sorting_id = '{u['sorting_id']}' THEN {u['planned_inside_cluster_sequence']}" for u in batch)
        planned_seq_cases = " ".join(f"WHEN sorting_id = '{u['sorting_id']}' THEN {u['planned_sequence_order']}" for u in batch)
        actual_cluster_cases = " ".join(f"WHEN sorting_id = '{u['sorting_id']}' THEN {u['actual_inside_cluster_sequence']}" for u in batch)
        actual_seq_cases = " ".join(f"WHEN sorting_id = '{u['sorting_id']}' THEN {u['actual_sequence_order']}" for u in batch)
        formatted_ids = ", ".join(f"'{u['sorting_id']}'" for u in batch)

        query = f"""
            UPDATE `{TABLE_REF}`
            SET
                geohash_group_id = CASE {geohash_cases} END,
                starting_address = CASE {address_cases} END,
                starting_latitude = CASE {start_lat_cases} END,
                starting_longitude = CASE {start_lon_cases} END,
                planned_inside_cluster_sequence = CASE {planned_cluster_cases} END,
                planned_sequence_order = CASE {planned_seq_cases} END,
                actual_inside_cluster_sequence = CASE {actual_cluster_cases} END,
                actual_sequence_order = CASE {actual_seq_cases} END,
                updated_at = TIMESTAMP('{now}')
            WHERE sorting_id IN ({formatted_ids})
        """
        bq_client.query(query).result()

    logger.info("Wrote route assignments for %d row(s) to BigQuery.", len(updates))
