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

# Dynamic feature-flag/config store (see .specify/spec.md §5.6). Deliberately
# shorter than the geocode cache's TTL above -- the whole point of this store
# is same-day enable/revert without a redeploy, and a 5-minute-stale flag
# undermines that.
FEATURE_FLAG_SNAPSHOT_TTL_SECONDS = int(os.environ.get("FEATURE_FLAG_SNAPSHOT_TTL_SECONDS", "45"))

GEOHASH_LOCALITY_LEN = 5
GEOHASH_BUILDING_LEN = 6
MAX_ROWS_PER_MERGE = 500
MAX_ROWS_PER_UPDATE = 500
FIRESTORE_BATCH_SIZE = 500

TABLE_REF = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"
STRUCTURED_TABLE_REF = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_STRUCTURED_TABLE}"