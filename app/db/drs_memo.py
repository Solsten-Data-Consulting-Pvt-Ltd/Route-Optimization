"""DRS address memo - reuse a pin for same-place consignments in one DRS.

Why: `geocode_cache` only serves `verified` entries, and executives verify at
the END of a DRS. So during the day, parcel #3 to the same receiver as parcel
#1 misses the cache and calls Places again. The memo remembers every pin
resolved in this DRS so later same-place consignments reuse it.

Storage: ONE Firestore document per DRS,

    drs_address_memo/{drsId}
        drs_id, updated_at, expires_at       (TTL policy on expires_at)
        entries: {
          <entry_id>: {
            latitude, longitude, ...          same fields as geocode_cache
            match:  {match_address, match_key, phones, pincode, locality_words}
            source: exec_corrected | exec_accepted | global_cache | api
            variants:        [receiver.address strings that used this pin]
            consignment_ids: [...]
          }
        }

so a scan costs one document read. Entries are written with set(merge=True)
on distinct map keys, so concurrent scans in the same DRS do not clobber each
other.

Matching rules live in `app.services.address_match.drs_match`.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from google.cloud import firestore

from app.config import DRS_MEMO_COLLECTION, DRS_MEMO_TTL_DAYS
from app.db.clients import get_fs_client
from app.db.geocache import CACHED_RESULT_FIELDS, _result_from_cache
from app.services.address_match import drs_match

logger = logging.getLogger(__name__)

SOURCE_EXEC_CORRECTED = "exec_corrected"
SOURCE_EXEC_ACCEPTED = "exec_accepted"
SOURCE_GLOBAL_CACHE = "global_cache"
SOURCE_API = "api"

# Higher wins; a lower-priority source never overwrites a higher one.
_SOURCE_PRIORITY = {
    SOURCE_API: 0,
    SOURCE_GLOBAL_CACHE: 1,
    SOURCE_EXEC_ACCEPTED: 2,
    SOURCE_EXEC_CORRECTED: 3,
}
_REASON_RANK = {"phone": 0, "exact": 1, "contained": 2, "fuzzy": 3}

MATCH_FIELDS = ("match_address", "match_key", "phones", "pincode", "locality_words")


def _doc(drs_id):
    return get_fs_client().collection(DRS_MEMO_COLLECTION).document(str(drs_id))


def group_id(drs_id, entry_id) -> str:
    """Links geocode_cache variant entries to one memo entry (EOD verification)."""
    return f"{drs_id}:{entry_id}"


def load_entries(drs_id) -> dict:
    snap = _doc(drs_id).get()
    if not snap.exists:
        return {}
    return (snap.to_dict() or {}).get("entries") or {}


def find_match(entries: dict, info: dict, sources=None) -> Optional[Tuple[str, dict, str]]:
    """Best (entry_id, entry, reason) for `info`, or None.

    Executive-corrected entries win, then phone > exact > contained > fuzzy.
    """
    best = None
    for entry_id, entry in (entries or {}).items():
        source = entry.get("source")
        if sources and source not in sources:
            continue
        if entry.get("latitude") is None or entry.get("longitude") is None:
            continue
        reason = drs_match(info, entry.get("match") or {})
        if not reason:
            continue
        rank = (-_SOURCE_PRIORITY.get(source, 0), _REASON_RANK.get(reason, 9))
        if best is None or rank < best[0]:
            best = (rank, entry_id, entry, reason)
    return (best[1], best[2], best[3]) if best else None


def result_from_entry(entry: dict, address: str) -> dict:
    """Same shape as a geocode_cache hit; pincode_match recomputed for `address`."""
    return _result_from_cache(entry, address)


def remember(drs_id, info: dict, result: dict, source: str, *,
             lookup_address: Optional[str] = None,
             consignment_id: Optional[str] = None,
             entries: Optional[dict] = None) -> Optional[str]:
    """Store `result` as this consignment's memo entry. Returns the entry id.

    `entries` (already-loaded memo) avoids a second read; if omitted it is
    loaded. An entry is never downgraded (e.g. an API pin never replaces an
    executive correction) - in that case only variants/ids are appended.
    """
    entry_id = (info or {}).get("entry_id")
    if not drs_id or not entry_id or not result:
        return None
    if result.get("latitude") is None or result.get("longitude") is None:
        return None

    if entries is None:
        entries = load_entries(drs_id)
    existing = entries.get(entry_id)
    if existing and _SOURCE_PRIORITY.get(existing.get("source"), 0) > _SOURCE_PRIORITY.get(source, 0):
        link(drs_id, entry_id, lookup_address=lookup_address, consignment_id=consignment_id)
        return entry_id

    entry = {field: result.get(field) for field in CACHED_RESULT_FIELDS}
    entry["types"] = entry.get("types") or []
    entry["partial_match"] = bool(entry.get("partial_match"))
    entry["match"] = {field: info.get(field) for field in MATCH_FIELDS}
    entry["source"] = source
    entry["updated_at"] = firestore.SERVER_TIMESTAMP
    if lookup_address:
        entry["variants"] = firestore.ArrayUnion([lookup_address])
    if consignment_id:
        entry["consignment_ids"] = firestore.ArrayUnion([consignment_id])

    _doc(drs_id).set({
        "drs_id": str(drs_id),
        "updated_at": firestore.SERVER_TIMESTAMP,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=DRS_MEMO_TTL_DAYS),
        "entries": {entry_id: entry},
    }, merge=True)
    return entry_id


def link(drs_id, entry_id, *, lookup_address=None, consignment_id=None) -> None:
    """Record that another consignment reused this entry's pin."""
    update = {}
    if lookup_address:
        update["variants"] = firestore.ArrayUnion([lookup_address])
    if consignment_id:
        update["consignment_ids"] = firestore.ArrayUnion([consignment_id])
    if not update:
        return
    _doc(drs_id).set({
        "updated_at": firestore.SERVER_TIMESTAMP,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=DRS_MEMO_TTL_DAYS),
        "entries": {entry_id: update},
    }, merge=True)
