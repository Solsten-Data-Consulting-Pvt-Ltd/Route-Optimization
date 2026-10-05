-- §5.5 — 01b: interim ground truth until delivery_audit (§5.4) has data:
-- pins an executive/admin placed by hand (locationOverridden = TRUE).
SELECT
  consignmentId,
  drsNo,
  latitude                              AS truth_lat,
  longitude                             AS truth_lng,
  'routing_override'                    AS label_source,
  'corrected'                           AS label_kind,
  COALESCE(overriddenAt, updated_at)    AS labelled_at
FROM `{{routing_table}}`
WHERE locationOverridden IS TRUE
  AND latitude IS NOT NULL AND longitude IS NOT NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY consignmentId, drsNo
                           ORDER BY COALESCE(overriddenAt, updated_at) DESC) = 1
