"""Master-waypoint tier — see .specify/spec.md §5.3.

A small, curated set of high-confidence address -> coordinate anchors
(large apartment complexes, SEZs, tech parks). Checked *first*, ahead of
app/db/geocache.py, from app/services/save.py's `_geocode()`, gated behind
the `master_waypoint_v2` feature flag (app/db/feature_flags.py) — flag off
means this module is never called and save.py behaves exactly as it does
today. A hit costs $0: no Places API call, no BQML call.

A `verified: True` entry is an immutable anchor. The ONLY path allowed to
change one is `record_confirmation()`'s 3-distinct-confirmation promotion
rule (fed by the EOD ingestion contract, spec.md §5.4 — not built in this
pass, but this module is ready for it to call). `seed()` is the ops/manual
entry point for adding a new anchor; it refuses to overwrite an existing
verified entry for the same reason.

Lookup is exact-match only, no fuzzy tier. A false positive here sends a
driver to the wrong building with no API call in the loop to catch it —
worse than the cost of a cache miss falling through to geocache.py/the API.
"""

import logging
from typing import Optional

from google.cloud import firestore

from app.db.clients import get_fs_client
from app.db.geocache import normalize_address

logger = logging.getLogger(__name__)

MASTER_WAYPOINT_COLLECTION = "master_waypoints"
PROMOTION_CONFIRMATION_COUNT = 3
# ~1.1m at the equator -- distinct confirmations must agree to roughly this
# precision to count as "the same corrected point," not coincidentally close.
COORDINATE_ROUND_DP = 5


def _collection():
    return get_fs_client().collection(MASTER_WAYPOINT_COLLECTION)


def normalize_alias(text: str) -> str:
    """Aliases are building/complex names -- the same normalization
    geocache.py already applies to addresses (lowercase, strip pincodes/Plus
    Codes, canonicalize city/building spelling variants) applies just as well
    here, so this reuses it rather than maintaining a second implementation."""
    return normalize_address(text)


def _doc_id(normalized_alias: str) -> str:
    # Firestore doc ids can't be arbitrarily long; normalized aliases are
    # already short (building names), but cap defensively.
    return normalized_alias[:200] or "unknown"


def lookup(alias_or_address: str) -> Optional[dict]:
    """Exact-match lookup against verified entries. Returns None on a miss
    OR on any Firestore error -- a lookup failure must fall through to
    geocache.py, never block the pipeline."""
    normalized = normalize_alias(alias_or_address)
    if not normalized:
        return None

    try:
        docs = list(
            _collection()
            .where("alias_normalized", "==", normalized)
            .where("verified", "==", True)
            .limit(1)
            .stream()
        )
    except Exception:
        logger.exception("Master-waypoint lookup failed for '%s'", alias_or_address[:80])
        return None

    if not docs:
        return None

    data = docs[0].to_dict() or {}
    return {
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "formatted_address": data.get("formatted_address") or alias_or_address,
        "source": "master_waypoint",
    }


def seed(alias: str, latitude: float, longitude: float, formatted_address: Optional[str] = None) -> None:
    """Ops entry point for a manually-confirmed anchor. Populating the initial
    set of anchors is a data/ops task (spec.md §1.2), not application logic --
    this is just the write primitive it calls. Refuses to touch an existing
    verified entry so re-running a seed script can't silently clobber a point
    that's already been confirmed/corrected in prod."""
    normalized = normalize_alias(alias)
    if not normalized:
        raise ValueError("alias must not be empty")

    doc_ref = _collection().document(_doc_id(normalized))
    existing = doc_ref.get()
    if existing.exists and (existing.to_dict() or {}).get("verified"):
        logger.info("Master-waypoint '%s' already verified; seed is a no-op.", alias)
        return

    doc_ref.set({
        "alias_raw": alias,
        "alias_normalized": normalized,
        "latitude": latitude,
        "longitude": longitude,
        "formatted_address": formatted_address or alias,
        "verified": True,
        "confirmation_count": 0,
        "pending_confirmations": [],
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })


def record_confirmation(alias: str, corrected_latitude: float, corrected_longitude: float, consignment_id: str) -> dict:
    """EOD-feedback promotion path (spec.md §5.3/§5.4). Not wired to a live
    caller in this pass -- the /eod-delivery-feedback endpoint is §5.4,
    a separate feature -- but implemented now so that feature has something
    to call rather than inventing its own promotion logic later.

    Every DISTINCT consignmentId confirming the same rounded coordinate
    counts once (a redelivery retry of the same consignment doesn't inflate
    the count). On the 3rd distinct confirmation, the point is promoted to
    verified=True -- the one sanctioned exception to a verified entry's
    immutability. Confirmations against an already-verified entry are
    ignored here by design; a verified point that keeps getting corrected is
    a HILT/dispatcher matter, not something this function silently acts on.
    """
    normalized = normalize_alias(alias)
    if not normalized:
        raise ValueError("alias must not be empty")

    coord_key = f"{round(corrected_latitude, COORDINATE_ROUND_DP)},{round(corrected_longitude, COORDINATE_ROUND_DP)}"
    doc_ref = _collection().document(_doc_id(normalized))
    snapshot = doc_ref.get()
    data = (snapshot.to_dict() or {}) if snapshot.exists else {}

    if data.get("verified"):
        logger.info("Ignoring confirmation for already-verified waypoint '%s'.", alias)
        return {"promoted": False, "reason": "already_verified"}

    pending = list(data.get("pending_confirmations") or [])
    already_confirmed_by = {
        p.get("consignmentId") for p in pending if p.get("coord_key") == coord_key
    }
    if consignment_id in already_confirmed_by:
        return {"promoted": False, "reason": "duplicate_confirmation", "distinct_count": len(already_confirmed_by)}

    pending.append({"consignmentId": consignment_id, "coord_key": coord_key})
    distinct_count = len({
        p.get("consignmentId") for p in pending if p.get("coord_key") == coord_key
    })

    if distinct_count >= PROMOTION_CONFIRMATION_COUNT:
        doc_ref.set({
            "alias_raw": alias,
            "alias_normalized": normalized,
            "latitude": corrected_latitude,
            "longitude": corrected_longitude,
            "verified": True,
            "confirmation_count": distinct_count,
            "pending_confirmations": [],
            "promoted_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        logger.info("Promoted master-waypoint '%s' after %d distinct confirmations.", alias, distinct_count)
        return {"promoted": True, "distinct_count": distinct_count}

    doc_ref.set({
        "alias_raw": alias,
        "alias_normalized": normalized,
        "pending_confirmations": pending,
        "confirmation_count": distinct_count,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)
    return {"promoted": False, "distinct_count": distinct_count}
