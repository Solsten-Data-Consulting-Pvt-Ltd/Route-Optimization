"""Persist one cache-analytics summary per DRS save run."""

import uuid
from datetime import datetime
from typing import Dict

from google.cloud import firestore

from app.db.clients import get_fs_client


DRS_CACHE_METRICS_COLLECTION = "drs_cache_metrics"


def write_drs_cache_metrics(
    metrics_by_drs: Dict[str, dict],
    run_started_at: datetime,
) -> None:
    """Write all per-DRS summaries in one Firestore batch.

    A new document is deliberately created for every completed save run. That
    makes time-series analysis possible while keeping metrics writes out of the
    per-consignment hot path.
    """
    if not metrics_by_drs:
        return

    client = get_fs_client()
    batch = client.batch()
    run_id = uuid.uuid4().hex

    for drs_id, counts in metrics_by_drs.items():
        lookups = counts["cacheLookups"]
        hits = counts["exactHits"] + counts["fuzzyHits"]
        payload = {
            "runId": run_id,
            "drsId": drs_id,
            "drsNo": counts["drsNo"],
            "runStartedAt": run_started_at,
            "completedAt": firestore.SERVER_TIMESTAMP,
            "consignmentsProcessed": counts["consignmentsProcessed"],
            "invalidAddresses": counts["invalidAddresses"],
            "cacheLookups": lookups,
            "exactHits": counts["exactHits"],
            "fuzzyHits": counts["fuzzyHits"],
            "drsHits": counts.get("drsHits", 0),
            "misses": counts["misses"],
            "cacheErrors": counts["cacheErrors"],
            "apiCalls": counts["apiCalls"],
            "apiFailures": counts["apiFailures"],
            "hitRate": round((hits / lookups) * 100, 2) if lookups else 0.0,
        }
        doc_ref = client.collection(DRS_CACHE_METRICS_COLLECTION).document(
            f"{run_id}_{uuid.uuid4().hex[:8]}"
        )
        batch.set(doc_ref, payload)

    batch.commit()
