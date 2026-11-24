# OSRM Phase 2 — offline accuracy test (23 Nov 2026)

Read-only test. Ran in Claude's cloud workspace; nothing in prod or dev changed.

## Setup

- Map: Geofabrik `southern-zone-261122.osm.pbf` (22 Nov 2026), cut to a Karnataka box (74.0–78.6 E, 11.5–18.5 N) → 213 MB.
- OSRM `osrm-backend:latest`, car profile, MLD. Build (extract + partition + customize) ≈ 5 min, peak RAM ≈ 1.8 GB. Server answers a 77×77 table in well under a second.
- Ground truth: Google Routes distances (`offline_eval/data/*.json`):
  - DHRD10230: 76 stops, 4,290 Google pairs
  - DHRD10239: 19 stops, 380 pairs
  - DHRD10240: 9 stops, 90 pairs
- Script: `offline_eval/compare.py` (same OR-Tools setup as `app/services/tsp.py`: open path from hub, PATH_CHEAPEST_ARC + GLS, 15 s / 3 s / 1 s).

## Per pair (OSRM ÷ Google distance)

| DRS | Median | p10 – p90 | Median abs diff | Pairs off by >25% and >500 m | Max snap |
|---|---|---|---|---|---|
| DHRD10230 | 0.995 | 0.83 – 1.08 | 89 m | 8.2% | 136 m |
| DHRD10239 | 0.991 | 0.87 – 1.02 | 72 m | 2.9% | 55 m |
| DHRD10240 | 0.992 | 0.79 – 1.16 | 158 m | 7.8% | 170 m |

Most pairs match closely. A small set of pairs is far off.

## Per route (km)

| DRS | Google route, Google km | OSRM route, Google km | Loss | Google route, OSRM km | OSRM route, OSRM km |
|---|---|---|---|---|---|
| DHRD10230 | 81.0 | 86.1 | +6.3% | 86.7 | 80.4 |
| DHRD10239 | 57.2 | 61.7 | +7.9% | 55.9 | 54.9 |
| DHRD10240 | 22.9 | 23.2 | +0.9% | 23.6 | 23.6 |

The disagreement runs both ways. On DHRD10230, Google's own route, measured on OSRM's map, is 7.8% longer than OSRM's route. So the two maps disagree about some roads. We can't tell from this data alone which one is right.

Solving on OSRM *duration* instead of distance was worse: 93.7 km (Google) on DHRD10230.

## Where they disagree (two patterns)

1. **Out of the hub (OSRM much shorter).** Example: hub → stop 38 on DHRD10230 is Google 13.4 km vs OSRM 8.3 km. OSRM goes Kudlu Road → Silver County Road → Central Jail Road (Parappana Agrahara). The same thing happens on DHRD10239 (hub → stops 7/8/10: Google 12.4–13.2 km, OSRM 8.9–9.6 km). Either Google avoids these roads (restricted/slow/closed) or OSM is missing a restriction. Needs a manual check on the ground or in Google Maps.
2. **Gated communities (OSRM much longer).** Example: stops 63 → 71 → 68 on DHRD10230. OSRM snaps stop 71 to the *Adarsh Palm Retreat Internal Road* and routes in through the gate (7.3 km vs Google 3.1 km). Google snaps to the outside road.
3. A few pairs where OSRM is about 4 km longer around Huskur (stops 43/49). This is likely a missing link in OSM.

## Takeaways

- OSRM is usable as a distance source: medians are within 1% and it's fast and cheap to run.
- It is **not yet a drop-in swap**. Routes differ by about 6–8% on two of the three DRS because of a few local road disagreements. Those can be fixed (OSM edits, profile tweaks, snapping rules), but they need checking first.
- Next steps before Phase 3:
  1. Verify Central Jail Road / Silver County Road and the Huskur link (drive or check in Google Maps). If OSM is wrong, fix it in OSM or block it in the profile.
  2. Snapping: try `exclude`/`approaches=curb` or snapping to roads outside gated layouts (`access=private` roads).
  3. Repeat on 5–10 more DRS (needs Google ground truth from `road_distance_cache`).
  4. If driver GPS tracks exist, use them as the real tie-breaker instead of Google.

## Fix attempts (23 Nov 2026, same day)

Route loss = OSRM's stop order measured in Google km vs Google's own order.

| Try | DHRD10230 | DHRD10239 | DHRD10240 |
|---|---|---|---|
| Default OSRM (baseline) | +6.3% | +7.9% | +0.9% |
| Never place a stop on a private road (profile `is_startpoint=false` for access=private) | +5.0% | **+19.6%** | +0.9% |
| Hybrid: move a stop to a public road only if within 50 / 100 / 150 m | +8.7 / +8.7 / +10.4% | +7.9% | +0.9% |
| Prefer main roads (residential/unclassified 12 km/h, service 6, living_street 5) | +6.9% | +7.9% | **+12.2%** |
| For reference: old straight-line method | +26.6% | +13.0% | +25.9% |
| For reference: Google with ±5% random noise | +0.2% | +0.0% | +0.0% |

None of the snapping or speed fixes helped.

What actually causes the gap: short legs inside neighbourhoods where OSRM finds a 100–900 m path and Google says 1.3–2.4 km. Examples on DHRD10230: 35→34 is OSRM 114 m vs Google 1,302 m (both directions); 66→65 is OSRM 460 m vs Google 2,310 m (Google's reverse is 891 m, so likely a one-way street). Of 87 such pairs, 25 look like one-way streets; the rest look like lanes that are closed, gated or not passable in reality, or a stop that Google places on a different road.

The central roads (Central Jail Road, Silver County Road, Kudlu Road) are normal public secondary/tertiary roads in OSM. Using Google's distances out of the hub changed nothing (open route, first stop is close anyway).

Mixing Google values (as if from cache) with OSRM for the rest: 50% Google gave +4.5% / +6.6%, and 80% Google gave +2.9% / +3.1%. Not a clean win.

**Conclusion:** this can't be fixed with profile settings. The gap is local street data (one-way streets, closed lanes). Since drivers navigate with Google Maps, Google's distances are closer to what they actually drive.

## Hybrid by distance: Google for nearby pairs, OSRM for far pairs

Pairs whose straight-line distance is under X km use Google; the rest use OSRM.

| X | DHRD10230 | DHRD10239 | DHRD10240 |
|---|---|---|---|
| 1 km (~11–22% of pairs Google) | +3.9% | +6.7% | +0.0% |
| 1.5 km (~21–33%) | +0.0% | +6.7% | +0.0% |
| 2 km (~34–38%) | +0.2% | +6.7% | +0.0% |
| 3 km (~56–67%) | +0.6% | +0.0% | +2.5% (≈0.6 km) |

At 3 km the hybrid matches Google's route quality, but it still needs about 60% of the Google calls, so it only saves about 40%.

## Options

1. **OSRM only:** ₹0 per lookup; routes about 6–8% longer than Google on big DRS (still far better than the old straight-line method at 13–27%).
2. **Hybrid (Google < 3 km, OSRM beyond):** same quality as Google; about 40% fewer Google calls.
3. **Google only (today):** best quality; currently inside the free tier.
4. **Google, with OSRM as fallback:** if the free tier runs out or Google fails, use OSRM instead of straight-line.
