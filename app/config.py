import os

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
BQ_PROJECT = os.environ.get("BQ_PROJECT", "prj-dev-hermes")
BQ_DATASET = os.environ.get("BQ_DATASET", "Hermes_Exports")
BQ_TABLE = os.environ.get("BQ_TABLE", "consignments_routing")
BQ_STRUCTURED_TABLE = os.environ.get("BQ_STRUCTURED_TABLE", "consignments_structured")
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", BQ_PROJECT)

# Save pipeline geocodes through the Places API v1 text search (same as the
# hermes-save Cloud Function). The legacy Geocoding API endpoint is kept here
# only for reference — nothing calls it.
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

GEOCODING_SOURCE = "google_places_api"

# Geocode cache (see app/db/geocache.py). The fuzzy cutoff and the scorer are
# the ones the hermes-save cache was tuned with; the snapshot TTL bounds how
# long a process reuses its in-memory copy of the verified entries.
GEOCODE_CACHE_FUZZY_THRESHOLD = 85
GEOCODE_CACHE_SNAPSHOT_TTL_SECONDS = 300

GEOHASH_LOCALITY_LEN = 5
GEOHASH_BUILDING_LEN = 6
MAX_ROWS_PER_MERGE = 500
MAX_ROWS_PER_UPDATE = 500
FIRESTORE_BATCH_SIZE = 500

TABLE_REF = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"
STRUCTURED_TABLE_REF = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_STRUCTURED_TABLE}"
