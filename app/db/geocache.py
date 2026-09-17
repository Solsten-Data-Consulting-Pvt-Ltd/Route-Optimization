"""Address-scoped geocode cache.

Before calling Places, check whether this address has already been geocoded
and verified; if so, skip the API call entirely and reuse the stored result.

Ported from the hermes-save `geocache.py`, keeping its matching semantics:

  * `normalize_address` is unchanged — lowercase, collapse commas/whitespace,
    strip any 6-digit pincode, then canonicalize city spellings with a
    word-boundary substitution (a plain str.replace() is unsafe when a variant
    is a substring of its canonical form, e.g. "sarjapur" -> "sarjapura"), and
    re-collapse whitespace afterwards because the pincode strip leaves a hole.
  * The servable-cache gate is still an explicit `verified` boolean, not a
    numeric confidence threshold. Nothing here sets it; it is an ops verdict.
  * The fuzzy scorer is still `fuzz.token_sort_ratio` at 85. token_set_ratio
    treats one address as a match whenever its tokens are a subset of the
    other's, scoring "123 Sarjapur Road" against "456 Sarjapur Road" at 87.5 —
    above the cutoff, so it would false-positive. token_sort_ratio tolerates
    reordering but not changed tokens, scoring that pair at 83.3. A false
    positive sends a driver to the wrong building, which is worse than a fresh
    but flawed API call.

Two things differ from the original, both deliberate:

  1. The cache document stores the WHOLE geocode result, not just lat/lon.
     The original stored four fields, so a cache hit produced a row with
     locality/area = "UNKNOWN", pincode = NULL and is_commercial = False —
     the same address got worse data the second time it was saved.
  2. Lookup tries an indexed equality query on `address_normalized` first and
     only falls back to the fuzzy scan, and that scan runs against a snapshot
     loaded once per process (TTL below) instead of streaming the entire
     collection once per consignment. Documents are also keyed by a hash of
     the normalized address rather than an auto-id, so re-saving an address
     updates one document instead of appending a duplicate.

`pincode_match` is recomputed on every hit rather than read from the cache:
normalization strips the pincode, so two addresses that differ only by their
pincode share a cache entry, and the stored flag would be about the wrong one.
"""

import hashlib
import logging
import re
import time
from typing import Optional

from rapidfuzz import fuzz, process, utils

from google.cloud import firestore

from app.config import GEOCODE_CACHE_FUZZY_THRESHOLD, GEOCODE_CACHE_SNAPSHOT_TTL_SECONDS
from app.db.clients import get_fs_client

logger = logging.getLogger(__name__)

GEOCODE_CACHE_COLLECTION = "geocode_cache"

CITY_VARIANTS = {
    "bangalore": "bengaluru",
    "sarjapur": "sarjapura",
    "amblipura": "ambalipura",
    # Expand this from real spelling variants found in your own address data
    # (the ~30% address-repeat-rate finding is the right source to mine this from).
}

PINCODE_RE = re.compile(r"\b\d{6}\b")

# Everything places_search_address returns, minus pincode_match, which is
# recomputed per lookup against the address actually being geocoded.
CACHED_RESULT_FIELDS = (
    "latitude",
    "longitude",
    "pincode",
    "locality",
    "area",
    "location_type",
    "partial_match",
    "types",
    "formatted_address",
    "place_id",
    "street_number",
    "route_name",
    "district",
    "state",
    "country_code",
)

_snapshot = {"loaded_at": 0.0, "entries": None}


def normalize_address(address: str) -> str:
    if not address:
        return ""
    s = address.lower().strip()
    s = re.sub(r"[,\s]+", " ", s)
    s = PINCODE_RE.sub("", s)  # strip any 6-digit pincode, not just 560xxx
    for variant, canonical in CITY_VARIANTS.items():
        s = re.sub(r"\b" + re.escape(variant) + r"\b", canonical, s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def cache_key(normalized: str) -> str:
    return hashlib.sha256(f"v1|{normalized}".encode("utf-8")).hexdigest()[:40]


def _collection():
    return get_fs_client().collection(GEOCODE_CACHE_COLLECTION)


def _verified_entries():
    """Verified cache entries, loaded once per process and reused for the TTL.

    The original streamed the whole collection for every consignment; a 500-row
    DRS against a 10k-entry cache was 5M document reads per batch.
    """
    now = time.monotonic()
    if (
        _snapshot["entries"] is not None
        and now - _snapshot["loaded_at"] < GEOCODE_CACHE_SNAPSHOT_TTL_SECONDS
    ):
        return _snapshot["entries"]

    docs = list(_collection().where("verified", "==", True).stream())
    entries = [(doc.id, doc.to_dict() or {}) for doc in docs]
    _snapshot["entries"] = entries
    _snapshot["loaded_at"] = now
    logger.info("Loaded %d verified geocode cache entry(ies).", len(entries))
    return entries


def invalidate_snapshot():
    _snapshot["entries"] = None
    _snapshot["loaded_at"] = 0.0


def _exact_lookup(normalized: str):
    docs = list(
        _collection()
        .where("address_normalized", "==", normalized)
        .where("verified", "==", True)
        .limit(1)
        .stream()
    )
    if not docs:
        return None
    return docs[0].id, (docs[0].to_dict() or {})


def _fuzzy_lookup(normalized: str):
    entries = _verified_entries()
    if not entries:
        return None

    candidates = {doc_id: data.get("address_normalized") or "" for doc_id, data in entries}
    match = process.extractOne(
        normalized,
        candidates,
        scorer=fuzz.token_sort_ratio,
        processor=utils.default_process,
        score_cutoff=GEOCODE_CACHE_FUZZY_THRESHOLD,
    )
    if not match:
        return None  # cache miss -> caller falls through to places_search_address()

    _matched_text, score, doc_id = match
    data = next(d for i, d in entries if i == doc_id)
    logger.info("Fuzzy geocode cache hit (score=%.1f) for '%s'", score, normalized[:60])
    return doc_id, data


def _result_from_cache(data: dict, address: str) -> dict:
    result = {field: data.get(field) for field in CACHED_RESULT_FIELDS}
    result["types"] = result.get("types") or []
    result["partial_match"] = bool(result.get("partial_match"))

    requested_pin_match = PINCODE_RE.search(address or "")
    requested_pin = requested_pin_match.group(0) if requested_pin_match else None
    cached_pin = result.get("pincode")
    result["pincode_match"] = not (requested_pin and cached_pin and requested_pin != cached_pin)
    return result


def get_cached_geocode(address: str) -> Optional[dict]:
    """Best-match lookup against verified cache entries, or None on a miss."""
    normalized = normalize_address(address)
    if not normalized:
        return None

    hit = _exact_lookup(normalized) or _fuzzy_lookup(normalized)
    if not hit:
        return None

    doc_id, data = hit
    try:
        _collection().document(doc_id).update({"hit_count": (data.get("hit_count") or 0) + 1})
    except Exception:
        logger.exception("Geocode cache hit_count update failed")

    return _result_from_cache(data, address)


def save_to_cache(address: str, geocode_result: dict) -> None:
    normalized = normalize_address(address)
    if not normalized or not geocode_result:
        return

    payload = {field: geocode_result.get(field) for field in CACHED_RESULT_FIELDS}
    payload["types"] = payload.get("types") or []
    payload["partial_match"] = bool(payload.get("partial_match"))
    payload.update({
        "address_raw": address,
        "address_normalized": normalized,
        "source": "api",
        "updated_at": firestore.SERVER_TIMESTAMP,
    })

    doc_ref = _collection().document(cache_key(normalized))
    # An existing entry keeps its ops verdict and hit count; only the geocode
    # payload is refreshed.
    if doc_ref.get().exists:
        doc_ref.update(payload)
        return

    doc_ref.set({
        **payload,
        "verified": False,
        "verified_by": None,
        "verified_at": None,
        "hit_count": 0,
        "created_at": firestore.SERVER_TIMESTAMP,
    })
