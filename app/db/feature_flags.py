"""Dynamic feature-flag / config store — see .specify/spec.md §5.6.

Static values that don't change at runtime (project IDs, table names, API
keys) stay in app/config.py as plain env vars, same as always. Anything
*behavioral* — a feature on/off switch, a threshold — lives here instead,
in Firestore, so it can be flipped without a Cloud Run redeploy. That's the
entire point: "run it today, revert immediately if it's wrong" only works
if reverting doesn't require a deploy.

Mirrors app/db/geocache.py's `_verified_entries()` snapshot/TTL pattern
(same idiom, new collection) rather than reading Firestore on every call.

One document per flag, collection `app_config`, doc id = flag name, e.g.:

    app_config/master_waypoint_v2
    {
        "enabled": false,
        "version": 1,
        "gates": ["5.3"],
        "description": "...",
    }

A flag with no document (never seeded, or the read failed) defaults to
`False` — the same "ships disabled" posture as an explicitly-off flag.
Nothing here ever raises out to a caller; a config-store outage degrades to
"every flag behaves as off" rather than breaking the request it's gating.
"""

import logging
import time
from typing import Any, Optional

from google.cloud import firestore

from app.config import FEATURE_FLAG_SNAPSHOT_TTL_SECONDS
from app.db.clients import get_fs_client

logger = logging.getLogger(__name__)

APP_CONFIG_COLLECTION = "app_config"

# Every flag this spec currently names (§5.1-§5.5), seeded disabled by
# seed_default_flags() if not already present. seed_default_flags() never
# overwrites an existing doc, so this list is only ever a starting point.
DEFAULT_FLAGS = {
    "master_waypoint_v2": {
        "version": 1,
        "gates": ["5.3"],
        "description": "Master-waypoint lookup ahead of geocache in save.py's _geocode().",
    },
    "geocoding_ambiguity_v2": {
        "version": 1,
        "gates": ["5.1"],
        "description": "Bounds/multi-candidate classification in geocoding.py.",
    },
    "outlier_trap_v2": {
        "version": 1,
        "gates": ["5.2"],
        "thresholds_km": {"suggest": 2, "medium": 5, "block": 10},
        "description": "Pre-DRS solitary-outlier check in sorting.py.",
    },
    "eod_ingestion_v2": {
        "version": 1,
        "gates": ["5.4"],
        "description": "Whether the /eod-delivery-feedback endpoint is live.",
    },
    "bqml_embeddings_eval": {
        "version": 1,
        "gates": ["5.5"],
        "description": "Evaluation-only; never gates the live save-consignments path.",
    },
}

_snapshot = {"loaded_at": 0.0, "flags": None}


def _collection():
    return get_fs_client().collection(APP_CONFIG_COLLECTION)


def _load_flags() -> dict:
    now = time.monotonic()
    if (
        _snapshot["flags"] is not None
        and now - _snapshot["loaded_at"] < FEATURE_FLAG_SNAPSHOT_TTL_SECONDS
    ):
        return _snapshot["flags"]

    try:
        flags = {doc.id: (doc.to_dict() or {}) for doc in _collection().stream()}
    except Exception:
        logger.exception("Failed to load app_config flags.")
        # A transient read failure keeps the last-known snapshot rather than
        # wiping it to "everything off" out from under a request in flight.
        # A cold start with no prior snapshot genuinely has nothing but
        # defaults to fall back to.
        return _snapshot["flags"] if _snapshot["flags"] is not None else {}

    _snapshot["flags"] = flags
    _snapshot["loaded_at"] = now
    logger.info("Loaded %d app_config flag(s).", len(flags))
    return flags


def invalidate_snapshot() -> None:
    """Force the next read to hit Firestore. Mainly for tests."""
    _snapshot["flags"] = None
    _snapshot["loaded_at"] = 0.0


def is_enabled(flag_name: str, default: bool = False) -> bool:
    doc = _load_flags().get(flag_name)
    if doc is None:
        return default
    return bool(doc.get("enabled", default))


def get_flag(flag_name: str) -> Optional[dict]:
    return _load_flags().get(flag_name)


def get_value(flag_name: str, key: str, default: Any = None) -> Any:
    """Read a non-boolean config value off a flag doc, e.g.
    get_value('outlier_trap_v2', 'thresholds_km') -> {'suggest': 2, ...}.
    """
    doc = get_flag(flag_name)
    if not doc:
        return default
    return doc.get(key, default)


def seed_default_flags() -> int:
    """Idempotent: create any of DEFAULT_FLAGS not already present, all
    disabled. Never touches an existing doc, so a flag someone has already
    configured — on, off, or with edited thresholds — is left exactly as is.
    Safe to run on every deploy; returns the number of docs actually created.
    """
    created = 0
    for name, payload in DEFAULT_FLAGS.items():
        doc_ref = _collection().document(name)
        if doc_ref.get().exists:
            continue
        doc_ref.set({
            **payload,
            "enabled": False,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        created += 1

    if created:
        invalidate_snapshot()
        logger.info("Seeded %d new app_config flag(s).", created)
    return created
