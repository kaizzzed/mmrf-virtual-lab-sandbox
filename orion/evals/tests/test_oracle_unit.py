"""The oracle's arithmetic over a small synthetic stack served by a fake gateway.

The survival data is the Freireich 6-MP trial (6-MP as "High risk", placebo as "Standard risk"),
so the oracle's J1 must give the textbook log-rank chi-square of 16.79.
"""

import json
import secrets
import tempfile
import unittest
from pathlib import Path
from typing import Any

from test_stats import PLACEBO_EVENTS, SIX_MP_CENSORED, SIX_MP_EVENTS

from orion_evals.oracle import Gateway, Oracle, OracleError, PANEL_GENE_IDS, case_id_filter, gen3_filter, group_keys, rank_with_ties

GENE_BY_ID = {v: k for k, v in PANEL_GENE_IDS.items()}


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    rows = [("High risk", t, True) for t in SIX_MP_EVENTS] + [("High risk", t, False) for t in SIX_MP_CENSORED] + [("Standard risk", t, True) for t in PLACEBO_EVENTS]
    for i, (risk, time, dead) in enumerate(rows, start=1):
        cases.append(
            {
                "case_id": f"SYNTH-CASE-{i:04d}",
                "demographic": {"vital_status": "Dead" if dead else "Alive", "days_to_death": time if dead else None, "race": "black or african american" if i % 3 == 0 else "white"},
                "diagnoses": [{"days_to_last_follow_up": time}],
                "cgs_risk_key_criteria": {"cgs_risk_category": risk},
                "available_variation_data": ["ssm"] if i != 7 else [],
            }
        )
    # One standard-risk case with no follow-up at all: counted in n, not in the survival data.
    cases.append({"case_id": "SYNTH-CASE-0099", "demographic": {"vital_status": "Alive", "days_to_death": None, "race": "white"}, "diagnoses": [], "cgs_risk_key_criteria": {"cgs_risk_category": "Standard risk"}, "available_variation_data": ["ssm"]})
    return cases


CASES = build_cases()
# Mutations: KRAS in every third case (case 6, 9, ... and case 7, which has no mutation data),
# NRAS and DIS3 in the same cases so they tie, TP53 in one.
MUTATED = {
    "KRAS": {f"SYNTH-CASE-{i:04d}" for i in range(3, 43, 3)} | {"SYNTH-CASE-0007"},
    "NRAS": {f"SYNTH-CASE-{i:04d}" for i in range(3, 25, 6)},
    "DIS3": {f"SYNTH-CASE-{i:04d}" for i in range(3, 25, 6)},
    "TP53": {"SYNTH-CASE-0003"},
    "BRAF": set(),
    "MYC": set(),
}


def value_of(case: dict[str, Any], field: str) -> list[Any]:
    current: Any = case
    for part in field.split("."):
        if isinstance(current, list):
            current = [c.get(part) for c in current]
        else:
            current = current.get(part) if isinstance(current, dict) else None
    return current if isinstance(current, list) else [current]


def matches(case: dict[str, Any], flt: dict[str, Any]) -> bool:
    for item in flt.get("and", []):
        (op, body), = item.items()
        (field, values), = body.items()
        if op != "in" or not set(value_of(case, field)) & set(values):
            return False
    return True


class FakeGateway(Gateway):
    def __init__(self) -> None:
        super().__init__("http://identity:8080")
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if path == "/sandbox/status":
            return {"status": "OK", "synthetic": True, "cases": len(CASES), "csrf": "x"}
        if path == "/analysis/v0/cases/":
            hits = [c for c in CASES if matches(c, body["filter"])]
            page = hits[body["offset"] : body["offset"] + body["size"]]
            return {"data": [{"case_id": c["case_id"], "demographic": c["demographic"], "diagnoses": c["diagnoses"]} for c in page], "pagination": {"total": len(hits)}}
        if path == "/analysis/v0/genomic/gene_cases":
            gene = GENE_BY_ID[body["gene_filter"]["and"][0]["in"]["gene_id"][0]]
            return sorted(MUTATED[gene])
        if path == "/analysis/v0/survival/":
            return {"results": [], "overallStats": {"pValue": 4.2e-05}}
        if path == "/protein-paint/termdb/topMutatedGenes":
            ids = set(body["filter0"]["and"][0]["in"]["case_id"]) if body["filter0"]["and"] else {c["case_id"] for c in CASES}
            ranking = sorted(((g, len(m & ids)) for g, m in MUTATED.items() if m & ids), key=lambda r: (-r[1], r[0]))
            return {"genes": [{"gene": g, "mutationStat": [{"dt": 1, "count": n}]} for g, n in ranking]}
        if path == "/protein-paint/termdb":
            if body["filter"]["lst"]:
                value = body["filter"]["lst"][0]["tvs"]["values"][0]["key"]
                tested = {c["case_id"] for c in CASES if c["available_variation_data"]}
                ids = (MUTATED["KRAS"] & tested) if value == "Mutated" else (tested - MUTATED["KRAS"])
            else:
                ids = set(body["filter0"]["and"][0]["in"]["case_id"])
            return [{"id": int(i.rsplit("-", 1)[1]), "name": i.replace("CASE", "SAMPLE")} for i in sorted(ids)]
        if path == "/protein-paint/termdb/DE" and body.get("preAnalysis"):
            groups = body["samplelst"]["groups"]
            # Sample 3 has no RNA-seq: one control-or-case sample is ineligible.
            return {"data": {g["name"]: len([v for v in g["values"] if v["sampleId"] != 3]) for g in groups}}
        if path == "/protein-paint/termdb/DE":
            dots = [
                {"gene_name": "GENE_A", "fold_change": 1.5, "original_p_value": 1e-12, "adjusted_p_value": 1e-10},
                {"gene_name": "GENE_B", "fold_change": -2.0, "original_p_value": 1e-11, "adjusted_p_value": 2e-10},
                {"gene_name": "GENE_C", "fold_change": 0.9, "original_p_value": 1e-11, "adjusted_p_value": 2e-10},
                {"gene_name": "GENE_D", "fold_change": 0.2, "original_p_value": 1e-13, "adjusted_p_value": 1e-12},
                {"gene_name": "GENE_E", "fold_change": 3.0, "original_p_value": 0.01, "adjusted_p_value": 0.2},
            ]
            return {"method": body["method"], "sample_size1": 1, "sample_size2": 1, "data": {"dots": dots, "totalRows": 100, "totalSignificantRows": 5, "totalSignificantUp": 3, "totalSignificantDown": 2}}
        raise OracleError(f"unexpected {method} {path}")


class Helpers(unittest.TestCase):
    def test_filters(self) -> None:
        self.assertEqual(gen3_filter([{"field": "a", "op": "in", "values": ["x"]}, {"field": "b", "op": ">=", "values": [3]}]), {"and": [{"in": {"a": ["x"]}}, {">=": {"b": 3}}]})
        self.assertEqual(case_id_filter(["B", "A"]), {"and": [{"in": {"case_id": ["A", "B"]}}]})

    def test_group_keys(self) -> None:
        self.assertEqual(group_keys(["High risk", "Standard risk"]), ["high", "standard"])
        self.assertEqual(group_keys(["Stage I", "Stage II", "Stage III"]), ["i", "ii", "iii"])
        self.assertEqual(group_keys(["Same", "Same"]), ["g1", "g2"])

    def test_rank_with_ties(self) -> None:
        rows = [("KRAS", 5.0), ("NRAS", 3.0), ("DIS3", 3.0), ("MYC", 1.0)]
        self.assertEqual(rank_with_ties(rows, 2, descending=True), ([("KRAS", 5.0), ("DIS3", 3.0), ("NRAS", 3.0)], ["NRAS"]))
        self.assertEqual(rank_with_ties(rows, 10, descending=True)[1], [])
        self.assertEqual(rank_with_ties([("b", 0.1), ("a", 0.1)], 1, descending=False), ([("a", 0.1), ("b", 0.1)], ["b"]))


class Journeys(unittest.TestCase):
    def setUp(self) -> None:
        self.gw = FakeGateway()
        self.oracle = Oracle(self.gw)

    def test_j1_is_the_textbook_log_rank(self) -> None:
        exp = self.oracle.j1()
        high, standard = exp.groups
        self.assertEqual((high.cases, high.with_survival, high.events), (21, 21, 9))
        self.assertEqual((standard.cases, standard.with_survival, standard.events), (22, 21, 21))
        self.assertAlmostEqual(exp.statistic, 16.79, places=2)
        self.assertAlmostEqual(exp.p_value, 4.17e-05, delta=1e-07)
        self.assertEqual((high.median_days, standard.median_days), (23.0, 8.0))
        self.assertEqual(exp.engine_p_value, 4.2e-05)
        self.assertEqual([g.key for g in exp.groups], ["high", "standard"])

    def test_j2_intersects_in_code_and_finds_the_tie(self) -> None:
        exp = self.oracle.j2([{"field": "demographic.race", "op": "in", "values": ["black or african american"]}], n=2)
        self.assertEqual(exp.cohort_size, 14)
        self.assertEqual(exp.denominator, 14)
        self.assertEqual(exp.counts["KRAS"], 14)
        self.assertEqual(exp.shown, [("KRAS", 14), ("DIS3", 4), ("NRAS", 4)])
        self.assertEqual(exp.ties, ["NRAS"])
        self.assertEqual(exp.engine_ranking, [("KRAS", 14), ("DIS3", 4), ("NRAS", 4), ("TP53", 1)])

    def test_j3_subtracts_and_counts_the_untested_mutated_case(self) -> None:
        exp = self.oracle.j3(top_n=2)
        self.assertEqual(exp.scope_cases, 43)
        self.assertEqual(exp.tested, 42)
        self.assertEqual(exp.mutated_in_scope, 15)
        self.assertEqual(exp.outside_tested, 1)
        self.assertEqual(exp.mutated_tested, 14)
        self.assertEqual(exp.not_detected, 28)
        self.assertEqual(exp.samples, {"control": 28, "case": 14})
        self.assertEqual(exp.eligible, {"control": 28, "case": 13})
        self.assertTrue(exp.eligible_ok)
        self.assertEqual(exp.term_counts, {"control": 28, "case": 14})
        assert exp.de is not None
        # GENE_D fails |log2FC| > 0.3 and GENE_E fails adjusted p < 0.05; B and C tie at the cutoff.
        self.assertEqual([r["gene"] for r in exp.de["top"]], ["GENE_A", "GENE_B", "GENE_C"])
        self.assertEqual(exp.de["ties"], ["GENE_C"])
        self.assertEqual(exp.de["passing"], 3)
        de_body = next(b for m, p, b in self.gw.calls if p == "/protein-paint/termdb/DE" and not b.get("preAnalysis"))
        self.assertEqual(de_body["volcanoRender"]["significanceThresholds"], {"pValueCutoff": 1.3, "pValueType": "adjusted", "foldChangeCutoff": 0.3})
        self.assertEqual([g["name"] for g in de_body["samplelst"]["groups"]], ["oracle control", "oracle case"])

    def test_j3_skips_de_when_a_group_is_ineligible(self) -> None:
        exp = self.oracle.j3("TP53", top_n=10)
        self.assertEqual(exp.samples["case"], 1)
        self.assertFalse(exp.eligible_ok)
        self.assertIsNone(exp.de)

    def test_a_source_the_stack_does_not_serve_fails_cleanly(self) -> None:
        with self.assertRaises(OracleError):
            self.oracle.fingerprint()


class Credentials(unittest.TestCase):
    def test_reads_the_access_file_at_run_time_and_never_logs_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "access.json"
            secret = secrets.token_urlsafe(24)  # generated per run; no credential lives in the test
            path.write_text(json.dumps({"origin": "https://d111111abcdef8.cloudfront.net", "username": "sandbox", "password": secret}))
            gateway = Gateway("https://d111111abcdef8.cloudfront.net", path)
            self.assertTrue(gateway.authenticated)
            self.assertNotIn(secret, repr(vars(gateway).get("log")))
            with self.assertRaises(OracleError):
                Gateway("https://d222222abcdef8.cloudfront.net", path)
        self.assertFalse(Gateway().authenticated)
        with self.assertRaises(ValueError):
            Gateway("http://user:pw@identity:8080")


if __name__ == "__main__":
    unittest.main()
