"""Delivery evidence and human corrections for geocode-cache owners.

Only this service changes a cache verdict.  The callers (3PL delivery/admin)
name a consignment; we resolve its owner cache entry before recording evidence,
so spelling variants and same-DRS memo reuse cannot create competing entries.
"""

from datetime import datetime, timezone
import re

from google.cloud import firestore

from app.config import (
    GEOCODE_CACHE_MAX_OBSERVATIONS,
    GEOCODE_CACHE_MAX_OBSERVATION_ACCURACY_METERS,
    GEOCODE_CACHE_MIN_VISITS,
    GEOCODE_CACHE_VERIFY_RADIUS_METERS,
)
from app.db import drs_memo
from app.db.clients import get_fs_client
from app.db.firestore import (
    CONSIGNMENTS_COLLECTION,
    ROUTING_COLLECTION,
    set_routing_cache_metadata,
)
from app.db.geocache import (
    CACHED_RESULT_FIELDS,
    distance_meters,
    find_cache_entry,
    get_cache_entry,
    invalidate_snapshot,
    normalize_address,
    save_to_cache,
    verify_cache_group,
)
from app.services.address_match import consignment_match_info


PINCODE_RE = re.compile(r"\b\d{6}\b")


def _routing(consignment_id):
    snap = get_fs_client().collection(ROUTING_COLLECTION).document(str(consignment_id)).get()
    return (snap.to_dict() or {}) if snap.exists else {}


def _consignment(consignment_id):
    snap = get_fs_client().collection(CONSIGNMENTS_COLLECTION).document(str(consignment_id)).get()
    return (snap.to_dict() or {}) if snap.exists else {}


def _cache_previous(cache_id, data, *, created=False):
    # Keep the entire entry so restore can safely replace it and remove values
    # introduced by an incorrect verdict (observations/suggested pin included).
    return {"id": cache_id, "created": bool(created), "entry": dict(data or {})}


def _normalise_observations(observations):
    return [item for item in (observations or []) if isinstance(item, dict)]


def _unique_drs_observations(observations):
    """One delivery point per DRS: multiple parcels on a run are one visit."""
    seen = set()
    unique = []
    for observation in observations:
        drs_id = str(observation.get("drs_id") or "").strip()
        if not drs_id or drs_id in seen:
            continue
        latitude, longitude = observation.get("latitude"), observation.get("longitude")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            seen.add(drs_id)
            unique.append(observation)
    return unique


def _within(latitude_a, longitude_a, latitude_b, longitude_b, radius):
    distance = distance_meters(latitude_a, longitude_a, latitude_b, longitude_b)
    return distance is not None and distance <= radius


def visit_count(entry, observations=None):
    """Separate DRS visits agreeing with the currently stored cache pin."""
    observations = _unique_drs_observations(
        observations if observations is not None else entry.get("observations")
    )
    return sum(
        1
        for observation in observations
        if _within(
            entry.get("latitude"), entry.get("longitude"),
            observation.get("latitude"), observation.get("longitude"),
            GEOCODE_CACHE_VERIFY_RADIUS_METERS,
        )
    )


def _centroid(points):
    return (
        sum(point["latitude"] for point in points) / len(points),
        sum(point["longitude"] for point in points) / len(points),
    )


def suggested_pin(entry, observations=None):
    """A separate, three-DRS cluster that is plausible enough for review."""
    observations = _unique_drs_observations(
        observations if observations is not None else entry.get("observations")
    )
    best = None
    for seed in observations:
        cluster = [
            item for item in observations
            if _within(seed["latitude"], seed["longitude"],
                       item["latitude"], item["longitude"],
                       GEOCODE_CACHE_VERIFY_RADIUS_METERS)
        ]
        if len(cluster) < 3:
            continue
        latitude, longitude = _centroid(cluster)
        if _within(entry.get("latitude"), entry.get("longitude"), latitude, longitude,
                   GEOCODE_CACHE_VERIFY_RADIUS_METERS):
            continue
        candidate = {"latitude": latitude, "longitude": longitude, "visit_count": len(cluster)}
        if best is None or candidate["visit_count"] > best["visit_count"]:
            best = candidate
    return best


def _drs_id(routing, consignment):
    return str(
        routing.get("drsId") or routing.get("drsNo")
        or consignment.get("drsId") or consignment.get("drsNo") or ""
    ).strip()


def _resolve_owner(consignment_id):
    """(cache_id, entry, routing, consignment) or no owner.

    The resolution order is deliberately stable: routing's explicit owner,
    address text/variants, then the same-DRS memo group.
    """
    routing = _routing(consignment_id)
    consignment = _consignment(consignment_id)
    cache_id = routing.get("geocode_cache_id")
    if cache_id:
        hit = get_cache_entry(cache_id)
        if hit:
            return *hit, routing, consignment

    receiver = consignment.get("receiver") or {}
    address = receiver.get("address") or routing.get("receiverAddress") or ""
    hit = find_cache_entry(normalize_address(address))
    if hit:
        return *hit, routing, consignment

    drs_id = _drs_id(routing, consignment)
    if drs_id:
        entries = drs_memo.load_entries(drs_id)
        info = consignment_match_info(receiver) if receiver else None
        memo_hit = drs_memo.find_match(entries, info) if info else None
        if memo_hit and memo_hit[1].get("cache_id"):
            hit = get_cache_entry(memo_hit[1]["cache_id"])
            if hit:
                return *hit, routing, consignment
        for entry in entries.values():
            if consignment_id in (entry.get("consignment_ids") or []) and entry.get("cache_id"):
                hit = get_cache_entry(entry["cache_id"])
                if hit:
                    return *hit, routing, consignment
    return None, None, routing, consignment


def _pincodes(routing, consignment):
    values = set()
    for value in (routing.get("pincode"), (consignment.get("receiver") or {}).get("address"),
                  routing.get("receiverAddress")):
        if value:
            values.update(PINCODE_RE.findall(str(value)))
    return sorted(values)


def confirm(consignment_id, delivery_location, actor):
    cache_id, entry, routing, consignment = _resolve_owner(consignment_id)
    if not entry:
        raise LookupError("No geocode cache entry is linked to this consignment.")

    previous = _cache_previous(cache_id, entry)
    observations = _normalise_observations(entry.get("observations"))
    accuracy = delivery_location.get("accuracy")
    drs_id = _drs_id(routing, consignment)
    accepted = accuracy is None or accuracy <= GEOCODE_CACHE_MAX_OBSERVATION_ACCURACY_METERS
    if accepted and drs_id and not any(str(item.get("drs_id")) == drs_id for item in observations):
        observations.append({
            "latitude": float(delivery_location["latitude"]),
            "longitude": float(delivery_location["longitude"]),
            "accuracy": float(accuracy) if accuracy is not None else None,
            "drs_id": drs_id,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        })
    observations = observations[-GEOCODE_CACHE_MAX_OBSERVATIONS:]
    visits = visit_count(entry, observations)
    verify = visits >= GEOCODE_CACHE_MIN_VISITS
    update = {
        "label_pincodes": firestore.ArrayUnion(_pincodes(routing, consignment)),
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    # An inaccurate delivery is not evidence at all; in particular, do not
    # create a misleading empty observations list on a previously untouched
    # entry. Existing observations are left intact.
    if (accepted and drs_id) or "observations" in entry:
        update["observations"] = observations
    candidate = suggested_pin(entry, observations)
    if candidate:
        update["suggested_pin"] = candidate
    if verify:
        update.update({
            "verified": True,
            "verified_by": actor,
            "verified_at": firestore.SERVER_TIMESTAMP,
            "source": "verified",
            "expire_at": firestore.DELETE_FIELD,
        })
    get_fs_client().collection("geocode_cache").document(cache_id).update(update)
    set_routing_cache_metadata({consignment_id: {"cache_id": cache_id, "person_corrected": False}})
    # Existing memo siblings identify the same physical place, so retain the
    # pre-Phase-3 group behavior once repeat delivery evidence is sufficient.
    group = entry.get("drs_memo_group")
    if verify and group:
        verify_cache_group(group, actor)
    invalidate_snapshot()
    return {
        "id": cache_id,
        "previous": previous,
        "visitCount": visits,
        "verified": verify or bool(entry.get("verified")),
        "observationAccepted": accepted and bool(drs_id),
        "suggestedPin": candidate,
    }


def correct(consignment_id, latitude, longitude, actor):
    cache_id, entry, routing, consignment = _resolve_owner(consignment_id)
    created = False
    if not entry:
        receiver = consignment.get("receiver") or {}
        address = receiver.get("address") or routing.get("receiverAddress") or ""
        if not address:
            raise LookupError("No address is available to create a geocode cache entry.")
        result = {field: None for field in CACHED_RESULT_FIELDS}
        result.update({"latitude": latitude, "longitude": longitude, "pincode": routing.get("pincode")})
        cache_id = save_to_cache(address, result, lookup_address=address, source="verified")
        entry = get_cache_entry(cache_id)[1] if cache_id else None
        created = True
    if not entry or not cache_id:
        raise LookupError("Could not create a geocode cache entry.")

    previous = _cache_previous(cache_id, entry, created=created)
    visits = visit_count(entry)
    get_fs_client().collection("geocode_cache").document(cache_id).update({
        "latitude": float(latitude),
        "longitude": float(longitude),
        "verified": True,
        "verified_by": actor,
        "verified_at": firestore.SERVER_TIMESTAMP,
        "source": "verified",
        "suggested_pin": firestore.DELETE_FIELD,
        "expire_at": firestore.DELETE_FIELD,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    set_routing_cache_metadata({consignment_id: {"cache_id": cache_id, "person_corrected": True}})
    invalidate_snapshot()
    return {"id": cache_id, "previous": previous, "visitCount": visits, "verified": True}


def restore(previous):
    """Undo a confirm/correct response.  Accepts the exact returned payload."""
    previous = previous or {}
    cache_id = previous.get("id")
    if not cache_id:
        raise ValueError("previous.id is required")
    ref = get_fs_client().collection("geocode_cache").document(cache_id)
    if previous.get("created"):
        ref.delete()
    else:
        entry = previous.get("entry")
        if not isinstance(entry, dict):
            raise ValueError("previous.entry is required")
        ref.set(entry, merge=False)
    invalidate_snapshot()
    return {"id": cache_id, "restored": True}
