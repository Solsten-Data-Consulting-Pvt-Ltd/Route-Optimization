"""Spec §5.5 — BQML address-embedding evaluation runner (offline only).

Builds the labelled address corpus, embeds it with text-embedding-005 through
a BQML remote model, replays recent consignments against earlier labelled
addresses INSIDE their pincode / geohash-6 partition, and writes a
precision/recall report. See README.md next to this file.

Guarantees, enforced here and by tests/test_bqml_embeddings_eval.py:

  * Gated by the `bqml_embeddings_eval` flag (app_config, §5.6). With the flag
    off nothing touches BigQuery or Vertex AI; --dry-run only prints SQL.
  * Never unconstrained: the only ML.DISTANCE call is pre-scoped by a
    partition predicate, and rendering refuses an empty/unknown scope.
  * Only rows where BOTH the master-waypoint tier (§5.3) and geocache.py's
    exact/fuzzy lookup would have missed form the decision slice
    ("prior_tiers_missed").
  * Nothing under app/ calls this; no production path calls
    ML.GENERATE_EMBEDDING.

Usage:
    python -m offline_eval.bqml_embeddings.run_eval --project prj-dev-hermes \\
        --location asia-south1 --connection prj-dev-hermes.asia-south1.vertex_embeddings \\
        --steps setup,corpus,prior,embed,evaluate,report
    python -m offline_eval.bqml_embeddings.run_eval ... --dry-run
"""

import argparse
import bisect
import csv
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("bqml_embeddings_eval")

FLAG_NAME = "bqml_embeddings_eval"
HERE = Path(__file__).resolve().parent
SQL_DIR = HERE / "sql"
REPORTS_DIR = HERE / "reports"

DEFAULT_DATASET = "route_opt_eval"
STEPS = ("setup", "corpus", "neighbours", "prior", "embed", "evaluate", "report")
DEFAULT_STEPS = ("corpus", "neighbours", "prior", "embed", "evaluate", "report")
REPO_ROOT = HERE.parents[1]
DEFAULT_NEIGHBOURS_FILE = REPO_ROOT / "app" / "data" / "pincode_neighbours.json"
PINCODE_RE = re.compile(r"^\d{6}$")

# The hard constraint (spec §5.5): every search is inside a bounded set of
# partitions. `predicate` joins query q to candidate c; there is deliberately
# no "unscoped"/"none" option. `search` is the list of pincode partitions a
# query may search: its own, or its own plus its neighbouring pincodes
# ({eval} is filled in by scope_params).
_OWN_PINCODE = "IF(q.scope_pincode IS NULL, CAST([] AS ARRAY<STRING>), [q.scope_pincode])"
SCOPES = {
    "pincode": {
        "predicate": "c.scope_pincode = q.scope_pincode",
        "query_not_null": "q.scope_pincode IS NOT NULL",
        "search": _OWN_PINCODE,
    },
    "pincode_neighbours": {
        "predicate": "c.scope_pincode IN UNNEST(q.search_pincodes)",
        "query_not_null": "q.scope_pincode IS NOT NULL",
        "search": ("IF(q.scope_pincode IS NULL, CAST([] AS ARRAY<STRING>), ARRAY_CONCAT("
                   "[q.scope_pincode], ARRAY(SELECT n.neighbour FROM `{eval}.pincode_neighbours` AS n "
                   "WHERE n.pincode = q.scope_pincode)))"),
    },
    "geohash6": {
        "predicate": "c.truth_geohash6 = q.initial_geohash6",
        "query_not_null": "q.initial_geohash6 IS NOT NULL",
        "search": _OWN_PINCODE,
    },
}

# Columns the §5.4 delivery_audit table must have (spec §5.4 step 4, names as
# in EodDeliveryFeedbackRequest). correctedLatitude/Longitude are optional.
DELIVERY_AUDIT_REQUIRED = (
    "consignmentId", "drsNo", "plannedLatitude", "plannedLongitude",
    "actualLatitude", "actualLongitude", "deltaMetres", "reasonCode", "eventTimestamp",
)
ROUTING_OVERRIDE_REQUIRED = (
    "consignmentId", "drsNo", "latitude", "longitude", "locationOverridden",
    "overriddenAt", "updated_at", "receiverAddress", "created_at",
)

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


class EvalError(RuntimeError):
    """A configuration problem the user must fix; printed without a traceback."""


# ---------------------------------------------------------------------------
# SQL rendering
# ---------------------------------------------------------------------------
def render(template_name: str, **params) -> str:
    """Fill {{placeholders}} in sql/<template_name>. Every placeholder must be
    supplied and every supplied value used, so a typo can't silently leave a
    template half-rendered."""
    text = (SQL_DIR / template_name).read_text(encoding="utf-8")
    needed = set(PLACEHOLDER_RE.findall(text))
    missing = needed - params.keys()
    unused = params.keys() - needed
    if missing:
        raise EvalError(f"{template_name}: missing placeholder(s) {sorted(missing)}")
    if unused:
        raise EvalError(f"{template_name}: unused placeholder(s) {sorted(unused)}")
    for key in needed:
        value = str(params[key])
        if not value.strip():
            raise EvalError(f"{template_name}: placeholder {key!r} is empty")
        text = text.replace("{{" + key + "}}", value)
    return text


def scope_params(scope: str, eval_ds: str = "<eval>") -> dict:
    if scope not in SCOPES:
        raise EvalError(f"unknown scope {scope!r}; choose one of {sorted(SCOPES)} "
                        "(an unconstrained search is not an option, spec §5.5)")
    s = SCOPES[scope]
    return {"scope": scope, "scope_predicate": s["predicate"],
            "query_scope_not_null": s["query_not_null"],
            "search_pincodes_expr": s["search"].replace("{eval}", eval_ds)}


def load_seed_pairs(path) -> list:
    """Reviewed neighbour pairs from app/data/pincode_neighbours.json (the same
    file the live code reads). Missing file -> no seed pairs."""
    path = Path(path) if path else None
    if not path or not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    pairs = []
    for pair in data.get("pairs") or []:
        a, b = str(pair[0]).strip(), str(pair[1]).strip()
        if PINCODE_RE.match(a) and PINCODE_RE.match(b) and a != b:
            pairs.append((a, b))
    return pairs


def seed_pairs_sql(pairs) -> str:
    """Typed array literal; pincodes are validated as 6 digits, so inlining is safe."""
    for a, b in pairs:
        if not (PINCODE_RE.match(a) and PINCODE_RE.match(b)):
            raise EvalError(f"invalid pincode pair {(a, b)!r}")
    items = ", ".join(f"('{a}', '{b}')" for a, b in pairs)
    return f"ARRAY<STRUCT<a STRING, b STRING>>[{items}]"


def run_tag(run_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", run_id)


def labels_select(label_sources, *, delivery_audit_table, routing_table, has_corrected_cols):
    parts = []
    if "delivery_audit" in label_sources:
        corrected_lat = ("COALESCE(correctedLatitude, actualLatitude)"
                         if has_corrected_cols else "actualLatitude")
        corrected_lng = ("COALESCE(correctedLongitude, actualLongitude)"
                         if has_corrected_cols else "actualLongitude")
        parts.append(render("01_labels_from_delivery_audit.sql",
                            delivery_audit_table=delivery_audit_table,
                            corrected_lat=corrected_lat, corrected_lng=corrected_lng))
    if "routing_overrides" in label_sources:
        parts.append(render("01_labels_from_routing_overrides.sql",
                            routing_table=routing_table))
    if not parts:
        raise EvalError("no label source available")
    # Strip trailing comments/whitespace so UNION ALL stays valid.
    return "\nUNION ALL\n".join(f"(\n{p.strip()}\n)" for p in parts)


# ---------------------------------------------------------------------------
# Prior tiers: would master-waypoint (§5.3) or geocache.py have answered?
# ---------------------------------------------------------------------------
def _ts(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "timestamp"):
        return value.timestamp()
    return 0.0


def prior_tier_hits(queries, waypoint_entries, cache_entries, normalize, fuzzy_threshold):
    """For each query {address_key, address, created_at} decide whether the
    master-waypoint tier or the verified geocache would have answered it at
    the time it was scanned (entries verified later don't count; entries with
    no timestamp are assumed known, which can only shrink the decision slice).

    waypoint_entries / cache_entries: iterables of (normalized_text, ts).
    Matching mirrors the live code: master-waypoint = exact normalized alias;
    geocache = exact normalized address, else token_sort_ratio >= threshold.
    """
    from rapidfuzz import fuzz, process, utils

    wp_first_seen = {}
    for text, ts in waypoint_entries:
        if text:
            wp_first_seen[text] = min(wp_first_seen.get(text, float("inf")), _ts(ts))

    cache_sorted = sorted(((_ts(ts), text) for text, ts in cache_entries if text),
                          key=lambda x: x[0])
    cache_times = [t for t, _ in cache_sorted]
    cache_texts = [text for _, text in cache_sorted]
    cache_first_seen = {}
    for t, text in cache_sorted:
        cache_first_seen.setdefault(text, t)

    out = []
    for q in queries:
        normalized = normalize(q.get("address") or "")
        q_ts = _ts(q.get("created_at"))
        mw_hit = bool(normalized) and wp_first_seen.get(normalized, float("inf")) <= q_ts
        gc_hit = False
        if normalized:
            if cache_first_seen.get(normalized, float("inf")) <= q_ts:
                gc_hit = True
            else:
                known = cache_texts[:bisect.bisect_right(cache_times, q_ts)]
                if known and process.extractOne(
                        normalized, known, scorer=fuzz.token_sort_ratio,
                        processor=utils.default_process, score_cutoff=fuzzy_threshold):
                    gc_hit = True
        out.append({"address_key": q["address_key"],
                    "master_waypoint_hit": mw_hit, "geocache_hit": gc_hit})
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def choose_threshold(rows, *, slice_="prior_tiers_missed", guard="door_numbers",
                     min_precision=0.995, min_predictions=20):
    """Highest-recall threshold whose precision is >= min_precision, using
    only thresholds that made at least `min_predictions` calls (a perfect
    score on 3 calls proves nothing). None if no threshold qualifies."""
    best = None
    for r in rows:
        if r["slice"] != slice_ or r["guard"] != guard:
            continue
        if (r["said_same_place"] or 0) < min_predictions or r["precision"] is None:
            continue
        if r["precision"] < min_precision:
            continue
        if best is None or (r["recall"] or 0) > (best["recall"] or 0) or (
                (r["recall"] or 0) == (best["recall"] or 0) and r["threshold"] < best["threshold"]):
            best = r
    return best


def _pct(v):
    return "–" if v is None else f"{v * 100:.1f}%"


def render_report(meta, rows, recommendation, coverage=None, min_precision=0.995,
                  learned_neighbours=None):
    lines = [
        "# §5.5 address-embedding evaluation",
        "",
        f"Run `{meta['run_id']}` · {meta['generated_at']} · scope **{meta['scope']}** · "
        f"labels: {', '.join(meta['label_sources'])} · same place = pins within "
        f"{meta['same_place_m']:g} m · replayed last {meta['query_days']} days",
        "",
    ]
    if recommendation:
        lines += [
            f"**Recommendation:** threshold **{recommendation['threshold']:.2f}** "
            f"(guard `{recommendation['guard']}`, slice `{recommendation['slice']}`): "
            f"precision {_pct(recommendation['precision'])}, recall {_pct(recommendation['recall'])}, "
            f"{recommendation['wrong_building']} wrong-building call(s) out of "
            f"{recommendation['said_same_place']}.",
        ]
    else:
        lines += [f"**Recommendation:** no threshold reaches {min_precision * 100:.1f}% precision "
                  "with enough calls — do not wire this into save-consignments."]
    if coverage:
        lines += ["", f"Coverage: {coverage['labelled_in_window']} labelled consignments in the window; "
                      f"{coverage['unscoped_never_searched']} had no partition key and were never searched."]
    lines += ["", "| Slice | Guard | Threshold | Queries | Had a match | Said same | Correct | "
                  "Wrong building | Precision | Recall |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in sorted(rows, key=lambda r: (r["slice"], r["guard"], r["threshold"])):
        lines.append(
            f"| {r['slice']} | {r['guard']} | {r['threshold']:.2f} | {r['queries']} | "
            f"{r['had_a_match']} | {r['said_same_place']} | {r['correct']} | "
            f"{r['wrong_building']} | {_pct(r['precision'])} | {_pct(r['recall'])} |")
    lines += ["", "Precision = right calls / all \"same place\" calls (a wrong call sends a "
                  "parcel to the wrong building). Recall = right calls / queries that really had "
                  "a same-place address earlier in their partition.", ""]
    if learned_neighbours:
        lines += ["## Learned neighbouring pincodes — review before adding to "
                  "`app/data/pincode_neighbours.json`", "",
                  "| Written pincode | Pin actually in | Observations | Median distance from written pincode |",
                  "|---|---|---:|---:|"]
        for n in learned_neighbours:
            lines.append(f"| {n['pincode']} | {n['neighbour']} | {n['observations']} | "
                         f"{(n['median_m'] or 0) / 1000:.1f} km |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# BigQuery / Firestore plumbing (not exercised by unit tests)
# ---------------------------------------------------------------------------
def _flag_enabled() -> bool:
    from app.db import feature_flags  # needs the app's env vars
    return feature_flags.is_enabled(FLAG_NAME)


def _bq(args):
    from google.cloud import bigquery
    return bigquery.Client(project=args.project, location=args.location)


def _params(**values):
    from google.cloud import bigquery
    kinds = {int: "INT64", float: "FLOAT64"}
    return [bigquery.ScalarQueryParameter(k, kinds[type(v)], v) for k, v in values.items()]


def _columns(client, table_id):
    from google.api_core.exceptions import NotFound
    try:
        return {f.name for f in client.get_table(table_id).schema}
    except NotFound:
        return None


def _run(client, sql, args, **params):
    from google.cloud import bigquery
    config = bigquery.QueryJobConfig(query_parameters=_params(**params))
    job = client.query(sql, job_config=config)
    rows = [dict(r.items()) for r in job.result()]
    logger.info("job %s done (%s bytes billed)", job.job_id, getattr(job, "total_bytes_billed", "?"))
    return rows


def resolve_sources(args, client=None):
    """Decide label sources and column expressions, checking the real tables
    when a client is given (dry-run assumes everything exists)."""
    routing_cols = _columns(client, args.routing_table) if client else set(ROUTING_OVERRIDE_REQUIRED) | {"planned_latitude", "planned_longitude"}
    if routing_cols is None:
        raise EvalError(f"routing table {args.routing_table} not found")
    audit_cols = _columns(client, args.delivery_audit_table) if client else set(DELIVERY_AUDIT_REQUIRED)

    sources = []
    wanted = args.labels
    if wanted in ("auto", "delivery_audit"):
        if audit_cols is None:
            if wanted == "delivery_audit":
                raise EvalError(f"{args.delivery_audit_table} does not exist yet (spec §5.4 not built)")
        else:
            missing = [c for c in DELIVERY_AUDIT_REQUIRED if c not in audit_cols]
            if missing:
                raise EvalError(f"{args.delivery_audit_table} is missing column(s) {missing} "
                                "(expected the §5.4 contract, see README)")
            sources.append("delivery_audit")
    if wanted in ("auto", "routing_overrides"):
        missing = [c for c in ROUTING_OVERRIDE_REQUIRED if c not in routing_cols]
        if missing:
            raise EvalError(f"{args.routing_table} is missing column(s) {missing}")
        sources.append("routing_overrides")

    has_planned = {"planned_latitude", "planned_longitude"} <= routing_cols
    if args.scope == "geohash6" and not has_planned:
        raise EvalError("geohash6 scope needs consignments_routing.planned_latitude/longitude "
                        "(the first pin, before any correction); use --scope pincode")
    has_corrected = audit_cols is not None and {"correctedLatitude", "correctedLongitude"} <= audit_cols
    return {
        "label_sources": sources,
        "initial_lat": "r.planned_latitude" if has_planned else "CAST(NULL AS FLOAT64)",
        "initial_lng": "r.planned_longitude" if has_planned else "CAST(NULL AS FLOAT64)",
        "has_corrected_cols": has_corrected,
    }


def build_sql(args, src, run_id):
    eval_ds = f"{args.project}.{args.dataset}"
    tag = run_tag(run_id)
    return {
        "setup": render("00_setup.sql", eval=eval_ds, location=args.location,
                        connection=args.connection or "<connection>"),
        "corpus": render("02_corpus.sql", eval=eval_ds, routing_table=args.routing_table,
                         initial_lat=src["initial_lat"], initial_lng=src["initial_lng"],
                         labels_select=labels_select(
                             src["label_sources"], delivery_audit_table=args.delivery_audit_table,
                             routing_table=args.routing_table,
                             has_corrected_cols=src["has_corrected_cols"])),
        "neighbours": render("02b_pincode_neighbours.sql", eval=eval_ds,
                             routing_table=args.routing_table,
                             seed_pairs=seed_pairs_sql(load_seed_pairs(args.neighbours_file))),
        "embed": render("03_embed.sql", eval=eval_ds),
        "evaluate": render("04_evaluate.sql", eval=eval_ds, run_id=run_id, run_tag=tag,
                           **scope_params(args.scope, eval_ds)),
        "mistakes": render("05_mistakes.sql", eval=eval_ds, run_tag=tag),
    }


def _prior_step(client, args):
    from google.cloud import bigquery
    from app.config import GEOCODE_CACHE_FUZZY_THRESHOLD
    from app.db.clients import get_fs_client
    from app.db.geocache import GEOCODE_CACHE_COLLECTION, normalize_address
    from app.db.master_waypoint import MASTER_WAYPOINT_COLLECTION

    eval_ds = f"{args.project}.{args.dataset}"
    queries = _run(client, f"""
        SELECT address_key, receiverAddress AS address, created_at
        FROM `{eval_ds}.corpus`
        WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @query_days DAY)""",
                   args, query_days=args.query_days)
    fs = get_fs_client()   # read-only
    waypoints = [((d.to_dict() or {}).get("alias_normalized"),
                  (d.to_dict() or {}).get("verified_at") or (d.to_dict() or {}).get("created_at"))
                 for d in fs.collection(MASTER_WAYPOINT_COLLECTION).where("verified", "==", True).stream()]
    cache = [((d.to_dict() or {}).get("address_normalized"),
              (d.to_dict() or {}).get("verified_at") or (d.to_dict() or {}).get("created_at"))
             for d in fs.collection(GEOCODE_CACHE_COLLECTION).where("verified", "==", True).stream()]
    logger.info("prior tiers: %d queries, %d waypoints, %d verified cache entries",
                len(queries), len(waypoints), len(cache))
    hits = prior_tier_hits(queries, waypoints, cache, normalize_address, GEOCODE_CACHE_FUZZY_THRESHOLD)
    schema = [bigquery.SchemaField("address_key", "STRING"),
              bigquery.SchemaField("master_waypoint_hit", "BOOL"),
              bigquery.SchemaField("geocache_hit", "BOOL")]
    job = client.load_table_from_json(
        hits, f"{eval_ds}.prior_tier_check",
        job_config=bigquery.LoadJobConfig(schema=schema, write_disposition="WRITE_TRUNCATE"))
    job.result()
    return hits


def parse_args(argv=None):
    env = os.environ
    default_routing = (f"{env['BQ_PROJECT']}.{env['BQ_DATASET']}.{env['BQ_TABLE']}"
                       if all(k in env for k in ("BQ_PROJECT", "BQ_DATASET", "BQ_TABLE")) else None)
    default_audit = (f"{env['BQ_PROJECT']}.{env['BQ_DATASET']}.delivery_audit"
                     if all(k in env for k in ("BQ_PROJECT", "BQ_DATASET")) else None)
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", default=env.get("BQ_PROJECT"), required=not env.get("BQ_PROJECT"))
    p.add_argument("--location", required=True, help="same location as the routing dataset")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--connection", help="<project>.<location>.<connection_id>, for --steps setup")
    p.add_argument("--routing-table", default=default_routing, required=default_routing is None)
    p.add_argument("--delivery-audit-table", default=default_audit, required=default_audit is None)
    p.add_argument("--labels", choices=("auto", "delivery_audit", "routing_overrides"), default="auto")
    p.add_argument("--scope", choices=sorted(SCOPES), default="pincode")
    p.add_argument("--steps", default=",".join(DEFAULT_STEPS))
    p.add_argument("--history-days", type=int, default=180)
    p.add_argument("--query-days", type=int, default=30)
    p.add_argument("--same-place-m", type=float, default=50.0)
    p.add_argument("--accurate-within-m", type=float, default=100.0)
    p.add_argument("--min-precision", type=float, default=0.995)
    p.add_argument("--neighbours-file", default=str(DEFAULT_NEIGHBOURS_FILE),
                   help="reviewed neighbour pairs (same file the app reads)")
    p.add_argument("--min-pair-count", type=int, default=5,
                   help="observations needed to learn a neighbouring-pincode pair")
    p.add_argument("--neighbour-max-m", type=float, default=6000.0,
                   help="learned pair: median distance of its pins from the written pincode")
    p.add_argument("--mistakes-threshold", type=float, default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--dry-run", action="store_true", help="print the SQL; touch nothing")
    args = p.parse_args(argv)
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    bad = [s for s in steps if s not in STEPS]
    if bad:
        p.error(f"unknown step(s) {bad}; choose from {list(STEPS)}")
    if "setup" in steps and not args.connection and not args.dry_run:
        p.error("--steps setup needs --connection")
    args.steps = steps
    return args


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    try:
        if args.dry_run:
            sql = build_sql(args, resolve_sources(args, client=None), run_id)
            for step in args.steps:
                if step in sql:
                    print(f"-- ===== step: {step} =====\n{sql[step]}\n")
            if "evaluate" in args.steps:
                print(f"-- ===== step: mistakes =====\n{sql['mistakes']}\n")
            return 0

        if not _flag_enabled():
            print(f"Flag app_config/{FLAG_NAME} is off — nothing run. Turn it on (dev first) "
                  "to allow this evaluation to spend BigQuery/Vertex AI quota, or use --dry-run.",
                  file=sys.stderr)
            return 2

        client = _bq(args)
        src = resolve_sources(args, client)
        logger.info("label sources: %s", src["label_sources"])
        sql = build_sql(args, src, run_id)
        coverage = None
        if "setup" in args.steps:
            _run(client, sql["setup"], args)
        if "corpus" in args.steps:
            _run(client, sql["corpus"], args, history_days=args.history_days,
                 accurate_within_m=args.accurate_within_m)
        if "neighbours" in args.steps:
            _run(client, sql["neighbours"], args, history_days=args.history_days,
                 min_pair_count=args.min_pair_count, neighbour_max_m=args.neighbour_max_m)
        if "prior" in args.steps:
            _prior_step(client, args)
        if "embed" in args.steps:
            previous = None
            for _ in range(3):   # retry rows that failed transiently
                missing = _run(client, sql["embed"], args)[0]["still_missing"]
                logger.info("embeddings still missing: %s", missing)
                if missing == 0 or missing == previous:
                    break
                previous = missing
        if "evaluate" in args.steps:
            rows = _run(client, sql["evaluate"], args, query_days=args.query_days,
                        same_place_m=args.same_place_m)
            coverage = rows[0] if rows else None
        if "report" in args.steps:
            rows = _fetch_results(client, args, run_id)
            rec = choose_threshold(rows, min_precision=args.min_precision)
            learned = _fetch_learned_neighbours(client, args)
            meta = {"run_id": run_id, "scope": args.scope, "label_sources": src["label_sources"],
                    "same_place_m": args.same_place_m, "query_days": args.query_days,
                    "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
            REPORTS_DIR.mkdir(exist_ok=True)
            md = REPORTS_DIR / f"{run_tag(run_id)}.md"
            md.write_text(render_report(meta, rows, rec, coverage, args.min_precision, learned),
                          encoding="utf-8")
            with open(REPORTS_DIR / f"{run_tag(run_id)}.csv", "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["slice"])
                writer.writeheader()
                writer.writerows(rows)
            mistakes_t = args.mistakes_threshold or (rec["threshold"] if rec else None)
            if mistakes_t is not None:
                mistakes = _run(client, sql["mistakes"], args, threshold=float(mistakes_t),
                                same_place_m=args.same_place_m)
                with open(REPORTS_DIR / f"{run_tag(run_id)}_mistakes.csv", "w", newline="",
                          encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(mistakes[0].keys()) if mistakes else ["scanned_address"])
                    writer.writeheader()
                    writer.writerows(mistakes)
            print(f"Report: {md}")
        return 0
    except EvalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _fetch_results(client, args, run_id):
    from google.cloud import bigquery
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("run_id", "STRING", run_id)])
    job = client.query(
        f"SELECT slice, guard, threshold, queries, had_a_match, said_same_place, correct, "
        f"wrong_building, precision, recall FROM `{args.project}.{args.dataset}.results` "
        f"WHERE run_id = @run_id", job_config=config)
    return [dict(r.items()) for r in job.result()]



def _fetch_learned_neighbours(client, args):
    from google.api_core.exceptions import NotFound
    try:
        job = client.query(
            f"SELECT pincode, neighbour, observations, median_m "
            f"FROM `{args.project}.{args.dataset}.pincode_neighbours` "
            f"WHERE source = 'learned' AND pincode < neighbour "
            f"ORDER BY observations DESC LIMIT 50")
        return [dict(r.items()) for r in job.result()]
    except NotFound:
        return []


if __name__ == "__main__":
    sys.exit(main())
