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

Three improvements over the original port:

  Fix 1 — Address-only cache lookup key.
     `get_cached_geocode` and `save_to_cache` now accept an optional
     `lookup_address` parameter (the raw receiver address without the
     receiver name prefix).  Cache lookup and the Firestore document key both
     use the normalized form of `lookup_address` when it is provided.  The
     full `geocode_address` (name + address) is still sent to the Places API
     and stored as `geocode_address_raw` for traceability, but the cache key
     is keyed on location only.  This means two consignments for different
     people at the same building (e.g. different employees at Shahi Exports)
     share one cache entry instead of each causing a fresh API call.

  Fix 2 — Google Plus Code stripping.
     Plus Codes (e.g. "VPMX+F5R") that appear in raw address strings are
     stripped by `normalize_address` before fuzzy comparison.  They are
     meaningless tokens that would otherwise cause a genuine location match
     to score below the threshold.

  Fix 3 — Building-name abbreviation normalisation.
     A small `BUILDING_VARIANTS` table (analogous to `CITY_VARIANTS`) maps
     known abbreviation patterns to their canonical forms so that e.g.
     "R K Complex" and "RK Com" normalise to the same token.
"""

import hashlib
import logging
import re
import time
from typing import Optional, Tuple

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

# Fix 3 — building-name abbreviations.
# Keyed on the LONGER/spaced form so the substitution always goes from verbose
# to compact; order does not matter because each key is matched independently.
BUILDING_VARIANTS = {
    "r k complex": "rk com",
    "domasandra":  "dommasandra",   # common missing-m typo seen in the wild
}

PINCODE_RE = re.compile(r"\b\d{6}\b")

# Fix 2 — Google Plus Codes (e.g. "VPMX+F5R", "9F2X+4C").
# Format: 4-8 uppercase alphanumeric chars (excluding vowels/1), a "+",
# then 2-3 more.  Strip them before fuzzy comparison — they are opaque
# location tokens that match nothing in a human-written address string.
PLUS_CODE_RE = re.compile(
    r"\b[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}\b",
    re.IGNORECASE,
)

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
    s = PINCODE_RE.sub("", s)      # strip any 6-digit pincode, not just 560xxx
    s = PLUS_CODE_RE.sub("", s)    # Fix 2: strip Google Plus Codes
    for variant, canonical in CITY_VARIANTS.items():
        s = re.sub(r"\b" + re.escape(variant) + r"\b", canonical, s)
    # Fix 3: normalise building-name abbreviations (longer form -> compact form)
    for verbose, compact in BUILDING_VARIANTS.items():
        s = s.replace(verbose, compact)
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


def get_cached_geocode_with_outcome(
    address: str,
    lookup_address: Optional[str] = None,
) -> Tuple[Optional[dict], str]:
    """Return a cached result and its outcome: exact_hit, fuzzy_hit, or miss.

    Fix 1 — `lookup_address` is the raw receiver address WITHOUT the receiver
    name prefix.  When provided, the cache lookup key is derived from
    `lookup_address` so that deliveries to the same building for different
    recipients (e.g. different employees at Shahi Exports) still get a hit.
    `address` (full geocode string: name + address) is only used to recompute
    `pincode_match` on a hit and is not used as the lookup key.

    If `lookup_address` is omitted the behaviour is identical to before.
    """
    key_str = lookup_address if lookup_address else address
    normalized = normalize_address(key_str)
    if not normalized:
        return None, "miss"

    hit = _exact_lookup(normalized)
    outcome = "exact_hit"
    if not hit:
        hit = _fuzzy_lookup(normalized)
        outcome = "fuzzy_hit"
    if not hit:
        return None, "miss"

    doc_id, data = hit
    try:
        _collection().document(doc_id).update({
            "hit_count": firestore.Increment(1),
            "last_hit_at": firestore.SERVER_TIMESTAMP,
        })
    except Exception:
        logger.exception("Geocode cache hit_count update failed")

    return _result_from_cache(data, address), outcome


def get_cached_geocode(
    address: str,
    lookup_address: Optional[str] = None,
) -> Optional[dict]:
    """Best-match lookup against verified cache entries, or None on a miss.

    This compatibility wrapper preserves the original public cache API. Save
    pipeline analytics use ``get_cached_geocode_with_outcome`` to distinguish
    exact and fuzzy reuse without changing any existing callers.
    """
    result, _outcome = get_cached_geocode_with_outcome(address, lookup_address)
    return result


def save_to_cache(
    address: str,
    geocode_result: dict,
    lookup_address: Optional[str] = None,
    source: str = "api",
    drs_memo_group: Optional[str] = None,
) -> None:
    """Persist a geocode result to the cache.

    Fix 1 — `lookup_address` is the raw receiver address WITHOUT the receiver
    name prefix.  When provided, both the Firestore document key and
    `address_normalized` are derived from `lookup_address` so that future
    consignments for different recipients at the same location can find this
    entry.  `address` (full geocode string sent to the API) is stored as
    `geocode_address_raw` for traceability.

    If `lookup_address` is omitted the behaviour is identical to before.

    `source` is "api" for a fresh Places result or "drs_memo" when the pin was
    reused from another consignment in the same DRS. `drs_memo_group` links
    all address variants that share one DRS-memo pin, so end-of-day
    verification can verify them together (see `verify_cache_group`).
    """
    key_str = lookup_address if lookup_address else address
    normalized = normalize_address(key_str)
    if not normalized or not geocode_result:
        return

    payload = {field: geocode_result.get(field) for field in CACHED_RESULT_FIELDS}
    payload["types"] = payload.get("types") or []
    payload["partial_match"] = bool(payload.get("partial_match"))
    payload.update({
        "address_raw":         address,     # full geocode string (name + address)
        "geocode_address_raw": address,     # alias kept for clarity
        "address_normalized":  normalized,  # Fix 1: keyed on location only
        "source": source,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    if drs_memo_group:
        payload["drs_memo_group"] = drs_memo_group

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


def verify_cache_group(drs_memo_group: str, verified_by: str) -> int:
    """Mark every geocode_cache entry in one DRS-memo group as verified.

    For the end-of-DRS verification step: when an executive verifies one
    address, the other spellings of it seen in the same DRS (which reused its
    pin via the DRS memo) become servable too. Returns the number updated.
    """
    if not drs_memo_group:
        return 0
    docs = list(
        _collection().where("drs_memo_group", "==", drs_memo_group).stream()
    )
    if not docs:
        return 0
    batch = get_fs_client().batch()
    for doc in docs:
        batch.update(doc.reference, {
            "verified": True,
            "verified_by": verified_by,
            "verified_at": firestore.SERVER_TIMESTAMP,
        })
    batch.commit()
    invalidate_snapshot()
    return len(docs)
