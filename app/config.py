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
# A delivery can verify a pin only after separate DRS visits agree with it.
# These are deploy-time constants, deliberately not feature flags.
GEOCODE_CACHE_VERIFY_RADIUS_METERS = 150
GEOCODE_CACHE_MIN_VISITS = 2
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

# Road distances for route sorting (featureFlags/roadDistanceRouting).
# Google Routes API, Compute Route Matrix, TRAFFIC_UNAWARE (Essentials SKU).
ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
# Road distance is fetched only from each stop to its N nearest stops (by
# straight line); the solver never links far-apart stops directly, so those
# pairs use straight line x ROAD_DETOUR_FACTOR instead. Cuts API use ~75 %.
ROAD_DISTANCE_NEIGHBOURS = 8
ROAD_DETOUR_FACTOR = 1.4
# Hard cap on billable matrix elements per optimize run (budget guard); pairs
# beyond it use the straight-line estimate.
ROAD_MAX_ELEMENTS_PER_RUN = 600
ROAD_REQUEST_TIMEOUT_S = 10
ROAD_MAX_PARALLEL_REQUESTS = 8
# Cache of road distances between ~150 m cells (geohash-7), per direction.
ROAD_CACHE_COLLECTION = "road_distance_cache"
ROAD_CACHE_GEOHASH_LEN = 7
ROAD_CACHE_TTL_DAYS = 30

GEOHASH_LOCALITY_LEN = 5
GEOHASH_BUILDING_LEN = 6
MAX_ROWS_PER_MERGE = 500
MAX_ROWS_PER_UPDATE = 500
FIRESTORE_BATCH_SIZE = 500

TABLE_REF = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"
STRUCTURED_TABLE_REF = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_STRUCTURED_TABLE}"
