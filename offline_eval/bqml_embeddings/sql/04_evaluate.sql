-- §5.5 — 04: offline replay + precision/recall, written to `results`.
--
-- Scopes: pincode (the address's own), pincode_neighbours (its own plus the
-- reviewed/learned neighbouring pincodes from 02b — still a bounded set of
-- partitions), geohash6 (the first resolved pin's cell).
--
-- Each labelled consignment Q in the last @query_days days is replayed as a
-- new scan. It is compared ONLY with addresses whose truth was known before Q
-- was created, inside Q's partition ({{scope}}):
--
--   HARD CONSTRAINT (spec §5.5): never search unconstrained. The join below is
--   the only place ML.DISTANCE runs, and it is always pre-scoped by
--   {{scope_predicate}}.
--
--   truth      : the candidate's labelled pin is within @same_place_m of Q's
--   prediction : the nearest candidate's cosine distance <= threshold
--   guard      : 'door_numbers' also requires identical door/plot numbers
--   slices     : all / prior_tiers_missed (master-waypoint AND geocache would
--                both have missed — where §5.5 would actually run) /
--                corrected_only (labels that were real corrections)

CREATE OR REPLACE TABLE `{{eval}}.top1_{{run_tag}}` AS
WITH corpus AS (
  SELECT c.*, e.embedding
  FROM `{{eval}}.corpus` AS c
  JOIN `{{eval}}.embeddings` AS e
    ON e.address_key = c.address_key AND e.content = c.content
),
queries AS (
  SELECT
    q.*,
    NOT (IFNULL(p.master_waypoint_hit, FALSE) OR IFNULL(p.geocache_hit, FALSE)) AS prior_tiers_missed,
    p.address_key IS NOT NULL AS prior_checked,
    {{search_pincodes_expr}} AS search_pincodes   -- partitions this query may search
  FROM corpus AS q
  LEFT JOIN `{{eval}}.prior_tier_check` AS p USING (address_key)
  WHERE q.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @query_days DAY)
    AND {{query_scope_not_null}}
),
pairs AS (
  SELECT
    q.address_key                                   AS q_key,
    c.address_key                                   AS c_key,
    ML.DISTANCE(q.embedding, c.embedding, 'COSINE') AS cos_dist,
    ST_DISTANCE(q.truth_pin, c.truth_pin)           AS pin_dist_m,
    q.door_numbers = c.door_numbers                 AS doors_agree
  FROM queries AS q
  JOIN corpus AS c
    ON {{scope_predicate}}                          -- SCOPE: the hard constraint
   AND c.labelled_at < q.created_at                 -- only what was known at scan time
   AND c.consignmentId != q.consignmentId
)
SELECT
  q.address_key,
  q.receiverAddress,
  q.label_kind,
  q.prior_tiers_missed,
  q.prior_checked,
  LOGICAL_OR(p.pin_dist_m <= @same_place_m)          AS has_true_match,
  COUNT(p.c_key)                                     AS candidates,
  ARRAY_AGG(IF(p.c_key IS NULL, NULL, STRUCT(p.c_key, p.cos_dist, p.pin_dist_m))
            IGNORE NULLS ORDER BY p.cos_dist LIMIT 1)[SAFE_OFFSET(0)]          AS top1,
  ARRAY_AGG(IF(p.doors_agree, STRUCT(p.c_key, p.cos_dist, p.pin_dist_m), NULL)
            IGNORE NULLS ORDER BY p.cos_dist LIMIT 1)[SAFE_OFFSET(0)]          AS top1_guarded
FROM queries AS q
LEFT JOIN pairs AS p ON p.q_key = q.address_key
GROUP BY 1, 2, 3, 4, 5;

CREATE TABLE IF NOT EXISTS `{{eval}}.results` (
  run_id          STRING,
  scope           STRING,
  slice           STRING,
  guard           STRING,
  threshold       FLOAT64,
  queries         INT64,
  had_a_match     INT64,
  said_same_place INT64,
  correct         INT64,
  wrong_building  INT64,
  precision       FLOAT64,
  recall          FLOAT64,
  created_at      TIMESTAMP
);

INSERT INTO `{{eval}}.results`
WITH sliced AS (
  SELECT t.*, slice
  FROM `{{eval}}.top1_{{run_tag}}` AS t,
       UNNEST(ARRAY_CONCAT(
         ['all'],
         IF(t.prior_checked AND t.prior_tiers_missed, ['prior_tiers_missed'], CAST([] AS ARRAY<STRING>)),
         IF(t.label_kind = 'corrected', ['corrected_only'], CAST([] AS ARRAY<STRING>))
       )) AS slice
),
picks AS (
  SELECT slice, 'none' AS guard, has_true_match,
         top1.cos_dist AS cos_dist, top1.pin_dist_m AS pin_dist_m
  FROM sliced
  UNION ALL
  SELECT slice, 'door_numbers', has_true_match,
         top1_guarded.cos_dist, top1_guarded.pin_dist_m
  FROM sliced
),
scored AS (
  SELECT
    slice, guard, t AS threshold,
    IFNULL(has_true_match, FALSE)                              AS has_true_match,
    IFNULL(cos_dist <= t, FALSE)                               AS predicted,
    IFNULL(cos_dist <= t AND pin_dist_m <= @same_place_m, FALSE) AS correct
  FROM picks, UNNEST(GENERATE_ARRAY(0.02, 0.40, 0.02)) AS t
)
SELECT
  '{{run_id}}', '{{scope}}', slice, guard, ROUND(threshold, 2),
  COUNT(*),
  COUNTIF(has_true_match),
  COUNTIF(predicted),
  COUNTIF(correct),
  COUNTIF(predicted AND NOT correct),
  SAFE_DIVIDE(COUNTIF(correct), COUNTIF(predicted)),
  SAFE_DIVIDE(COUNTIF(correct), COUNTIF(has_true_match)),
  CURRENT_TIMESTAMP()
FROM scored
GROUP BY slice, guard, threshold;

-- Coverage: how many labelled queries could not be scoped at all (no
-- partition key) and were therefore never searched.
SELECT
  COUNT(*)                                            AS labelled_in_window,
  COUNTIF(NOT ({{query_scope_not_null}}))             AS unscoped_never_searched
FROM `{{eval}}.corpus` AS q
WHERE q.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @query_days DAY);
