-- §5.5 — 02b: neighbouring pincodes, learned from your own data + reviewed seed.
--
-- Customers often write the pincode next door (label 560102, Shahi Exports in
-- Ambalipura is actually 560103). A pair (A, B) is learned when, over
-- @history_days:
--   * at least @min_pair_count saved pins whose LABEL says A sit in Google's
--     pincode B, and
--   * those pins are typically (median) within @neighbour_max_m of where A
--     really is (the centroid of pins whose label and Google pincode agree).
-- The distance test is what separates "the pincode next door" from plain
-- wrong pins on the other side of the city. Seed pairs come from the
-- reviewed app/data/pincode_neighbours.json. Learned pairs are listed in the
-- report for ops to review before they are added to that file.
CREATE OR REPLACE TABLE `{{eval}}.pincode_neighbours` AS
WITH pins AS (
  SELECT
    REGEXP_EXTRACT(receiverAddress, r'\b(\d{6})\b') AS label_pin,
    pincode                                        AS pin_pincode,
    ST_GEOGPOINT(longitude, latitude)              AS pt
  FROM `{{routing_table}}`
  WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND pincode IS NOT NULL
    AND created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @history_days DAY)
),
centroids AS (
  SELECT label_pin AS pin, ST_CENTROID_AGG(pt) AS centre
  FROM pins
  WHERE label_pin = pin_pincode
  GROUP BY label_pin
  HAVING COUNT(*) >= @min_pair_count
),
mismatches AS (
  SELECT
    p.label_pin                                                  AS a,
    p.pin_pincode                                                AS b,
    COUNT(*)                                                     AS observations,
    APPROX_QUANTILES(ST_DISTANCE(p.pt, c.centre), 2)[OFFSET(1)]  AS median_m
  FROM pins AS p
  JOIN centroids AS c ON c.pin = p.label_pin
  WHERE p.label_pin IS NOT NULL AND p.label_pin != p.pin_pincode
  GROUP BY a, b
),
learned AS (
  SELECT a, b, observations, median_m
  FROM mismatches
  WHERE observations >= @min_pair_count AND median_m <= @neighbour_max_m
),
seed AS (
  SELECT s.a, s.b FROM UNNEST({{seed_pairs}}) AS s
),
both_ways AS (
  SELECT a AS pincode, b AS neighbour, 'learned' AS source, observations, median_m FROM learned
  UNION ALL
  SELECT b, a, 'learned', observations, median_m FROM learned
  UNION ALL
  SELECT a, b, 'seed', CAST(NULL AS INT64), CAST(NULL AS FLOAT64) FROM seed
  UNION ALL
  SELECT b, a, 'seed', CAST(NULL AS INT64), CAST(NULL AS FLOAT64) FROM seed
)
SELECT
  pincode,
  neighbour,
  IF(LOGICAL_OR(source = 'seed'), 'seed', 'learned') AS source,
  MAX(observations)                                  AS observations,
  MIN(median_m)                                      AS median_m
FROM both_ways
GROUP BY pincode, neighbour;
