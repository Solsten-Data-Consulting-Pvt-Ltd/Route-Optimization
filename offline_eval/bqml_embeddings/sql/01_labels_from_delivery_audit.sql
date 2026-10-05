-- §5.5 — 01a: ground truth from §5.4 EOD feedback (delivery_audit).
-- One labelled pin per (consignmentId, drsNo), latest debrief wins:
--   reasonCode A / D (waypoint wrong) -> the corrected pin (else the actual
--                                        delivery GPS)          'corrected'
--   no reasonCode and the delivery happened within @accurate_within_m of the
--   planned pin                        -> the planned pin was right
--                                                               'confirmed_accurate'
--   B (met elsewhere), C (sync lag), NOT_PROVIDED beyond the limit -> no label.
SELECT
  consignmentId,
  drsNo,
  IF(reasonCode IN ('A', 'D'), {{corrected_lat}}, plannedLatitude)  AS truth_lat,
  IF(reasonCode IN ('A', 'D'), {{corrected_lng}}, plannedLongitude) AS truth_lng,
  'delivery_audit'                                                  AS label_source,
  IF(reasonCode IN ('A', 'D'), 'corrected', 'confirmed_accurate')   AS label_kind,
  TIMESTAMP(eventTimestamp)                                         AS labelled_at
FROM `{{delivery_audit_table}}`
WHERE (reasonCode IN ('A', 'D') AND {{corrected_lat}} IS NOT NULL AND {{corrected_lng}} IS NOT NULL)
   OR (IFNULL(reasonCode, 'NOT_PROVIDED') = 'NOT_PROVIDED'
       AND deltaMetres IS NOT NULL AND deltaMetres <= @accurate_within_m
       AND plannedLatitude IS NOT NULL AND plannedLongitude IS NOT NULL)
QUALIFY ROW_NUMBER() OVER (PARTITION BY consignmentId, drsNo
                           ORDER BY TIMESTAMP(eventTimestamp) DESC) = 1
