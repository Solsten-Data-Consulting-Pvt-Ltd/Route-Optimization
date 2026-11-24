import os

GOOGLE_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]

BQ_PROJECT = os.environ["BQ_PROJECT"]
BQ_DATASET = os.environ["BQ_DATASET"]
BQ_TABLE = os.environ["BQ_TABLE"]
BQ_STRUCTURED_TABLE = os.environ["BQ_STRUCTURED_TABLE"]
FIRESTORE_PROJECT = os.environ["FIRESTORE_PROJECT"]

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

GEOCODING_SOURCE = "google_places_api"

GEOCODE_CACHE_FUZZY_THRESHOLD = 85
GEOCODE_CACHE_SNAPSHOT_TTL_SECONDS = 300
# Unverified geocode_cache docs (never verified by an executive, so never served)
# auto-delete this many days after their last write, via a Firestore TTL policy
# on `expire_at`. Verified docs have no `expire_at` and are never deleted.
GEOCODE_CACHE_UNVERIFIED_TTL_DAYS = 3
# Auto-verify on delivery: the pin is verified once this many separate DRS
# visits delivered within this radius of it (GPS worse than
# GEOCODE_CACHE_MAX_OBSERVATION_ACCURACY_METERS is not counted).
# 20 Nov 2026: back to one delivery within 500 m, the rule before 14 Nov
# (which was 2 visits within 150 m). Deploy-time constants, not feature flags.
GEOCODE_CACHE_VERIFY_RADIUS_METERS = 500
GEOCODE_CACHE_MIN_VISITS = 1
# Suggested Pins: 3+ separate DRS visits agreeing within this radius on a spot
# this far or more from the saved pin. Kept tight on purpose, separate from
# the verify radius above.
GEOCODE_CACHE_SUGGEST_RADIUS_METERS = 150
GEOCODE_CACHE_MAX_OBSERVATIONS = 20
GEOCODE_CACHE_MAX_OBSERVATION_ACCURACY_METERS = 100

# DRS address memo: reuse a pin for same-place consignments within one DRS.
DRS_MEMO_COLLECTION = "drs_address_memo"
DRS_MEMO_TTL_DAYS = 2
DRS_MEMO_FUZZY_THRESHOLD = 90
DRS_MEMO_CONTAINMENT_RATIO = 0.9
DRS_MEMO_MIN_CONTAINED_TOKENS = 5
DRS_MEMO_PHONE_MIN_TEXT_SIMILARITY = 60  # shared phone also needs this much text overlap

# Feature flags: the 3PL featureFlags collection (same Firestore project).
FEATURE_FLAGS_COLLECTION = "featureFlags"
FEATURE_FLAG_TTL_SECONDS = int(os.environ.get("FEATURE_FLAG_TTL_SECONDS", "45"))

# Address resolution: Places candidates closer than this count as one place.
PLACES_CANDIDATE_PAGE_SIZE = 5
SAME_PLACE_SPREAD_M = 300
# Results whose names match the label's building name and sit within this
# distance of each other are one compound (towers, phases, gates, duplicate
# listings), not "several matches".
COMPOUND_SPREAD_M = 800

# Road distances for route sorting (featureFlags/roadDistanceRouting).
# Google Routes API, Compute Route Matrix, TRAFFIC_UNAWARE (Essentials SKU).
ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
# Large DRSs only (see ROAD_FULL_MATRIX_MAX_POINTS): road distance is fetched
# from each stop to its N nearest stops (by straight line), in both directions;
# other pairs are estimated (see the detour factor below).
ROAD_DISTANCE_NEIGHBOURS = 12
ROAD_DETOUR_FACTOR = 1.4
# Any pair still estimated (large DRS, cap, API error) uses this DRS's own
# typical road/straight-line ratio from the pairs just looked up, clamped to
# this range; ROAD_DETOUR_FACTOR only when there are too few samples.
ROAD_DETOUR_FACTOR_MIN = 1.3
ROAD_DETOUR_FACTOR_MAX = 3.0
ROAD_DETOUR_MIN_SAMPLES = 5
# DRSs up to this many points (stops + start) get the FULL road matrix: every
# pair is looked up, nothing is guessed (76 stops = 5,852 elements the first
# time; repeats come from the cache). Larger DRSs fall back to each stop's
# nearest ROAD_DISTANCE_NEIGHBOURS, both directions.
# Was 61. Measured on DHRD10230 (76 stops, 3 Nov 2026), scored in real road
# km: nearest-12 + estimates 89.8 km vs the full matrix 82.4 km (+9%); a
# longer search did not close the gap - the guessed pairs did.
ROAD_FULL_MATRIX_MAX_POINTS = 101
# Destinations per Compute Route Matrix request (one origin each).
ROAD_MAX_DESTINATIONS_PER_REQUEST = 25
# Hard cap on billable matrix elements per optimize run (budget guard); pairs
# beyond it use the estimate. Covers a full matrix at ROAD_FULL_MATRIX_MAX_POINTS
# (101 x 100 = 10,100).
ROAD_MAX_ELEMENTS_PER_RUN = 10100
ROAD_REQUEST_TIMEOUT_S = 10
ROAD_MAX_PARALLEL_REQUESTS = 8
# Google answers 429 (too many requests) past its per-minute element quota
# (seen at ~3,300 elements inside a minute). Rate-limited requests are retried
# after a pause, for at most this long in total per run, instead of quietly
# using the estimate; whatever is still missing after that is estimated.
# Kept well under 3PL's 3.5-minute timeout on the sort call.
ROAD_RATE_LIMIT_MAX_WAIT_S = 90
ROAD_RATE_LIMIT_PAUSE_S = 15
# Cache of road distances between ~150 m cells (geohash-7), per direction.
ROAD_CACHE_COLLECTION = "road_distance_cache"
ROAD_CACHE_GEOHASH_LEN = 7
ROAD_CACHE_TTL_DAYS = 30
# One small doc per optimize run (app/db/routing_runs.py): how many road
# distances were needed, bought from Google, served from the cache or
# estimated — so the Routes API cost and cache savings can be read per day.
# Add a Firestore TTL policy on `expire_at`.
ROUTING_RUNS_COLLECTION = "routing_runs"
ROUTING_RUNS_TTL_DAYS = 90

# Self-hosted OSRM road distances (featureFlags/roadDistanceProvider =
# "google" | "shadow" | "osrm"; default google). Empty OSRM_BASE_URL = OSRM off.
# The server is a private VM (no public IP), reached over the VPC, e.g.
# http://10.160.0.2:5000 (osrm-backend, MLD, --max-table-size 150).
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "").strip().rstrip("/")
OSRM_TIMEOUT_S = float(os.environ.get("OSRM_TIMEOUT_S", "10"))
# Sources / destinations per /table request (server limit is 150 each).
OSRM_TABLE_BLOCK = 100
# A stop whose pin snaps further than this from any road is not trusted: its
# pairs use the straight-line estimate instead (gated layouts, missing lanes).
OSRM_MAX_SNAP_M = 200
# Shadow stats call a pair "off" when OSRM differs from Google by more than
# this share AND more than this many metres.
OSRM_OFF_RATIO = 0.25
OSRM_OFF_MIN_M = 500
# /health/osrm: a known short route in Bengaluru and the distance it should
# be. 3,867 m on the 2026-11 map; a monthly map refresh may move it a little.
OSRM_HEALTH_ROUTE = ((12.8864375, 77.6381875), (12.8949382, 77.6572065))
OSRM_HEALTH_EXPECTED_M = 3867
OSRM_HEALTH_TOLERANCE = 0.30
# The daily check also asks OSRM about a few real stops picked at random from the
# most recent `consignments_routing` docs (so it tests today's operating area,
# not one fixed route). It fails when too many pairs have no route, or when the
# typical road/straight-line ratio is impossible (a broken or partial map).
OSRM_HEALTH_SAMPLE_POINTS = 10
OSRM_HEALTH_SAMPLE_POOL = 300
OSRM_HEALTH_MAX_NULL_SHARE = 0.20
OSRM_HEALTH_RATIO_RANGE = (0.90, 3.5)
OSRM_HEALTH_RATIO_MIN_STRAIGHT_M = 300

# Alerts: a Google Chat space incoming webhook. Empty = alerts are only logged.
ALERT_CHAT_WEBHOOK_URL = os.environ.get("ALERT_CHAT_WEBHOOK_URL", "").strip()
ALERT_ENV_NAME = os.environ.get("ALERT_ENV_NAME", "").strip()
# The same alert key is sent at most once per this many seconds per instance.
ALERT_COOLDOWN_S = int(os.environ.get("ALERT_COOLDOWN_S", "1800"))

GEOHASH_LOCALITY_LEN = 5
GEOHASH_BUILDING_LEN = 6
MAX_ROWS_PER_MERGE = 500
MAX_ROWS_PER_UPDATE = 500
FIRESTORE_BATCH_SIZE = 500

TABLE_REF = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"
STRUCTURED_TABLE_REF = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_STRUCTURED_TABLE}"
