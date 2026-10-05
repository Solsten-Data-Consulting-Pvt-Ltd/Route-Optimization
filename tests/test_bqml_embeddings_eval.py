"""Tests for the §5.5 offline embedding evaluation (offline_eval/bqml_embeddings).

No BigQuery, Vertex AI or Firestore access. These pin the spec's guarantees:
never an unconstrained search, flag-gated, and no production path touching
ML.GENERATE_EMBEDDING.
"""

import io
import os
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-key")
os.environ.setdefault("BQ_PROJECT", "test-project")
os.environ.setdefault("BQ_DATASET", "test_dataset")
os.environ.setdefault("BQ_TABLE", "test_table")
os.environ.setdefault("BQ_STRUCTURED_TABLE", "test_structured_table")
os.environ.setdefault("FIRESTORE_PROJECT", "test-project")

from app.db.geocache import normalize_address
from offline_eval.bqml_embeddings import run_eval as r

REPO = Path(__file__).resolve().parents[1]
SEARCH_RE = re.compile(r"\b(ML\.DISTANCE|VECTOR_SEARCH)\b", re.IGNORECASE)


def _args(*extra):
    return r.parse_args(["--location", "asia-south1", "--connection", "p.asia-south1.c",
                         "--steps", "setup,corpus,embed,evaluate", "--dry-run", *extra])


def _sql(scope="pincode", has_corrected=True, labels=("delivery_audit", "routing_overrides")):
    args = _args("--scope", scope)
    src = r.resolve_sources(args)
    src["has_corrected_cols"] = has_corrected
    src["label_sources"] = list(labels)
    return r.build_sql(args, src, "20261005T010203Z-abc123")


class ScopeConstraintTests(unittest.TestCase):
    """Spec §5.5 hard constraint: never search unconstrained."""

    def test_every_template_that_searches_is_scoped(self):
        searching = [p for p in r.SQL_DIR.glob("*.sql") if SEARCH_RE.search(p.read_text())]
        self.assertEqual([p.name for p in searching], ["04_evaluate.sql"])
        for p in searching:
            text = p.read_text()
            for m in SEARCH_RE.finditer(text):
                # the distance is computed in the pairs CTE whose JOIN is scoped
                tail = text[m.end():]
                join = tail[:tail.index("AND c.labelled_at")]
                self.assertIn("{{scope_predicate}}", join)

    def test_rendered_predicate_for_each_scope(self):
        for scope, spec in r.SCOPES.items():
            sql = _sql(scope)["evaluate"]
            self.assertIn(f"ON {spec['predicate']}", sql)
            self.assertNotIn("{{", sql)

    def test_no_unscoped_option_exists(self):
        for bad in ("none", "all", "", "global"):
            with self.assertRaises(r.EvalError):
                r.scope_params(bad)
        for spec in r.SCOPES.values():
            self.assertRegex(spec["predicate"], r"^c\.\w+ = q\.\w+$")

    def test_render_rejects_missing_unused_or_empty_placeholders(self):
        with self.assertRaises(r.EvalError):
            r.render("04_evaluate.sql", eval="p.d")
        params = dict(eval="p.d", run_id="x", run_tag="x", **r.scope_params("pincode"))
        with self.assertRaises(r.EvalError):
            r.render("04_evaluate.sql", **params, extra="nope")
        with self.assertRaises(r.EvalError):
            r.render("04_evaluate.sql", **{**params, "scope_predicate": "  "})

    def test_candidates_only_from_before_the_scan(self):
        self.assertIn("c.labelled_at < q.created_at", _sql()["evaluate"])


class BlastRadiusTests(unittest.TestCase):
    """§5.5: no production code path calls ML.GENERATE_EMBEDDING."""

    def test_app_never_references_the_evaluation(self):
        pattern = re.compile(r"GENERATE_EMBEDDING|VECTOR_SEARCH|ML\.DISTANCE|route_opt_eval|offline_eval")
        hits = [str(p) for p in (REPO / "app").rglob("*.py") if pattern.search(p.read_text())]
        self.assertEqual(hits, [])

    def test_container_ships_only_app(self):
        copies = [l for l in (REPO / "Dockerfile").read_text().splitlines()
                  if l.strip().upper().startswith("COPY")]
        self.assertFalse([l for l in copies if "offline_eval" in l or l.split()[1] == "."])


class LabelTests(unittest.TestCase):
    def test_delivery_audit_uses_corrected_pin_when_present(self):
        sql = _sql(has_corrected=True)["corpus"]
        self.assertIn("COALESCE(correctedLatitude, actualLatitude)", sql)
        self.assertIn("UNION ALL", sql)

    def test_delivery_audit_falls_back_to_actual_gps(self):
        sql = _sql(has_corrected=False, labels=("delivery_audit",))["corpus"]
        self.assertNotIn("correctedLatitude", sql)
        self.assertIn("IF(reasonCode IN ('A', 'D'), actualLatitude, plannedLatitude)", sql)
        self.assertNotIn("routing_override", sql)

    def test_only_a_and_d_are_corrections(self):
        sql = r.SQL_DIR.joinpath("01_labels_from_delivery_audit.sql").read_text()
        self.assertIn("reasonCode IN ('A', 'D')", sql)
        self.assertNotIn("'B'", sql.split("--")[-1])

    def test_routing_overrides_only(self):
        sql = _sql(labels=("routing_overrides",))["corpus"]
        self.assertIn("locationOverridden IS TRUE", sql)
        self.assertNotIn("reasonCode", sql)           # no delivery_audit SELECT

    def test_resolve_sources_checks_real_columns(self):
        args = r.parse_args(["--location", "asia-south1"])

        class _Client:
            def __init__(self, tables):
                self.tables = tables

        tables = {args.routing_table: set(r.ROUTING_OVERRIDE_REQUIRED),
                  args.delivery_audit_table: {"consignmentId", "drsNo"}}
        with patch.object(r, "_columns", side_effect=lambda c, t: tables.get(t)):
            with self.assertRaisesRegex(r.EvalError, "missing column"):
                r.resolve_sources(args, _Client(tables))
            tables.pop(args.delivery_audit_table)        # §5.4 not built yet
            src = r.resolve_sources(args, _Client(tables))
            self.assertEqual(src["label_sources"], ["routing_overrides"])
            self.assertEqual(src["initial_lat"], "CAST(NULL AS FLOAT64)")
            geo = r.parse_args(["--location", "asia-south1", "--scope", "geohash6"])
            with self.assertRaisesRegex(r.EvalError, "planned_latitude"):
                r.resolve_sources(geo, _Client(tables))


def _t(day):
    return datetime(2026, 9, day, tzinfo=timezone.utc)


class PriorTierTests(unittest.TestCase):
    """Only rows where master-waypoint AND geocache would both miss count."""

    def hits(self, queries, waypoints=(), cache=()):
        rows = r.prior_tier_hits(queries, waypoints, cache, normalize_address, 85)
        return {h["address_key"]: (h["master_waypoint_hit"], h["geocache_hit"]) for h in rows}

    def test_master_waypoint_exact_alias(self):
        q = [{"address_key": "k", "address": "Prestige Lakeside Habitat, Varthur", "created_at": _t(20)}]
        wp = [(normalize_address("prestige lakeside habitat varthur"), _t(1))]
        self.assertEqual(self.hits(q, wp)["k"], (True, False))

    def test_geocache_fuzzy_at_85_like_live_code(self):
        q = [{"address_key": "same", "address": "12 Sarjapur Road, Bangalore 560035", "created_at": _t(20)},
             {"address_key": "other", "address": "456 Sarjapur Road", "created_at": _t(20)}]
        cache = [(normalize_address("12, Sarjapura Road, Bengaluru"), _t(1)),
                 (normalize_address("123 Sarjapur Road"), _t(1))]
        h = self.hits(q, cache=cache)
        self.assertEqual(h["same"], (False, True))     # exact after normalisation
        self.assertEqual(h["other"], (False, False))   # 83.3 < 85, as in geocache.py

    def test_entries_verified_after_the_scan_do_not_count(self):
        q = [{"address_key": "k", "address": "No 140/1 Kodathi Gate", "created_at": _t(10)}]
        later = [(normalize_address("No 140/1 Kodathi Gate"), _t(15))]
        self.assertEqual(self.hits(q, later, later)["k"], (False, False))

    def test_entries_without_timestamp_are_assumed_known(self):
        q = [{"address_key": "k", "address": "No 140/1 Kodathi Gate", "created_at": _t(10)}]
        self.assertEqual(self.hits(q, cache=[(normalize_address("No 140/1 Kodathi Gate"), None)])["k"],
                         (False, True))


def _row(threshold, said, correct, had=100, slice_="prior_tiers_missed", guard="door_numbers"):
    return {"slice": slice_, "guard": guard, "threshold": threshold, "queries": 200,
            "had_a_match": had, "said_same_place": said, "correct": correct,
            "wrong_building": said - correct,
            "precision": correct / said if said else None, "recall": correct / had}


class ReportTests(unittest.TestCase):
    def test_choose_highest_recall_meeting_precision(self):
        rows = [_row(0.04, 30, 30), _row(0.08, 60, 60), _row(0.12, 90, 85),
                _row(0.20, 95, 95, guard="none")]          # other guard ignored
        self.assertEqual(r.choose_threshold(rows)["threshold"], 0.08)

    def test_too_few_calls_or_precision_never_reached(self):
        self.assertIsNone(r.choose_threshold([_row(0.02, 5, 5)]))
        self.assertIsNone(r.choose_threshold([_row(0.10, 100, 90)]))

    def test_report_states_no_go_and_lists_rows(self):
        meta = {"run_id": "x", "generated_at": "now", "scope": "pincode",
                "label_sources": ["routing_overrides"], "same_place_m": 50.0, "query_days": 30}
        md = r.render_report(meta, [_row(0.10, 100, 90)], None,
                             {"labelled_in_window": 500, "unscoped_never_searched": 40})
        self.assertIn("do not wire this into save-consignments", md)
        self.assertIn("| prior_tiers_missed | door_numbers | 0.10 |", md)
        self.assertIn("40 had no partition key", md)


class FlagGateTests(unittest.TestCase):
    def test_flag_off_touches_nothing(self):
        err = io.StringIO()
        with patch.object(r, "_flag_enabled", return_value=False), \
             patch.object(r, "_bq") as bq, redirect_stderr(err):
            code = r.main(["--location", "asia-south1"])
        self.assertEqual(code, 2)
        bq.assert_not_called()
        self.assertIn("bqml_embeddings_eval", err.getvalue())

    def test_dry_run_needs_no_flag_and_no_client(self):
        out = io.StringIO()
        with patch.object(r, "_flag_enabled") as flag, patch.object(r, "_bq") as bq, \
             redirect_stdout(out):
            code = r.main(["--location", "asia-south1", "--dry-run"])
        self.assertEqual(code, 0)
        flag.assert_not_called()
        bq.assert_not_called()
        self.assertIn("ML.GENERATE_EMBEDDING", out.getvalue())

    def test_flag_is_the_one_seeded_by_feature_flags(self):
        from app.db import feature_flags
        self.assertIn(r.FLAG_NAME, feature_flags.DEFAULT_FLAGS)
        self.assertEqual(feature_flags.DEFAULT_FLAGS[r.FLAG_NAME]["gates"], ["5.5"])


if __name__ == "__main__":
    unittest.main()
