"""Firestore reads and writes."""

import logging

from app.config import FIRESTORE_BATCH_SIZE
from app.db.clients import get_fs_client

logger = logging.getLogger(__name__)

CONSIGNMENTS_COLLECTION = "consignments"
ROUTING_COLLECTION = "consignments_routing"
DRS_STARTING_POINT_COLLECTION = "drs_starting_point"


def get_consignments_by_id(consignment_ids):
    fs_client = get_fs_client()
    collection = fs_client.collection(CONSIGNMENTS_COLLECTION)

    doc_refs = [collection.document(cid) for cid in consignment_ids]
    fetched_docs = list(fs_client.get_all(doc_refs))

    docs = []
    not_found_ids = []

    for cid, doc in zip(consignment_ids, fetched_docs):
        if doc.exists:
            docs.append(doc)
        else:
            not_found_ids.append(cid)

    # Fallback: field-equality query for anything not found by direct doc id.
    if not_found_ids:
        still_missing = []
        for cid in not_found_ids:
            query_docs = list(
                collection.where("consignmentId", "==", cid).limit(1).stream()
            )
            if query_docs:
                docs.append(query_docs[0])
            else:
                still_missing.append(cid)
        not_found_ids = still_missing

    logger.info(
        "Resolved %d/%d requested consignment(s); %d not found.",
        len(docs),
        len(consignment_ids),
        len(not_found_ids),
    )
    return docs, not_found_ids


def get_drs_starting_point(drsno):
    """Fetch the depot/hub coordinates for a DRS from drs_starting_point.

    This is the real place that data gets read from — the save pipeline no
    longer touches this collection, since the starting point may not exist yet
    at save-time.
    """
    doc = get_fs_client().collection(DRS_STARTING_POINT_COLLECTION).document(drsno).get()
    if not doc.exists:
        logger.warning("No starting point found in Firestore for DRS: %s", drsno)
        return None

    data = doc.to_dict()
    return {
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "starting_address": data.get("starting_address"),
    }


def _iso(value):
    return value.isoformat() if value else None


# Fields written by both pipelines.
def _base_routing_doc(row):
    return {
        "sorting_id": row.sorting_id,
        "drsNo": row.drsNo,
        "drsId": row.drsId,
        "driverNumericId": row.driverNumericId,
        "consignmentId": row.consignmentId,
        "receiverAddress": row.receiverAddress,
        "receiverName": row.receiverName,
        "geocode_address": getattr(row, "geocode_address", None),
        "starting_address": row.starting_address,
        "starting_latitude": row.starting_latitude,
        "starting_longitude": row.starting_longitude,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "locality": row.locality,
        "area": row.area,
        "geohash_locality_loc": row.geohash_locality_loc,
        "geohash_building_loc": row.geohash_building_loc,
        "geohash_exact_loc": row.geohash_exact_loc,
        "pincode": row.pincode,
        "geohash_group_id": row.geohash_group_id,
        "planned_inside_cluster_sequence": row.planned_inside_cluster_sequence,
        "planned_sequence_order": row.planned_sequence_order,
        "actual_inside_cluster_sequence": row.actual_inside_cluster_sequence,
        "actual_sequence_order": row.actual_sequence_order,
        "is_commercial": row.is_commercial,
        "is_active": row.is_active,
        "exception_flag": row.exception_flag,
        # getattr: safe on rows fetched before the BigQuery columns exist.
        "locationOverridden": bool(getattr(row, "locationOverridden", False) or False),
        "overriddenBy": getattr(row, "overriddenBy", None),
        "overriddenAt": _iso(getattr(row, "overriddenAt", None)),
        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
    }


# The save pipeline additionally publishes the geocode provenance fields.
def _save_routing_doc(row):
    doc = _base_routing_doc(row)
    doc.update({
        "formatted_address": getattr(row, "formatted_address", None),
        "place_id": getattr(row, "place_id", None),
        "location_type": getattr(row, "location_type", None),
        "street_number": getattr(row, "street_number", None),
        "route_name": getattr(row, "route_name", None),
        "district": getattr(row, "district", None),
        "state": getattr(row, "state", None),
        "country_code": getattr(row, "country_code", None),
        "geocode_status": getattr(row, "geocode_status", None),
        "geocode_error": getattr(row, "geocode_error", None),
    })
    return doc


def _commit_in_batches(rows, doc_builder):
    fs_client = get_fs_client()
    batch = fs_client.batch()
    count = 0

    for row in rows:
        doc_ref = fs_client.collection(ROUTING_COLLECTION).document(row.consignmentId)
        batch.set(doc_ref, doc_builder(row), merge=True)
        count += 1
        if count % FIRESTORE_BATCH_SIZE == 0:
            batch.commit()
            batch = fs_client.batch()

    if count % FIRESTORE_BATCH_SIZE != 0:
        batch.commit()

    logger.info("Upserted %d record(s) to Firestore %s.", count, ROUTING_COLLECTION)
    return count


def upsert_consignments_routing(rows):
    """Post-save sync: routing fields plus geocode provenance."""
    return _commit_in_batches(rows, _save_routing_doc)


def upsert_routing_from_sorting(rows):
    """Post-sorting sync: routing fields only, leaving the geocode provenance
    fields written by the save pipeline untouched (the writes are merges)."""
    return _commit_in_batches(rows, _base_routing_doc)
