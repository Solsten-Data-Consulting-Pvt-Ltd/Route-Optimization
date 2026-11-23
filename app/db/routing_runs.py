"""One record per optimize run in Firestore `routing_runs` (20 Nov 2026).

Answers "how many road distances did we buy from Google today, and how many
came free from the cache?" — the Routes API is billed per distance (element),
and before this the per-run numbers were only returned to the caller and lost.

Logging only: never raises, never changes the route.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import ROUTING_RUNS_COLLECTION, ROUTING_RUNS_TTL_DAYS

logger = logging.getLogger(__name__)

_STAT_KEYS = ("mode", "points", "road_pairs", "estimated_pairs", "from_cache",
              "from_api", "api_elements", "api_errors", "rate_limited", "detour_factor")


def build_routing_run(drs_no, result, road_stats, now=None):
    """The document for one run. Pure, so it is easy to test."""
    now = now or datetime.now(timezone.utc)
    stats = road_stats or {}
    points = stats.get("points")
    doc = {
        "drs_no": drs_no,
        "created_at": now,
        "expire_at": now + timedelta(days=ROUTING_RUNS_TTL_DAYS),
        "optimized_count": (result or {}).get("optimized_count", 0),
        "planned_from": (result or {}).get("planned_from"),
        "distance_source": (result or {}).get("distance_source", "straight_line"),
        # Every point to every other point, start included.
        "pairs_needed": points * (points - 1) if isinstance(points, int) and points > 1 else 0,
    }
    for key in _STAT_KEYS:
        if key in stats:
            doc[key] = stats[key]
    return doc


def record_routing_run(drs_no, result, road_stats):
    """Best-effort write; a failure is logged and ignored."""
    try:
        from app.db.clients import get_fs_client
        doc = build_routing_run(drs_no, result, road_stats)
        get_fs_client().collection(ROUTING_RUNS_COLLECTION).add(doc)
        logger.info("Routing run DRS %s: %s points, %s from Google, %s from cache, %s estimated",
                    drs_no, doc.get("points"), doc.get("from_api"), doc.get("from_cache"),
                    doc.get("estimated_pairs"))
        return True
    except Exception:
        logger.exception("Could not record routing run for DRS %s", drs_no)
        return False
