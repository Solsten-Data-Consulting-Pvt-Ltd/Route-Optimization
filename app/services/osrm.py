"""Road distances from our own OSRM server (featureFlags/roadDistanceProvider).

OSRM (open-source routing on OpenStreetMap) runs on a private VM; this module
asks its /table service for the stop-to-stop road distances the optimizer
needs, instead of buying them from Google's Routes API. Nothing about the
routing itself changes: only where the distance table comes from.

Providers (see app/db/feature_flags.py):
  * google - as before (this module is not used);
  * shadow - the route is built from Google exactly as before; OSRM is asked
    for the same points and only the comparison is logged (shadow_stats).
    OSRM problems are ignored;
  * osrm   - every pair comes from OSRM. No Google calls. A pair OSRM cannot
    answer (no route, or a stop whose pin is > OSRM_MAX_SNAP_M from any road)
    uses the straight-line estimate (the DRS's own typical road/straight
    ratio), like any other unknown pair. If OSRM itself is unreachable the
    caller (sorting._road_matrix) uses straight lines for the whole DRS and
    raises an alert.

OSRM takes coordinates as lon,lat (Google and this code use lat,lon).
OSRM answers are NOT written to the Google `road_distance_cache`.
"""

import logging
import statistics
import time

import requests

from app import config
from app.services.road_matrix import detour_factor, straight_line_m

logger = logging.getLogger(__name__)


class OsrmError(Exception):
    """OSRM is off, unreachable, or answered something unusable."""


def _get(path, params=None):
    if not config.OSRM_BASE_URL:
        raise OsrmError("OSRM_BASE_URL is not set")
    try:
        resp = requests.get(f"{config.OSRM_BASE_URL}{path}", params=params,
                            timeout=config.OSRM_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise OsrmError(f"OSRM request failed: {e}") from e
    if not isinstance(data, dict) or data.get("code") != "Ok":
        raise OsrmError(f"OSRM answered {data.get('code') if isinstance(data, dict) else data!r}")
    return data


def _coords(points):
    return ";".join(f"{lon:.7f},{lat:.7f}" for lat, lon in points)


def table(points):
    """(distances, snaps, requests_made): `distances[i][j]` is road metres from
    points[i] to points[j] (None = no route), `snaps[i]` is how far points[i]
    is from the road it was placed on. Large sets are asked in blocks of
    OSRM_TABLE_BLOCK sources x destinations. Raises OsrmError."""
    n = len(points)
    block = config.OSRM_TABLE_BLOCK
    distances = [[None] * n for _ in range(n)]
    snaps = [None] * n
    made = 0
    for s0 in range(0, n, block):
        src = list(range(s0, min(s0 + block, n)))
        for d0 in range(0, n, block):
            dst = list(range(d0, min(d0 + block, n)))
            if src == dst:
                idx = src
                src_pos = list(range(len(idx)))
                dst_pos = src_pos
            else:
                idx = src + dst
                src_pos = list(range(len(src)))
                dst_pos = list(range(len(src), len(idx)))
            params = {"annotations": "distance"}
            if idx != list(range(n)):          # a block, not the whole set
                params["sources"] = ";".join(map(str, src_pos))
                params["destinations"] = ";".join(map(str, dst_pos))
            data = _get(f"/table/v1/driving/{_coords([points[k] for k in idx])}", params)
            made += 1
            rows = data.get("distances")
            if (not isinstance(rows, list) or len(rows) != len(src)
                    or any(not isinstance(r, list) or len(r) != len(dst) for r in rows)):
                raise OsrmError("OSRM /table answer has the wrong shape")
            for a, i in enumerate(src):
                for b, j in enumerate(dst):
                    distances[i][j] = rows[a][b]
            for a, w in enumerate(data.get("sources") or []):
                if a < len(src) and isinstance(w, dict) and w.get("distance") is not None:
                    snaps[src[a]] = float(w["distance"])
            for b, w in enumerate(data.get("destinations") or []):
                if b < len(dst) and isinstance(w, dict) and w.get("distance") is not None:
                    snaps[dst[b]] = float(w["distance"])
    return distances, snaps, made


def build_distance_matrix_osrm(points):
    """Square matrix (metres, int) over `points` [(lat, lon), ...] from OSRM,
    and stats shaped like road_matrix.build_distance_matrix's. Raises OsrmError
    when OSRM cannot be used at all."""
    n = len(points)
    started = time.monotonic()
    distances, snaps, made = table(points)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    bad_snap = {i for i, s in enumerate(snaps) if s is not None and s > config.OSRM_MAX_SNAP_M}
    road = {}
    unroutable = 0
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            metres = distances[i][j]
            if metres is None:
                unroutable += 1
            elif i in bad_snap or j in bad_snap:
                continue
            else:
                road[(i, j)] = max(0.0, float(metres))

    factor = detour_factor(points, road)
    matrix = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if (i, j) in road:
                matrix[i][j] = int(round(road[(i, j)]))
            else:
                matrix[i][j] = int(round(straight_line_m(points[i], points[j]) * factor))

    total = n * (n - 1)
    stats = {
        "provider": "osrm", "mode": "osrm", "points": n,
        "road_pairs": len(road), "estimated_pairs": total - len(road),
        "from_cache": 0, "from_api": 0, "api_elements": 0, "api_errors": 0,
        "rate_limited": 0, "detour_factor": round(factor, 2),
        "osrm_ms": elapsed_ms, "osrm_requests": made, "osrm_errors": 0,
        "osrm_unroutable_pairs": unroutable, "snap_outliers": len(bad_snap),
    }
    return matrix, stats


def shadow_stats(points, google_road):
    """Compare OSRM with the Google road distances just used for this DRS
    (`google_road`: {(i, j): metres}). Returns the stats to log. Never raises:
    any OSRM problem is returned as {"shadow_error": ...}."""
    try:
        distances, snaps, made = table(points)
    except Exception as e:
        logger.warning("OSRM shadow comparison skipped: %s", e)
        return {"shadow_error": str(e)[:200]}
    ratios, off = [], 0
    for (i, j), google_m in google_road.items():
        osrm_m = distances[i][j]
        if osrm_m is None or google_m <= 0:
            continue
        ratio = osrm_m / google_m
        ratios.append(ratio)
        if abs(ratio - 1) > config.OSRM_OFF_RATIO and abs(osrm_m - google_m) > config.OSRM_OFF_MIN_M:
            off += 1
    stats = {
        "shadow_pairs": len(ratios),
        "snap_outliers": sum(1 for s in snaps if s is not None and s > config.OSRM_MAX_SNAP_M),
        "osrm_requests": made,
    }
    if ratios:
        ordered = sorted(ratios)
        stats["shadow_median_ratio"] = round(statistics.median(ordered), 3)
        stats["shadow_p90_ratio"] = round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 3)
        stats["shadow_off_pairs"] = off
        stats["shadow_off_pct"] = round(100.0 * off / len(ratios), 1)
    return stats


def check_sample(points):
    """Sanity-check OSRM on real stops (`points`, [(lat, lon), ...]). Returns
    {"points", "pairs", "null_share", "median_ratio"} or raises OsrmError with
    what is wrong: too many pairs with no route, or a typical road distance
    that is impossible next to the straight line (broken / partial map)."""
    distances, snaps, _made = table(points)
    pairs = nulls = 0
    ratios = []
    for i in range(len(points)):
        for j in range(len(points)):
            if i == j:
                continue
            pairs += 1
            if distances[i][j] is None:
                nulls += 1
                continue
            straight = straight_line_m(points[i], points[j])
            if straight >= config.OSRM_HEALTH_RATIO_MIN_STRAIGHT_M:
                ratios.append(distances[i][j] / straight)
    null_share = nulls / pairs if pairs else 0.0
    median = statistics.median(ratios) if ratios else None
    lo, hi = config.OSRM_HEALTH_RATIO_RANGE
    if null_share > config.OSRM_HEALTH_MAX_NULL_SHARE:
        raise OsrmError(f"{nulls} of {pairs} pairs between {len(points)} real stops have no route")
    if median is not None and not (lo <= median <= hi):
        raise OsrmError(f"road/straight-line ratio on {len(points)} real stops is {median:.2f} "
                        f"(normal is {lo}-{hi}); the map may be broken or partial")
    return {"points": len(points), "pairs": pairs, "null_share": round(null_share, 3),
            "median_ratio": round(median, 2) if median is not None else None}


def health_check(sample_points=None):
    """Used by GET /health/osrm. {"status": "ok" | "disabled" | "failed", ...}.
    Asks for one known route and one small table, checks the answers, and when
    `sample_points` (real stops picked at random) are given checks those too."""
    if not config.OSRM_BASE_URL:
        return {"status": "disabled", "detail": "OSRM_BASE_URL is not set"}
    started = time.monotonic()
    try:
        (alat, alon), (blat, blon) = config.OSRM_HEALTH_ROUTE
        data = _get(f"/route/v1/driving/{alon},{alat};{blon},{blat}", {"overview": "false"})
        routes = data.get("routes") or []
        if not routes or routes[0].get("distance") is None:
            raise OsrmError("OSRM answered no route for the test points")
        metres = float(routes[0]["distance"])
        expected = config.OSRM_HEALTH_EXPECTED_M
        if abs(metres - expected) > expected * config.OSRM_HEALTH_TOLERANCE:
            raise OsrmError(f"test route is {metres:.0f} m, expected about {expected} m "
                            "(wrong or broken map?)")
        distances, _snaps, _made = table([config.OSRM_HEALTH_ROUTE[0], config.OSRM_HEALTH_ROUTE[1],
                                          (12.9, 77.65)])
        if any(distances[i][j] is None for i in range(3) for j in range(3) if i != j):
            raise OsrmError("OSRM /table could not route between the test points")
        sample = None
        if sample_points and len(sample_points) >= 3:
            sample = check_sample(sample_points)
    except OsrmError as e:
        return {"status": "failed", "detail": str(e),
                "ms": int((time.monotonic() - started) * 1000)}
    result = {"status": "ok", "test_route_m": round(metres),
              "ms": int((time.monotonic() - started) * 1000)}
    if sample:
        result["sample"] = sample
    return result
