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
     `lookup_address` parameter (the raw receiver address; the geocode
     string is now address-only too).  Cache lookup and the Firestore document key both
     use the normalized form of `lookup_address` when it is provided.  The
     full `geocode_address` (now address-only) is still sent to the Places API
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
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from rapidfuzz import fuzz, process, utils

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from app.config import (
    GEOCODE_CACHE_FUZZY_THRESHOLD,
    GEOCODE_CACHE_SNAPSHOT_TTL_SECONDS,
    GEOCODE_CACHE_UNVERIFIED_TTL_DAYS,
)
from app.db.clients import get_fs_client
from app.services.address_match import (
    building_name_conflict, door_numbers, is_precise, loose_key, match_key,
)

logger = logging.getLogger(__name__)

GEOCODE_CACHE_COLLECTION = "geocode_cache"
VERIFY_BATCH_SIZE = 450   # Firestore batches cap at 500 writes

CITY_VARIANTS = {
    "bangalore": "bengaluru",
    "sarjapur": "sarjapura",
    "amblipura": "ambalipura",
    "ambaipura": "ambalipura",   # OCR drops the 'l'
    "benglore": "bengaluru",
    "belandur": "bellandur",
    "ilandur": "bellandur",
    "kasarjpura": "sarjapura",
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


# Company-name spelling variants. Mirrored in 3PL geocodeAddress.js.
COMPANY_VARIANTS = (
    (re.compile(r"[\[(]\s*p\s*[\])]"), "pvt"),
    (re.compile(r"\bpvt\b\.?"), "pvt"),
    (re.compile(r"\bprivate\b"), "pvt"),
    (re.compile(r"\bltd\b\.?"), "ltd"),
    (re.compile(r"\blimited\b"), "ltd"),
    (re.compile(r"\bexports\b"), "export"),
    (re.compile(r"\bimports\b"), "import"),
    (re.compile(r"\benterprises\b"), "enterprise"),
    (re.compile(r"\bindustries\b"), "industry"),
)


SLASH_SPACE_RE = re.compile(r"(?<=[0-9a-z])\s*/\s*(?=[0-9a-z])")


def normalize_address(address: str) -> str:
    if not address:
        return ""
    s = address.lower().strip()
    s = re.sub(r"[,\s]+", " ", s)
    s = PINCODE_RE.sub("", s)      # strip any 6-digit pincode, not just 560xxx
    s = PLUS_CODE_RE.sub("", s)    # Fix 2: strip Google Plus Codes
    s = SLASH_SPACE_RE.sub("/", s)     # "18 / 2a" == "18/2a"
    for pattern, canonical in COMPANY_VARIANTS:
        s = pattern.sub(canonical, s)
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


def distance_meters(latitude_a, longitude_a, latitude_b, longitude_b) -> Optional[float]:
    """Great-circle distance, or None when either point is incomplete."""
    values = (latitude_a, longitude_a, latitude_b, longitude_b)
    if not all(isinstance(value, (int, float)) for value in values):
        return None
    from math import asin, cos, radians, sin, sqrt

    delta_lat = radians(latitude_b - latitude_a)
    delta_lng = radians(longitude_b - longitude_a)
    a = (
        sin(delta_lat / 2) ** 2
        + cos(radians(latitude_a)) * cos(radians(latitude_b)) * sin(delta_lng / 2) ** 2
    )
    return 6_371_000 * 2 * asin(sqrt(a))


def _record_variant(doc_ref, normalized: str) -> None:
    """Remember a spelling without changing the cache entry's identity."""
    if not normalized:
        return
    doc_ref.update({
        "address_normalized_variants": firestore.ArrayUnion([normalized]),
        "updated_at": firestore.SERVER_TIMESTAMP,
    })


def get_cache_entry(cache_id: str):
    """Return an entry by its Firestore id, including unverified entries."""
    if not cache_id:
        return None
    snap = _collection().document(cache_id).get()
    return (snap.id, snap.to_dict() or {}) if snap.exists else None


def find_cache_entry(normalized: str):
    """Find any cache entry for an exact spelling or its variants.

    Unlike `_exact_lookup`, this intentionally includes unverified entries for
    the cache-owner API. Serving geocodes remains verified-only.
    """
    if not normalized:
        return None
    docs = list(_collection().where("address_normalized", "==", normalized).limit(1).stream())
    if not docs:
        docs = list(
            _collection()
            .where("address_normalized_variants", "array_contains", normalized)
            .limit(1)
            .stream()
        )
    if not docs:
        return None
    return docs[0].id, (docs[0].to_dict() or {})


def find_cache_entry_by_place_id_near(place_id: str, result: dict, radius_meters=50):
    """One place-id represents one Google location; reuse it only nearby."""
    if not place_id or not result:
        return None
    latitude, longitude = result.get("latitude"), result.get("longitude")
    for doc in _collection().where("place_id", "==", place_id).stream():
        data = doc.to_dict() or {}
        distance = distance_meters(latitude, longitude, data.get("latitude"), data.get("longitude"))
        if distance is not None and distance <= radius_meters:
            return doc.id, data
    return None


def _expiry_fields(existing: Optional[dict] = None) -> dict:
    """`expire_at` for an UNVERIFIED doc; {} when the existing doc is verified.

    A Firestore TTL policy on `expire_at` deletes unverified entries that nobody
    verified (e.g. a test DRS that was never delivered). The deadline is pushed
    out on every write, so an address that keeps coming back stays alive.
    Verified docs never carry `expire_at` (verify_cache_group removes it), and
    a write to an already-verified doc must not add one back.
    """
    if existing and existing.get("verified"):
        return {}
    return {
        "expire_at": datetime.now(timezone.utc)
        + timedelta(days=GEOCODE_CACHE_UNVERIFIED_TTL_DAYS)
    }


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
    if docs:
        return docs[0].id, (docs[0].to_dict() or {})

    # A consolidated multi-spelling entry (see save_to_cache's drs_memo_group
    # reuse below) stores every OTHER spelling it has absorbed here, since
    # its own address_normalized only ever holds the first/canonical one.
    docs = list(
        _collection()
        .where("address_normalized_variants", "array_contains", normalized)
        .where("verified", "==", True)
        .limit(1)
        .stream()
    )
    if not docs:
        return None
    return docs[0].id, (docs[0].to_dict() or {})

def _is_safe_fuzzy_match(incoming: str, cached: str) -> bool:
    """A fuzzy text score cannot overrule a different building number or name.

    Vague road-only strings are equally unsafe: a high token score there means
    only that two labels name the same road, not the same delivery point.
    """
    incoming_key = loose_key(match_key(incoming))
    cached_key = loose_key(match_key(cached))
    if not is_precise(incoming_key) or not is_precise(cached_key):
        return False
    incoming_doors = door_numbers(incoming_key)
    cached_doors = door_numbers(cached_key)
    if (
        incoming_doors
        and cached_doors
        and not (incoming_doors <= cached_doors or cached_doors <= incoming_doors)
    ):
        return False
    # Same road and city is not the same building: both labels naming
    # different buildings (RBD Still Water vs Purva Skydale) is no match.
    return not building_name_conflict(incoming_key, cached_key)


def _fuzzy_lookup(normalized: str):
    entries = _verified_entries()
    if not entries:
        return None

    candidates = {doc_id: data.get("address_normalized") or "" for doc_id, data in entries}
    matches = process.extract(
        normalized,
        candidates,
        scorer=fuzz.token_sort_ratio,
        processor=utils.default_process,
        score_cutoff=GEOCODE_CACHE_FUZZY_THRESHOLD,
    )
    for _matched_text, score, doc_id in matches:
        data = next(d for i, d in entries if i == doc_id)
        if not _is_safe_fuzzy_match(normalized, data.get("address_normalized") or ""):
            continue
        logger.info("Fuzzy geocode cache hit (score=%.1f) for '%s'", score, normalized[:60])
        return doc_id, data
    return None  # cache miss -> caller falls through to places_search_address()


def _label_evidence_pincodes(data: dict) -> set:
    """Pincodes this entry already has evidence for.

    `label_pincodes` is learned from deliveries, so an entry that was verified
    before it existed has none. For a VERIFIED entry (a person confirmed the
    pin) the pincode written in its own address text counts as evidence too.
    Spellings in address_normalized(_variants) cannot be used: normalize_address
    strips pincodes, so only the raw / formatted address still carries one.
    """
    pins = {str(pin) for pin in (data.get("label_pincodes") or []) if pin}
    if data.get("verified") is True:
        for field in ("address_raw", "formatted_address"):
            pins.update(PINCODE_RE.findall(str(data.get(field) or "")))
        if data.get("pincode"):
            pins.add(str(data["pincode"]))
    return pins


def _result_from_cache(data: dict, address: str, *, outcome=None, normalized=None) -> dict:
    result = {field: data.get(field) for field in CACHED_RESULT_FIELDS}
    result["types"] = result.get("types") or []
    result["partial_match"] = bool(result.get("partial_match"))

    requested_pin_match = PINCODE_RE.search(address or "")
    requested_pin = requested_pin_match.group(0) if requested_pin_match else None
    cached_pin = result.get("pincode")
    label_pincodes = _label_evidence_pincodes(data)
    lookup_key = normalized or data.get("address_normalized") or ""
    precise = is_precise(loose_key(match_key(lookup_key)))
    if outcome == "fuzzy_hit":
        # A similar spelling needs prior label evidence for this pincode.
        result["pincode_match"] = not requested_pin or requested_pin in label_pincodes
    elif outcome == "exact_hit" and not precise:
        # Exact road-only text is safe only after this label pincode has been
        # seen on a delivery; building-level exact text does not nag ops.
        result["pincode_match"] = not requested_pin or requested_pin in label_pincodes
    elif outcome == "exact_hit":
        # Exact, building-level text is stronger evidence than a pincode
        # disagreement in an address label; do not create a review badge.
        result["pincode_match"] = True
    else:
        result["pincode_match"] = not (requested_pin and cached_pin and requested_pin != cached_pin)
    return result


def get_cached_geocode_with_outcome(
    address: str,
    lookup_address: Optional[str] = None,
    include_cache_id: bool = False,
) -> Tuple[Optional[dict], str]:
    """Return a cached result and its outcome: exact_hit, fuzzy_hit, or miss.

    Fix 1 — `lookup_address` is the raw receiver address WITHOUT the receiver
    name prefix.  When provided, the cache lookup key is derived from
    `lookup_address` so that deliveries to the same building for different
    recipients (e.g. different employees at Shahi Exports) still get a hit.
    `address` (full geocode string) is only used to recompute
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
        if outcome == "fuzzy_hit":
            _record_variant(_collection().document(doc_id), normalized)
    except Exception:
        logger.exception("Geocode cache hit_count update failed")

    result = _result_from_cache(data, address, outcome=outcome, normalized=normalized)
    return (result, outcome, doc_id) if include_cache_id else (result, outcome)


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


def _docs_by_group(drs_memo_group: str):
    """Every geocode_cache doc tagged with this drs_memo_group - normally 0
    (first spelling of a new place) or 1 (every later spelling of a place the
    DRS memo already resolved earlier today reuses that same doc instead of
    writing its own).

    A doc has ONE creating group (scalar `drs_memo_group`) plus every group
    that has since written to it (`drs_memo_groups`). Two memo groups can
    normalise to the same cache key (e.g. "Unit 17A, 18/2A ..." and "Unit 17
    ..." both end up as "ambalipura sarjapura road ..."); the later group must
    not steal the doc's tag, or verifying the earlier group would verify
    nothing. Docs written before `drs_memo_groups` existed only have the
    scalar, which is still queried.
    """
    docs = {d.id: d for d in _collection().where("drs_memo_group", "==", drs_memo_group).stream()}
    for d in _collection().where("drs_memo_groups", "array_contains", drs_memo_group).stream():
        docs.setdefault(d.id, d)
    return list(docs.values())


def _label_pincodes(address: str, result: dict) -> list[str]:
    """Pincodes actually observed with this cache label.

    The Google result can have a locality pincode that is broader than the
    receiver's typed label. Keep both, but never invent a label pincode from
    normalization (which intentionally removes it).
    """
    values = set(PINCODE_RE.findall(address or ""))
    if result and result.get("pincode"):
        values.add(str(result["pincode"]))
    return sorted(values)


def save_to_cache(
    address: str,
    geocode_result: dict,
    lookup_address: Optional[str] = None,
    source: str = "api",
    drs_memo_group: Optional[str] = None,
) -> Optional[str]:
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
        return None

    payload = {field: geocode_result.get(field) for field in CACHED_RESULT_FIELDS}
    payload["types"] = payload.get("types") or []
    payload["partial_match"] = bool(payload.get("partial_match"))
    payload.update({
        "address_raw":         address,     # full geocode string (address-only)
        "geocode_address_raw": address,     # alias kept for clarity
        "address_normalized":  normalized,  # Fix 1: keyed on location only
        "source": source,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    label_pincodes = _label_pincodes(address, geocode_result)
    label_update = (
        {"label_pincodes": firestore.ArrayUnion(label_pincodes)}
        if label_pincodes else {}
    )
    # The group tag is NOT part of `payload`: an existing doc keeps its creating
    # group and only gains this one in `drs_memo_groups` (see _docs_by_group).
    group_update = (
        {"drs_memo_groups": firestore.ArrayUnion([drs_memo_group])} if drs_memo_group else {}
    )
    if drs_memo_group:
        # Reuse a sibling entry the DRS memo already tagged with this same
        # group (see drs_memo.py & address_match.py::drs_match - it can
        # match two spellings that don't even look similar as text, e.g. via
        # a shared phone number) rather than writing yet another row keyed
        # on this spelling's own hash. The group is a cheaper, already-made
        # "same place" decision - this doesn't need to re-derive it from
        # text similarity, which is all cache_key()/_fuzzy_lookup() alone
        # could ever do.
        siblings = _docs_by_group(drs_memo_group)
        if siblings:
            # address_normalized/address_raw/geocode_address_raw are left
            # OUT of this update on purpose. A Firestore document's id is
            # fixed at creation - here, cache_key() of whichever spelling
            # this doc was FIRST written under - and can never change to
            # match a later spelling. Overwriting the scalar
            # address_normalized field to this spelling's text would make
            # it claim an id it doesn't actually have, so a later, ungrouped
            # write for that exact same text (see _exact_lookup/save below)
            # would compute cache_key() of it, find nothing at THAT id, and
            # wrongly create a duplicate doc instead of finding this one.
            # Only the array (which _exact_lookup also checks) is grown.
            sibling_payload = {
                k: v for k, v in payload.items()
                if k not in ("address_normalized", "address_raw", "geocode_address_raw")
            }
            siblings[0].reference.update({
                **sibling_payload,
                **group_update,
                **label_update,
                **_expiry_fields(siblings[0].to_dict()),
                "address_normalized_variants": firestore.ArrayUnion([normalized]),
            })
            return siblings[0].id

    doc_ref = _collection().document(cache_key(normalized))
    # An existing entry keeps its ops verdict and hit count; only the geocode
    # payload is refreshed.
    existing = doc_ref.get()
    if existing.exists:
        doc_ref.update({**payload, **group_update, **label_update,
                        **_expiry_fields(existing.to_dict())})
        _record_variant(doc_ref, normalized)
        return doc_ref.id

    # An entry with this exact normalized text may live under a DIFFERENT id: a
    # legacy auto-id doc from before entries were hash-keyed, or one written
    # under the spelling rules of its day. Update that one instead of adding a
    # second doc for the same text. (Any verified state, unlike _exact_lookup.)
    same_text = list(
        _collection()
        .where("address_normalized", "==", normalized)
        .limit(1)
        .stream()
    )
    if same_text:
        text_payload = {
            k: v for k, v in payload.items()
            if k not in ("address_normalized", "address_raw", "geocode_address_raw")
        }
        same_text[0].reference.update(
            {**text_payload, **group_update, **label_update,
             **_expiry_fields(same_text[0].to_dict())}
        )
        _record_variant(same_text[0].reference, normalized)
        return same_text[0].id

    # This exact spelling has no doc of its own - but it may already be a
    # recorded VARIANT of another doc (absorbed there under a drs_memo_group
    # by an earlier write, possibly one this call has no group for - e.g. a
    # DRS-unaware preview geocode running after a grouped save already
    # claimed this text). That other doc's real id is whatever ITS first
    # spelling hashed to, which is never `cache_key(normalized)` for a
    # variant spelling - so without this check, every such write would
    # wrongly create a fresh duplicate here every single time.
    variant_matches = list(
        _collection()
        .where("address_normalized_variants", "array_contains", normalized)
        .limit(1)
        .stream()
    )
    if variant_matches:
        sibling_payload = {
            k: v for k, v in payload.items()
            if k not in ("address_normalized", "address_raw", "geocode_address_raw")
        }
        variant_matches[0].reference.update(
            {**sibling_payload, **group_update, **label_update,
             **_expiry_fields(variant_matches[0].to_dict())}
        )
        _record_variant(variant_matches[0].reference, normalized)
        return variant_matches[0].id

    # Places occasionally returns several spellings for exactly the same
    # physical location. Avoid one cache doc per spelling when place-id and
    # coordinates agree; record this spelling as a variant of the owner.
    same_place = find_cache_entry_by_place_id_near(
        geocode_result.get("place_id"), geocode_result,
    )
    if same_place:
        existing_id, existing_data = same_place
        owner_ref = _collection().document(existing_id)
        owner_payload = {
            key: value for key, value in payload.items()
            if key not in ("address_normalized", "address_raw", "geocode_address_raw")
        }
        owner_ref.update({
            **owner_payload,
            **group_update,
            **label_update,
            **_expiry_fields(existing_data),
            "address_normalized_variants": firestore.ArrayUnion([normalized]),
        })
        return existing_id

    # create() (not set()): if another request wrote this same id after the
    # exists() check above, set() would replace that doc wholesale and reset its
    # verified flag and hit_count. create() refuses, and we refresh it instead.
    try:
        doc_ref.create({
            **payload,
            **({"drs_memo_group": drs_memo_group, "drs_memo_groups": [drs_memo_group]}
               if drs_memo_group else {}),
            "address_normalized_variants": [normalized],
            "label_pincodes": label_pincodes,
            "verified": False,
            "verified_by": None,
            "verified_at": None,
            "hit_count": 0,
            "created_at": firestore.SERVER_TIMESTAMP,
            **_expiry_fields(),
        })
        return doc_ref.id
    except AlreadyExists:
        doc_ref.update({**payload, **group_update, **label_update,
                        **_expiry_fields(doc_ref.get().to_dict())})
        _record_variant(doc_ref, normalized)
        return doc_ref.id


def verify_cache_group(drs_memo_group: str, verified_by: str) -> int:
    """Mark every geocode_cache entry in one DRS-memo group as verified.

    For the end-of-DRS verification step: when an executive verifies one
    address, the other spellings of it seen in the same DRS (which reused its
    pin via the DRS memo) become servable too. Returns the number updated.
    """
    if not drs_memo_group:
        return 0
    docs = _docs_by_group(drs_memo_group)
    if not docs:
        return 0
    # A Firestore batch holds at most 500 writes; a large DRS (500+ places in
    # one group chain) must be committed in chunks or the whole verify fails.
    for start in range(0, len(docs), VERIFY_BATCH_SIZE):
        batch = get_fs_client().batch()
        for doc in docs[start:start + VERIFY_BATCH_SIZE]:
            batch.update(doc.reference, {
                "verified": True,
                "verified_by": verified_by,
                "verified_at": firestore.SERVER_TIMESTAMP,
                "expire_at": firestore.DELETE_FIELD,   # verified docs never expire
            })
        batch.commit()
    invalidate_snapshot()
    return len(docs)
