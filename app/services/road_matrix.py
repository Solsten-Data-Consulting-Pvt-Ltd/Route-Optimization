"""Road distances for route sorting (featureFlags/roadDistanceRouting).

The optimizer used straight-line distance, so it could order 11 -> 12 -> 13
where 12 looks close to 11 but is far by road (a lake, a field, one entry to a
layout) and the driver passes 13 to reach 12 and comes back. This module builds
the optimizer's distance table from ROAD distances instead.

What is looked up (Google Routes API, Compute Route Matrix, Essentials):
  * a DRS up to ROAD_FULL_MATRIX_MAX_POINTS points gets EVERY pair by road -
    nothing is guessed. (With only nearest-neighbour lookups, the guessed far
    pairs at straight line x 1.4 looked cheaper than real roads, which run
    ~2x straight line around Carmelaram's lakes - DHRD20055, 19 Nov - and the
    solver jumped through the guesses);
  * a larger DRS asks for each point's ROAD_DISTANCE_NEIGHBOURS nearest
    points, in both directions;
  * any pair still not known (larger DRS, cap, API error, within one cell) is
    estimated as straight line x this DRS's own typical road/straight ratio
    from the pairs just looked up (ROAD_DETOUR_FACTOR when too few samples);
Cost control:
  * results are cached in Firestore `road_distance_cache` per pair of ~150 m
    cells (geohash-7, per direction), so repeat areas cost nothing;
  * at most ROAD_MAX_ELEMENTS_PER_RUN elements per optimize run;
  * Google's per-minute quota (HTTP 429) is waited out and retried, for at
    most ROAD_RATE_LIMIT_MAX_WAIT_S per run, nearest pairs asked first.

Never raises for API problems: any pair it can't get from the cache or the API
falls back to the straight-line estimate, so routing always completes.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pygeohash as pgh
import requests

import statistics

from app.config import (
    GOOGLE_MAPS_API_KEY,
    ROAD_CACHE_COLLECTION,
    ROAD_CACHE_GEOHASH_LEN,
    ROAD_CACHE_TTL_DAYS,
    ROAD_DETOUR_FACTOR,
    ROAD_DETOUR_FACTOR_MAX,
    ROAD_DETOUR_FACTOR_MIN,
    ROAD_DETOUR_MIN_SAMPLES,
    ROAD_DISTANCE_NEIGHBOURS,
    ROAD_FULL_MATRIX_MAX_POINTS,
    ROAD_MAX_DESTINATIONS_PER_REQUEST,
    ROAD_MAX_ELEMENTS_PER_RUN,
    ROAD_MAX_PARALLEL_REQUESTS,
    ROAD_RATE_LIMIT_MAX_WAIT_S,
    ROAD_RATE_LIMIT_PAUSE_S,
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
def _wanted_pairs(points):
    """{origin index: [destination indexes]} to look up by road, and the mode.
    Small DRS: every pair. Large DRS: nearest neighbours, both directions."""
    n = len(points)
    if n <= ROAD_FULL_MATRIX_MAX_POINTS:
        return {i: [j for j in range(n) if j != i] for i in range(n)}, "full"
    nearest = neighbour_pairs(points)
    wanted = {i: set(js) for i, js in nearest.items()}
    for i, js in nearest.items():
        for j in js:
            wanted[j].add(i)
    return {i: sorted(js) for i, js in wanted.items()}, "nearest"


def detour_factor(points, road_values):
    """Typical road/straight-line ratio for this DRS from the pairs looked up
    ({(i, j): metres}), clamped; ROAD_DETOUR_FACTOR with too few samples.
    Very short pairs are left out (their ratio is noise)."""
    ratios = []
    for (i, j), metres in road_values.items():
        straight = straight_line_m(points[i], points[j])
        if straight >= 200:
            ratios.append(metres / straight)
    if len(ratios) < ROAD_DETOUR_MIN_SAMPLES:
        return ROAD_DETOUR_FACTOR
    return min(ROAD_DETOUR_FACTOR_MAX, max(ROAD_DETOUR_FACTOR_MIN, statistics.median(ratios)))


def build_distance_matrix(points, max_elements=ROAD_MAX_ELEMENTS_PER_RUN, road_out=None):
    """Square matrix (metres, int) over `points` [(lat, lon), ...].

    Every pair (small DRS) or each point's nearest pairs (large DRS) use road
    distance (cache first, then the API). Anything left - and any pair the
    cache/API couldn't answer - is estimated from this DRS's own road/straight
    ratio. Returns (matrix, stats). `road_out`, if given (a dict), is filled
    with {(i, j): metres} for every pair answered by road (used by the OSRM
    shadow comparison).
    """
    n = len(points)
    cells = [_cell(p) for p in points]
    stats = {"road_pairs": 0, "from_cache": 0, "from_api": 0, "api_elements": 0,
             "estimated_pairs": 0, "api_errors": 0}

    wanted, mode = _wanted_pairs(points)
    stats["mode"] = mode
    stats["points"] = n
    # Pairs inside one ~150 m cell share one cache key and are too short to
    # be worth a lookup; they are estimated.
    needed = {(i, j) for i, js in wanted.items() for j in js if cells[i] != cells[j]}

    road = {}  # (i, j) -> metres, every pair answered by road
    cached = cache_get({(cells[i], cells[j]) for i, j in needed})
    to_fetch = {}
    for i, j in sorted(needed):
        key = (cells[i], cells[j])
        if key in cached:
            road[(i, j)] = cached[key]
            stats["from_cache"] += 1
        else:
            to_fetch.setdefault(i, []).append(j)

    # Requests: one origin and at most ROAD_MAX_DESTINATIONS_PER_REQUEST of its
    # destinations, nearest first. Jobs go in rounds - every origin's nearest
    # chunk, then every origin's next chunk - so if the element cap or the
    # rate-limit wait cuts the run short, what is missing are far pairs (the
    # least useful) rather than whole stops.
    chunks = {}
    for i, js in to_fetch.items():
        js = sorted(js, key=lambda j: (straight_line_m(points[i], points[j]), j))
        chunks[i] = [js[k:k + ROAD_MAX_DESTINATIONS_PER_REQUEST]
                     for k in range(0, len(js), ROAD_MAX_DESTINATIONS_PER_REQUEST)]
    jobs, budget = [], max_elements
    for rnd in range(max((len(c) for c in chunks.values()), default=0)):
        for i in sorted(chunks):
            if rnd < len(chunks[i]) and len(chunks[i][rnd]) <= budget:
                jobs.append((i, chunks[i][rnd]))
                budget -= len(chunks[i][rnd])
    skipped = sum(len(js) for js in to_fetch.values()) - sum(len(js) for _, js in jobs)
    if skipped:
        logger.warning("Road matrix: element cap %d reached; %d pair(s) use the estimate",
                       max_elements, skipped)

    def run(job):
        i, js = job
        try:
            return i, js, fetch_row(points[i], [points[j] for j in js]), None
        except Exception as e:     # network / HTTP / quota / disabled API
            return i, js, {}, e

    def rate_limited(err):
        response = getattr(err, "response", None)
        return getattr(response, "status_code", None) == 429

    new_cache = {}
    stats["rate_limited"] = 0
    deadline = time.monotonic() + ROAD_RATE_LIMIT_MAX_WAIT_S
    pending = jobs
    while pending:
        retry = []
        with ThreadPoolExecutor(max_workers=ROAD_MAX_PARALLEL_REQUESTS) as pool:
            for job, (i, js, row, err) in zip(pending, pool.map(run, pending)):
                if err is not None and rate_limited(err):
                    stats["rate_limited"] += 1      # refused, not billed
                    retry.append(job)
                    continue
                stats["api_elements"] += len(js)
                if err is not None:
                    stats["api_errors"] += 1
                    logger.warning("Routes API request failed for origin %d: %s", i, err)
                    continue
                for pos, j in enumerate(js):
                    if pos in row:
                        metres, seconds = row[pos]
                        road[(i, j)] = metres
                        new_cache[(cells[i], cells[j])] = (metres, seconds)
                        stats["from_api"] += 1
        if not retry:
            break
        wait = min(ROAD_RATE_LIMIT_PAUSE_S, deadline - time.monotonic())
        if wait <= 0:
            stats["api_errors"] += len(retry)
            logger.warning("Road matrix: still rate-limited after %ds; %d request(s) use the estimate",
                           ROAD_RATE_LIMIT_MAX_WAIT_S, len(retry))
            break
        logger.info("Road matrix: rate-limited on %d request(s); retrying in %.0fs", len(retry), wait)
        time.sleep(wait)
        pending = retry
    cache_put(new_cache)

    factor = detour_factor(points, road)
    stats["detour_factor"] = round(factor, 2)
    matrix = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if (i, j) in road:
                matrix[i][j] = int(round(road[(i, j)]))
            else:
                matrix[i][j] = int(round(straight_line_m(points[i], points[j]) * factor))

    stats["road_pairs"] = len(road)
    stats["estimated_pairs"] = n * (n - 1) - stats["road_pairs"]
    if road_out is not None:
        road_out.update(road)
    return matrix, stats
