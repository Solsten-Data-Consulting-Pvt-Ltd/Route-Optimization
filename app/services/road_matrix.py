"""Road distances for route sorting (featureFlags/roadDistanceRouting).

The optimizer used straight-line distance, so it could order 11 -> 12 -> 13
where 12 looks close to 11 but is far by road (a lake, a field, one entry to a
layout) and the driver passes 13 to reach 12 and comes back. This module builds
the optimizer's distance table from ROAD distances instead.

Cost control (Google Routes API, Compute Route Matrix, Essentials):
  * each point asks only for its ROAD_DISTANCE_NEIGHBOURS nearest points (one
    origin x up to 8 destinations per request); far pairs use straight line x
    ROAD_DETOUR_FACTOR, since the solver never links far stops directly;
  * results are cached in Firestore `road_distance_cache` per pair of ~150 m
    cells (geohash-7, per direction), so repeat areas cost nothing;
  * at most ROAD_MAX_ELEMENTS_PER_RUN elements per optimize run.

Never raises for API problems: any pair it can't get from the cache or the API
falls back to the straight-line estimate, so routing always completes.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pygeohash as pgh
import requests

from app.config import (
    GOOGLE_MAPS_API_KEY,
    ROAD_CACHE_COLLECTION,
    ROAD_CACHE_GEOHASH_LEN,
    ROAD_CACHE_TTL_DAYS,
    ROAD_DETOUR_FACTOR,
    ROAD_DISTANCE_NEIGHBOURS,
    ROAD_MAX_ELEMENTS_PER_RUN,
    ROAD_MAX_PARALLEL_REQUESTS,
    ROAD_REQUEST_TIMEOUT_S,
    ROUTES_MATRIX_URL,
)
from app.services.tsp import haversine_distance

logger = logging.getLogger(__name__)

FIELD_MASK = "originIndex,destinationIndex,distanceMeters,duration,condition,status"


def straight_line_m(a, b):
    return haversine_distance(a[0], a[1], b[0], b[1]) * 1000.0


def estimate_m(a, b):
    """Road distance estimate for pairs we don't look up."""
    return straight_line_m(a, b) * ROAD_DETOUR_FACTOR


def _cell(p):
    return pgh.encode(p[0], p[1], precision=ROAD_CACHE_GEOHASH_LEN)


def neighbour_pairs(points, k=ROAD_DISTANCE_NEIGHBOURS):
    """{origin index: [destination indexes]} - each point's k nearest points
    by straight line (ties broken by index, so the result is repeatable)."""
    pairs = {}
    for i, p in enumerate(points):
        others = sorted((j for j in range(len(points)) if j != i),
                        key=lambda j: (straight_line_m(p, points[j]), j))
        pairs[i] = others[:k]
    return pairs


# ---------------------------------------------------------------------------
# Firestore cache (cell -> cell, metres)
# ---------------------------------------------------------------------------
def _cache_collection():
    from app.db.clients import get_fs_client
    return get_fs_client().collection(ROAD_CACHE_COLLECTION)


def _cache_id(cell_a, cell_b):
    return f"{cell_a}_{cell_b}"


def cache_get(keys):
    """{(cell_a, cell_b): metres} for the keys found. Errors -> {}."""
    if not keys:
        return {}
    try:
        from app.db.clients import get_fs_client
        coll = _cache_collection()
        refs = [coll.document(_cache_id(a, b)) for a, b in keys]
        found = {}
        for snap in get_fs_client().get_all(refs):
            if snap.exists:
                data = snap.to_dict() or {}
                if data.get("distance_m") is not None:
                    a, b = snap.id.split("_", 1)
                    found[(a, b)] = float(data["distance_m"])
        return found
    except Exception:
        logger.exception("Road distance cache read failed")
        return {}


def cache_put(values):
    """values: {(cell_a, cell_b): (metres, seconds)}. Errors are logged only."""
    if not values:
        return
    try:
        from google.cloud import firestore
        from app.db.clients import get_fs_client
        client = get_fs_client()
        coll = _cache_collection()
        expires = datetime.now(timezone.utc) + timedelta(days=ROAD_CACHE_TTL_DAYS)
        items = list(values.items())
        for start in range(0, len(items), 450):
            batch = client.batch()
            for (a, b), (metres, seconds) in items[start:start + 450]:
                batch.set(coll.document(_cache_id(a, b)), {
                    "distance_m": metres, "duration_s": seconds,
                    "updated_at": firestore.SERVER_TIMESTAMP, "expires_at": expires,
                })
            batch.commit()
    except Exception:
        logger.exception("Road distance cache write failed")


# ---------------------------------------------------------------------------
# Google Routes API
# ---------------------------------------------------------------------------
def _waypoint(p):
    return {"waypoint": {"location": {"latLng": {"latitude": p[0], "longitude": p[1]}}}}


def _seconds(duration):
    try:
        return float(str(duration).rstrip("s"))
    except (TypeError, ValueError):
        return None


def fetch_row(origin, destinations):
    """Road distance (metres, seconds) from one origin to each destination.
    Returns {destination position: (metres, seconds)}; missing positions mean
    no route / error. Raises on HTTP failure (caller falls back)."""
    resp = requests.post(
        ROUTES_MATRIX_URL,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
            "X-Goog-FieldMask": FIELD_MASK,
        },
        json={
            "origins": [_waypoint(origin)],
            "destinations": [_waypoint(d) for d in destinations],
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_UNAWARE",
        },
        timeout=ROAD_REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    out = {}
    for el in resp.json() or []:
        if el.get("condition") != "ROUTE_EXISTS" or el.get("distanceMeters") is None:
            continue
        out[int(el.get("destinationIndex", 0))] = (float(el["distanceMeters"]),
                                                   _seconds(el.get("duration")))
    return out


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------
def build_distance_matrix(points, max_elements=ROAD_MAX_ELEMENTS_PER_RUN):
    """Square matrix (metres, int) over `points` [(lat, lon), ...].

    Neighbour pairs use road distance (cache first, then the API); every other
    pair, and any pair the cache/API couldn't answer, uses the straight-line
    estimate. Returns (matrix, stats).
    """
    n = len(points)
    cells = [_cell(p) for p in points]
    matrix = [[0 if i == j else int(round(estimate_m(points[i], points[j])))
               for j in range(n)] for i in range(n)]
    stats = {"road_pairs": 0, "from_cache": 0, "from_api": 0, "api_elements": 0,
             "estimated_pairs": 0, "api_errors": 0}

    wanted = neighbour_pairs(points)
    # Pairs inside one ~150 m cell are too short to be worth a lookup.
    needed = {(i, j) for i, js in wanted.items() for j in js if cells[i] != cells[j]}

    cached = cache_get({(cells[i], cells[j]) for i, j in needed})
    to_fetch = {}
    for i, j in sorted(needed):
        key = (cells[i], cells[j])
        if key in cached:
            matrix[i][j] = int(round(cached[key]))
            stats["from_cache"] += 1
        else:
            to_fetch.setdefault(i, []).append(j)

    # Respect the per-run budget: whole origin rows, nearest first, in order.
    jobs, budget = [], max_elements
    for i in sorted(to_fetch):
        js = to_fetch[i]
        if len(js) > budget:
            break
        jobs.append((i, js))
        budget -= len(js)
    skipped = sum(len(js) for i, js in to_fetch.items()) - sum(len(js) for _, js in jobs)
    if skipped:
        logger.warning("Road matrix: element cap %d reached; %d pair(s) use the estimate",
                       max_elements, skipped)

    def run(job):
        i, js = job
        try:
            return i, js, fetch_row(points[i], [points[j] for j in js]), None
        except Exception as e:     # network / HTTP / quota / disabled API
            return i, js, {}, e

    new_cache = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=ROAD_MAX_PARALLEL_REQUESTS) as pool:
            for i, js, row, err in pool.map(run, jobs):
                stats["api_elements"] += len(js)
                if err is not None:
                    stats["api_errors"] += 1
                    logger.warning("Routes API request failed for origin %d: %s", i, err)
                    continue
                for pos, j in enumerate(js):
                    if pos in row:
                        metres, seconds = row[pos]
                        matrix[i][j] = int(round(metres))
                        new_cache[(cells[i], cells[j])] = (metres, seconds)
                        stats["from_api"] += 1
    cache_put(new_cache)

    stats["road_pairs"] = stats["from_cache"] + stats["from_api"]
    stats["estimated_pairs"] = n * (n - 1) - stats["road_pairs"]
    return matrix, stats
