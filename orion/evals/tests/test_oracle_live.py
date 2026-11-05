"""The oracle against the live local stack (skipped when the identity gateway is not reachable).

It asserts what must hold whatever the data: the oracle's own log-rank equals the engine's p, the
J2 ranking from Gen3 `gene_cases` equals ProteinPaint's, the KRAS dictionary term corroborates the
J3 group sizes, and the sets add up. It records what it computed under reports/ (gitignored).
"""

import json
import math
import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orion_evals.cli import REPORTS, load_prompts
from orion_evals.oracle import DEFAULT_ORIGIN, Gateway, J1Expected, J2Expected, J3Expected, Oracle, to_json

ORIGIN = os.environ.get("ORION_VLAB_ORIGIN", DEFAULT_ORIGIN)
REACHABLE = Gateway(ORIGIN, timeout_s=5.0).reachable()
GATEWAY = Gateway(ORIGIN)


@unittest.skipUnless(REACHABLE, f"the local stack is not reachable at {ORIGIN}")
class LiveOracle(unittest.TestCase):
    computed: dict[str, object]
    oracle: Oracle
    fingerprint: dict[str, Any]
    j1: J1Expected
    j1_race: J1Expected
    race_count: int
    j2: J2Expected
    j3: J3Expected
    j3_top5: J3Expected
    e9: J3Expected

    @classmethod
    def setUpClass(cls) -> None:
        prompts = load_prompts()
        inputs = prompts["oracleInputs"]
        race = prompts["cohorts"]["race"]["definition"]["clauses"]
        oracle = Oracle(GATEWAY)
        cls.oracle = oracle
        cls.fingerprint = oracle.fingerprint()
        cls.j1 = oracle.j1(inputs["J1"]["values"], inputs["J1"]["field"])
        cls.j1_race = oracle.j1(inputs["J1"]["values"], inputs["J1"]["field"], race)
        cls.race_count = oracle.count(race)
        cls.j2 = oracle.j2(inputs["J2"]["clauses"], int(inputs["J2"]["n"]))
        cls.j3 = oracle.j3(inputs["J3"]["gene"], [], int(inputs["J3"]["topN"]))
        cls.j3_top5 = oracle.j3(inputs["J3"]["gene"], [], int(inputs["E15"]["topN"]))
        cls.e9 = oracle.j3(inputs["E9"]["gene"], inputs["E9"]["scope"], run_de=False)
        cls.computed = {
            "schema": "orion.oracle-live-test/v1",
            "computedAt": datetime.now(UTC).isoformat(timespec="seconds"),
            "origin": GATEWAY.origin,
            "fingerprint": cls.fingerprint,
            "j1": to_json(cls.j1),
            "j1Race": to_json(cls.j1_race),
            "raceCount": cls.race_count,
            "j2": to_json(cls.j2),
            "j3": to_json(cls.j3),
            "j3Top5": None if cls.j3_top5.de is None else [r["gene"] for r in cls.j3_top5.de["top"]],
            "e9": to_json(cls.e9),
            "requests": len(GATEWAY.log),
        }
        directory = Path(os.environ.get("ORION_EVAL_REPORTS_DIR", str(REPORTS)))
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            (directory / f"oracle-live-test-{stamp}.json").write_text(json.dumps(cls.computed, indent=1, default=str) + "\n", encoding="utf-8")
        except OSError:
            pass  # a read-only mount still runs the assertions

    def test_fingerprint(self) -> None:
        document = self.fingerprint["document"]
        self.assertEqual(document["schema"], "orion.fixture-fingerprint/v1")
        self.assertTrue(document["sandbox"]["synthetic"])
        self.assertRegex(self.fingerprint["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(document["sandbox"]["cases"], self.j1.scope_cases)

    def test_j1_log_rank_equals_the_engine(self) -> None:
        self.assertEqual(sum(g.cases for g in self.j1.groups), self.j1.scope_cases)
        for group in self.j1.groups:
            self.assertGreater(group.cases, 0)
            self.assertLessEqual(group.events, group.with_survival)
            self.assertLessEqual(group.with_survival, group.cases)
        assert self.j1.engine_p_value is not None
        self.assertTrue(math.isclose(self.j1.p_value, self.j1.engine_p_value, rel_tol=1e-6), (self.j1.p_value, self.j1.engine_p_value))

    def test_j1_in_the_race_cohort(self) -> None:
        self.assertEqual(self.j1_race.scope_cases, self.race_count)
        self.assertEqual(sum(g.cases for g in self.j1_race.groups), self.race_count)
        assert self.j1_race.engine_p_value is not None
        self.assertTrue(math.isclose(self.j1_race.p_value, self.j1_race.engine_p_value, rel_tol=1e-6))

    def test_j2_ranking_equals_proteinpaint(self) -> None:
        self.assertEqual(self.j2.cohort_size, self.race_count)
        self.assertEqual(self.j2.denominator, self.j2.cohort_size)
        self.assertEqual(self.j2.ranking, self.j2.engine_ranking)
        counts = [c for _, c in self.j2.ranking]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertTrue(all(c <= self.j2.cohort_size for c in counts))
        self.assertGreaterEqual(len(self.j2.shown), self.j2.n)

    def test_j3_sets_add_up_and_the_dictionary_term_agrees(self) -> None:
        j3 = self.j3
        self.assertEqual(j3.tested, j3.mutated_tested + j3.not_detected)
        self.assertEqual(j3.outside_tested, j3.mutated_in_scope - j3.mutated_tested)
        self.assertLessEqual(j3.samples["control"], j3.not_detected)
        self.assertLessEqual(j3.samples["case"], j3.mutated_tested)
        self.assertLessEqual(j3.eligible["control"], j3.samples["control"])
        self.assertLessEqual(j3.eligible["case"], j3.samples["case"])
        self.assertEqual(j3.term_counts, j3.samples)
        self.assertTrue(j3.eligible_ok)

    def test_j3_top_genes_pass_the_thresholds_in_order(self) -> None:
        de = self.j3.de
        assert de is not None
        self.assertEqual(de["method"], "wilcoxon")
        self.assertGreaterEqual(len(de["top"]), min(10, de["passing"]))
        padj = [r["padj"] for r in de["top"]]
        self.assertEqual(padj, sorted(padj))
        self.assertTrue(all(r["padj"] < 0.05 and abs(r["log2fc"]) > 0.3 for r in de["top"]))
        assert self.j3_top5.de is not None
        self.assertEqual([r["gene"] for r in self.j3_top5.de["top"]][:5], [r["gene"] for r in de["top"]][:5])

    def test_e9_has_a_group_under_ten(self) -> None:
        self.assertFalse(self.e9.eligible_ok, f"E9's question no longer has a small group: {self.e9.eligible}")

    def test_the_oracle_logs_no_credential(self) -> None:
        self.assertFalse(GATEWAY.authenticated)
        self.assertTrue(all(set(vars(entry)) == {"method", "path", "status", "elapsed_ms", "bytes", "request_sha256"} for entry in GATEWAY.log))


if __name__ == "__main__":
    unittest.main()
