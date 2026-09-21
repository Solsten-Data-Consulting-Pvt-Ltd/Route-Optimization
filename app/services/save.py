"""Save pipeline — port of the hermes-save Cloud Function."""

import logging
import time
from datetime import datetime, timezone

import pygeohash as pgh

from app.config import GEOHASH_BUILDING_LEN, GEOHASH_LOCALITY_LEN, GOOGLE_MAPS_API_KEY
from app.db.bigquery import fetch_rows_by_consignment_ids, merge_routing_rows
from app.db.cache_metrics import write_drs_cache_metrics
from app.db.firestore import get_consignments_by_id, upsert_consignments_routing
from app.db.geocache import get_cached_geocode_with_outcome, save_to_cache
from app.services.address import build_geocode_address, normalize_to_single_line
from app.services.geocoding import (
    derive_exception_flag,
    derive_is_commercial,
    places_search_address,
)

logger = logging.getLogger(__name__)


def build_row(drs_no, drs_id, driver_numeric_id, consignment_id, receiver_address, receiver_name,
              geocode_address_str, latitude, longitude, locality, area, geohash_exact, pincode,
              exception_flag, is_commercial, formatted_address=None, place_id=None, location_type=None,
              street_number=None, route_name=None, district=None, state=None, country_code=None,
              geocode_status="success", geocode_error=None):
    """Build one row dict for BigQuery/Firestore."""
    return {
        "drsNo": drs_no,
        "drsId": drs_id,
        "driverNumericId": str(driver_numeric_id) if driver_numeric_id is not None else "0",
        "consignmentId": consignment_id,
        "receiverAddress": receiver_address,
        "receiverName": receiver_name,
        "geocode_address": geocode_address_str,
        "starting_address": "PENDING_OPTIMIZATION",
        "starting_latitude": 0.0,
        "starting_longitude": 0.0,
        "latitude": latitude,
        "longitude": longitude,
        "locality": locality or "UNKNOWN",
        "area": area or "UNKNOWN",
        "geohash_locality_loc": geohash_exact[:GEOHASH_LOCALITY_LEN] if geohash_exact else None,
        "geohash_building_loc": geohash_exact[:GEOHASH_BUILDING_LEN] if geohash_exact else None,
        "geohash_exact_loc": geohash_exact,
        "pincode": pincode,
        "exception_flag": exception_flag,
        "is_commercial": is_commercial,
        "formatted_address": formatted_address,
        "place_id": place_id,
        "location_type": location_type,
        "street_number": street_number,
        "route_name": route_name,
        "district": district,
        "state": state,
        "country_code": country_code,
        "geocode_status": geocode_status,
        "geocode_error": geocode_error,
    }


def _failed_row(drs_no, drs_id, driver_numeric_id, consignment_id,
                receiver_address, receiver_name, geocode_address_str, reason):
    return build_row(
        drs_no, drs_id, driver_numeric_id, consignment_id,
        receiver_address, receiver_name, geocode_address_str,
        None, None, None, None, None, None,
        None, False,
        geocode_status="failed",
        geocode_error=reason,
    )


def _geocode(address, lookup_address=None):
    """Cache first, Places on a miss. A cache failure never fails the save.

    Fix 1 — `lookup_address` (receiver_address without name prefix) is
    forwarded to the cache functions so that the cache key is location-only.
    """
    try:
        cached, cache_outcome = get_cached_geocode_with_outcome(
            address, lookup_address=lookup_address
        )
    except Exception:
        logger.exception("Geocode cache read failed for '%s'", address[:80])
        cached = None
        cache_outcome = "cache_error"

    if cached:
        logger.info("Geocode cache hit for '%s'", address[:80])
        return cached, None, None, cache_outcome

    geocode_result, error_reason, error_code = places_search_address(address)

    if geocode_result:
        try:
            save_to_cache(address, geocode_result, lookup_address=lookup_address)
        except Exception:
            logger.exception("Geocode cache write failed for '%s'", address[:80])

    return geocode_result, error_reason, error_code, cache_outcome


def _new_drs_cache_counts(drs_no):
    return {
        "drsNo": drs_no,
        "consignmentsProcessed": 0,
        "invalidAddresses": 0,
        "cacheLookups": 0,
        "exactHits": 0,
        "fuzzyHits": 0,
        "misses": 0,
        "cacheErrors": 0,
        "apiCalls": 0,
        "apiFailures": 0,
    }


def _dedupe(consignment_ids):
    seen = set()
    deduped = []
    for cid in consignment_ids:
        if cid and cid not in seen:
            seen.add(cid)
            deduped.append(cid)
    return deduped


def save_consignments_pipeline(consignment_ids):
    """Fetch specific consignments by ID, geocode each, and insert-or-update
    (merge) them into BigQuery.

    Does NOT look up drs_starting_point here — that data may not exist yet at
    save-time (it's set later, before/during run_sorting). starting_* fields
    are written as placeholders and get filled in properly during route
    optimization.

    Geocoding strategy (simplified, no fallback):
      - `geocode_address_str` is the combined "ReceiverName, ReceiverAddress"
        string built by `build_geocode_address` (or whichever of the two
        exists). This SAME string is both what gets sent to the Google Places
        API and what gets persisted as the `geocode_address` column — there is
        no separate fallback query to receiverAddress alone, since the combined
        string consistently resolves better/more precise lat/lon (e.g. it can
        disambiguate apartment/unit-level matches that a bare street address
        can't).

    Returns a summary dict including a `failures` list of
    {consignmentId, reason} so the frontend can show exactly which
    consignments failed and why (e.g. bad/unresolvable address, or not found).
    """
    if not GOOGLE_MAPS_API_KEY:
        raise RuntimeError("GOOGLE_MAPS_API_KEY environment variable is not set.")

    start_dt = datetime.now(timezone.utc)
    start_perf = time.perf_counter()
    logger.info(
        "save_consignments_pipeline started at %s for %d requested consignment id(s)",
        start_dt.isoformat(), len(consignment_ids),
    )

    deduped_ids = _dedupe(consignment_ids)
    docs, not_found_ids = get_consignments_by_id(deduped_ids)

    rows_to_merge = []
    cache_metrics_by_drs = {}
    failures = [
        {"consignmentId": cid, "reason": "Consignment not found."}
        for cid in not_found_ids
    ]
    saved_count = 0

    for doc in docs:
        data = doc.to_dict() or {}
        consignment_id = data.get("consignmentId") or doc.id
        receiver = data.get("receiver") or {}

        receiver_name = normalize_to_single_line(str(receiver.get("name") or ""))
        receiver_address = normalize_to_single_line(str(receiver.get("address") or ""))

        drs_no = str(data.get("drsNo") or "").strip()
        drs_id = str(data.get("drsId") or "").strip()
        driver_numeric_id = data.get("driverNumericId")
        metric_drs_id = drs_id or "UNKNOWN"
        drs_metrics = cache_metrics_by_drs.setdefault(
            metric_drs_id, _new_drs_cache_counts(drs_no)
        )
        drs_metrics["consignmentsProcessed"] += 1

        geocode_address_str = build_geocode_address(receiver_name, receiver_address)

        if not geocode_address_str:
            drs_metrics["invalidAddresses"] += 1
            reason = "Receiver address and name are both missing."
            failures.append({"consignmentId": consignment_id, "reason": reason})
            rows_to_merge.append(_failed_row(
                drs_no, drs_id, driver_numeric_id, consignment_id,
                receiver_address, receiver_name, geocode_address_str, reason,
            ))
            continue

        geocode_result, error_reason, _error_code, cache_outcome = _geocode(
            geocode_address_str,
            lookup_address=receiver_address or None,
        )
        drs_metrics["cacheLookups"] += 1
        if cache_outcome == "exact_hit":
            drs_metrics["exactHits"] += 1
        elif cache_outcome == "fuzzy_hit":
            drs_metrics["fuzzyHits"] += 1
        elif cache_outcome == "miss":
            drs_metrics["misses"] += 1
        else:
            drs_metrics["cacheErrors"] += 1

        if cache_outcome not in ("exact_hit", "fuzzy_hit"):
            drs_metrics["apiCalls"] += 1
        geocode_error = error_reason
        geocode_status = "success" if geocode_result else "failed"

        if geocode_result is None:
            drs_metrics["apiFailures"] += 1
            logger.warning("Geocoding failed for consignment %s: %s", consignment_id, geocode_error)
            failures.append({"consignmentId": consignment_id, "reason": geocode_error})
            rows_to_merge.append(_failed_row(
                drs_no, drs_id, driver_numeric_id, consignment_id,
                receiver_address, receiver_name, geocode_address_str, geocode_error,
            ))
            continue

        geohash_exact = pgh.encode(
            geocode_result["latitude"], geocode_result["longitude"], precision=8
        )

        rows_to_merge.append(build_row(
            drs_no, drs_id, driver_numeric_id, consignment_id,
            receiver_address, receiver_name, geocode_address_str,
            geocode_result["latitude"], geocode_result["longitude"],
            geocode_result.get("locality"), geocode_result.get("area"),
            geohash_exact, geocode_result.get("pincode"),
            derive_exception_flag(geocode_result),
            derive_is_commercial(geocode_result),
            formatted_address=geocode_result.get("formatted_address"),
            place_id=geocode_result.get("place_id"),
            location_type=geocode_result.get("location_type"),
            street_number=geocode_result.get("street_number"),
            route_name=geocode_result.get("route_name"),
            district=geocode_result.get("district"),
            state=geocode_result.get("state"),
            country_code=geocode_result.get("country_code"),
            geocode_status=geocode_status,
            geocode_error=geocode_error,
        ))
        saved_count += 1

    if rows_to_merge:
        merge_routing_rows(rows_to_merge)
        bq_rows = fetch_rows_by_consignment_ids([r["consignmentId"] for r in rows_to_merge])
        upsert_consignments_routing(bq_rows)

    try:
        write_drs_cache_metrics(cache_metrics_by_drs, start_dt)
    except Exception:
        # Analytics must never make the customer-facing save operation fail.
        logger.exception("Failed to write DRS cache metrics")

    end_dt = datetime.now(timezone.utc)
    duration_seconds = round(time.perf_counter() - start_perf, 3)
    logger.info(
        "save_consignments_pipeline finished at %s (duration: %.3fs) - "
        "saved=%d failed=%d synced_rows=%d",
        end_dt.isoformat(), duration_seconds, saved_count, len(failures), len(rows_to_merge),
    )

    return {
        "saved": saved_count,
        "failed": len(failures),
        "failures": failures,
        "started_at": start_dt.isoformat(),
        "finished_at": end_dt.isoformat(),
        "duration_seconds": duration_seconds,
        "cache_metrics": [
            {
                "drsId": drs_id,
                "drsNo": counts["drsNo"],
                "consignmentsProcessed": counts["consignmentsProcessed"],
                "invalidAddresses": counts["invalidAddresses"],
                "cacheLookups": counts["cacheLookups"],
                "exactHits": counts["exactHits"],
                "fuzzyHits": counts["fuzzyHits"],
                "misses": counts["misses"],
                "cacheErrors": counts["cacheErrors"],
                "apiCalls": counts["apiCalls"],
                "apiFailures": counts["apiFailures"],
                "hitRate": round(
                    ((counts["exactHits"] + counts["fuzzyHits"]) / counts["cacheLookups"]) * 100,
                    2,
                ) if counts["cacheLookups"] else 0.0,
            }
            for drs_id, counts in cache_metrics_by_drs.items()
        ],
    }
