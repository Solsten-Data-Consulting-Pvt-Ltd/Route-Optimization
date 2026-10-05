-- §5.5 — 05: the wrong-building calls at a chosen threshold — read these
-- before any go/no-go decision. Uses the guarded pick (door numbers agree).
SELECT
  t.receiverAddress                       AS scanned_address,
  c.receiverAddress                       AS matched_to,
  ROUND(t.top1_guarded.cos_dist, 3)       AS cos_dist,
  ROUND(t.top1_guarded.pin_dist_m)        AS pins_apart_m,
  t.label_kind
FROM `{{eval}}.top1_{{run_tag}}` AS t
JOIN `{{eval}}.corpus` AS c ON c.address_key = t.top1_guarded.c_key
WHERE t.top1_guarded.cos_dist <= @threshold
  AND t.top1_guarded.pin_dist_m > @same_place_m
ORDER BY pins_apart_m DESC
LIMIT 100;
