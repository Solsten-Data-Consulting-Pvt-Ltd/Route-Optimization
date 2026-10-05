-- §5.5 — 02: the address corpus = labelled consignments + their address text.
--
-- scope_pincode  : the 6-digit pincode written IN THE ADDRESS — the only
--                  partition known before any geocoding, so it is what a live
--                  lookup could use. Rows without one cannot be scoped and are
--                  never searched (hard constraint), only counted.
-- initial_geohash6 / truth_geohash6 : for the geohash-6 scope (see README).
-- content        : normalised like app/db/geocache.normalize_address +
--                  phone numbers removed (embedding input).
-- door_numbers   : door/plot/shop numbers for the guard (ordinals excluded).
CREATE OR REPLACE TABLE `{{eval}}.corpus`
CLUSTER BY scope_pincode
AS
WITH labels AS (
  {{labels_select}}
),
joined AS (
  SELECT
    l.*,
    r.receiverAddress,
    {{initial_lat}} AS initial_lat,
    {{initial_lng}} AS initial_lng,
    r.created_at
  FROM labels AS l
  JOIN `{{routing_table}}` AS r USING (consignmentId, drsNo)
  WHERE r.receiverAddress IS NOT NULL AND TRIM(r.receiverAddress) != ''
    AND r.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @history_days DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY consignmentId, drsNo ORDER BY r.updated_at DESC) = 1
),
normalised AS (
  SELECT
    *,
    TRIM(REGEXP_REPLACE(
      REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(
      REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(
        LOWER(receiverAddress),
        r'(?:\+?91[\s-]?)?\b[6-9]\d{9}\b', ' '),                  -- mobile numbers
        r'\b\d{6}\b', ' '),                                       -- pincode
        r'\b[23456789cfghjmpqrvwx]{4,8}\+[23456789cfghjmpqrvwx]{2,3}\b', ' '),  -- plus codes
        r'[^a-z0-9/#]+', ' '),                                    -- punctuation
        r'\b(bangalore|banglore|bengalore|bangaluru|blr)\b', 'bengaluru'),
        r'\bsarjapur\b', 'sarjapura'),
        r'\b(flore|flr)\b', 'floor'),
        r'\bopp\b', 'opposite'),
      r'\s+', ' ')) AS content
  FROM joined
)
SELECT
  CONCAT(consignmentId, '|', drsNo)                              AS address_key,
  consignmentId,
  drsNo,
  receiverAddress,
  content,
  ARRAY_TO_STRING(ARRAY(
    SELECT DISTINCT n
    FROM UNNEST(REGEXP_EXTRACT_ALL(content, r'\b\d+(?:/\d+)*[a-z]?\b')) AS n
    ORDER BY n), ',')                                            AS door_numbers,
  REGEXP_EXTRACT(receiverAddress, r'\b(\d{6})\b')                AS scope_pincode,
  IF(initial_lat IS NULL OR initial_lng IS NULL, NULL,
     ST_GEOHASH(ST_GEOGPOINT(initial_lng, initial_lat), 6))       AS initial_geohash6,
  ST_GEOHASH(ST_GEOGPOINT(truth_lng, truth_lat), 6)              AS truth_geohash6,
  ST_GEOGPOINT(truth_lng, truth_lat)                             AS truth_pin,
  label_source,
  label_kind,
  labelled_at,
  created_at
FROM normalised
WHERE content != '';
