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

# DRS address memo: reuse a pin for same-place consignments within one DRS.
DRS_MEMO_COLLECTION = "drs_address_memo"
DRS_MEMO_TTL_DAYS = 2
DRS_MEMO_FUZZY_THRESHOLD = 90
DRS_MEMO_CONTAINMENT_RATIO = 0.9
DRS_MEMO_MIN_CONTAINED_TOKENS = 5

# Feature flags: the 3PL featureFlags collection (same Firestore project).
FEATURE_FLAGS_COLLECTION = "featureFlags"
FEATURE_FLAG_TTL_SECONDS = int(os.environ.get("FEATURE_FLAG_TTL_SECONDS", "45"))

# Address resolution: Places candidates closer than this count as one place.
PLACES_CANDIDATE_PAGE_SIZE = 5
SAME_PLACE_SPREAD_M = 300

GEOHASH_LOCALITY_LEN = 5
GEOHASH_BUILDING_LEN = 6
MAX_ROWS_PER_MERGE = 500
MAX_ROWS_PER_UPDATE = 500
FIRESTORE_BATCH_SIZE = 500

TABLE_REF = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"
STRUCTURED_TABLE_REF = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_STRUCTURED_TABLE}"