"""Feature-flag reader over the 3PL `featureFlags` Firestore collection.

Same Firestore project as geocode_cache, so no new collection is created.
Each flag is one document whose `enabled` field must be exactly True. Values
are cached for FEATURE_FLAG_TTL_SECONDS (default 45 s); a missing document or
any read error counts as OFF, so a Firestore problem can never switch new
behaviour on.
"""

import logging
import time

from app.config import FEATURE_FLAG_TTL_SECONDS, FEATURE_FLAGS_COLLECTION
from app.db.clients import get_fs_client

logger = logging.getLogger(__name__)

FLAG_GEOCODE_RESOLUTION_V2 = "geocodeResolutionV2"

_snapshot = {"loaded_at": 0.0, "flags": None}


def _load_snapshot() -> dict:
    """{doc_id: data} for the whole collection, reused for the TTL (same idiom
    as geocache._verified_entries). On error, keeps the last good snapshot if
    there is one, else returns {} (everything off)."""
    now = time.monotonic()
    if _snapshot["flags"] is not None and now - _snapshot["loaded_at"] < FEATURE_FLAG_TTL_SECONDS:
        return _snapshot["flags"]
    try:
        docs = get_fs_client().collection(FEATURE_FLAGS_COLLECTION).stream()
        flags = {doc.id: (doc.to_dict() or {}) for doc in docs}
    except Exception:
        logger.exception("Feature flag read failed; treating flags as off")
        # Retry after the TTL rather than hammering Firestore on every call.
        _snapshot["loaded_at"] = now
        _snapshot["flags"] = {}
        return {}
    _snapshot["flags"] = flags
    _snapshot["loaded_at"] = now
    return flags


def is_enabled(flag_id: str) -> bool:
    try:
        return (_load_snapshot().get(flag_id) or {}).get("enabled") is True
    except Exception:
        logger.exception("Feature flag check failed for %s", flag_id)
        return False


def geocode_resolution_v2() -> bool:
    return is_enabled(FLAG_GEOCODE_RESOLUTION_V2)


def reset_cache():
    """For tests."""
    _snapshot["flags"] = None
    _snapshot["loaded_at"] = 0.0
