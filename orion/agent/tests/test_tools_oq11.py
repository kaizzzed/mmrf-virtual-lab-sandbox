"""Open question 11, live: which cohort filters fail in the ProteinPaint bridge, and do the guards say so.

Each filter0 form goes to the bridge twice (getsamplelist, the sample_lists request, and
topMutatedGenes) and to Gen3 (Guppy's count). The guards in tools/guards.py must agree with what the
stack does: a filter the executor's check passes reaches the bridge with the same count Gen3 gives;
a filter the tools' own check refuses fails in the bridge (400), or differs from Gen3, or fails in
Gen3. The table prints with the test output and is recorded in orion/notes/oq11-pp-bridge-filters.md.
Read-only requests through the identity gateway; skips cleanly when the stack is not reachable.
"""

import json
import os
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from test_tools_fakes import LIVE_ORIGIN, goldens, live_stack_reachable, obj

from orion_agent.tools.guards import bridge_safe_with_fields, filter0_bridge_safe
from orion_agent.tools.schemas import DE_PREANALYSIS_REQUEST, EMPTY_TVSLST, case_id_filter
from orion_agent.vocab import Vocabulary

FIELDS = Path(os.environ.get("ORION_FIELDS_PATH", "/config/cohort-fields.json"))
COUNT = "query ($filter: JSON) { CaseCentric__aggregation { case_centric(filter: $filter, accessibility: accessible) { _totalCount } } }"
IDS = [f"SYNTH-CASE-{i:04d}" for i in range(1, 1001)]
UNKNOWN_IDS = [f"SYNTH-CASE-{i:04d}" for i in range(1001, 10001)]  # well-formed ids no case has

# (name, filter0, expected count when Gen3 cannot evaluate the form, or None to use Gen3's count)
FORMS: list[tuple[str, Any, int | None]] = [
    ("all cases, {and: []}", {"and": []}, None),
    ("in, one value", {"and": [{"in": {"demographic.gender": ["female"]}}]}, None),
    ("in, two values", {"and": [{"in": {"demographic.race": ["white", "asian"]}}]}, None),
    ("=", {"and": [{"=": {"demographic.gender": "female"}}]}, None),
    ("!=", {"and": [{"!=": {"demographic.gender": "female"}}]}, None),
    ("unwrapped clause (no and)", {"in": {"demographic.gender": ["female"]}}, None),
    ("range >= and < (age_at_index, days)", {"and": [{">=": {"demographic.age_at_index": 18250}}, {"<": {"demographic.age_at_index": 25550}}]}, None),
    ("range on a nested array (diagnoses.age_at_diagnosis >= 60)", {"and": [{">=": {"diagnoses.age_at_diagnosis": 60}}]}, None),
    ("range on a sparse field (days_to_death < 1000)", {"and": [{"<": {"demographic.days_to_death": 1000}}]}, None),
    ("nested and/or", {"and": [{"or": [{"in": {"demographic.gender": ["female"]}}, {"in": {"demographic.race": ["asian"]}}]},
                               {"in": {"diagnoses.iss_stage": ["Stage I"]}}]}, None),
    ("or at the top", {"or": [{"in": {"demographic.race": ["asian"]}}, {"in": {"diagnoses.iss_stage": ["Stage III"]}}]}, None),
    ("CGS risk category", {"and": [{"in": {"cgs_risk_key_criteria.cgs_risk_category": ["High risk"]}}]}, None),
    ("multiselect (regimen_short_name)", {"and": [{"in": {"administered_regimen_lines.regimen_short_name": ["VRd"]}}]}, None),
    ("numeric enum as numbers (censor_os in [1])", {"and": [{"in": {"outcomes.censor_os": [1]}}]}, None),
    ("numeric enum as strings (censor_os in ['1'])", {"and": [{"in": {"outcomes.censor_os": ["1"]}}]}, None),
    ("text enum of digits (ecog in ['0'])", {"and": [{"in": {"follow_ups.ecog_performance_status": ["0"]}}]}, None),
    ("project.project_id", {"and": [{"in": {"project.project_id": ["SYNTHETIC-MM"]}}]}, None),
    ("samples.sample_type", {"and": [{"in": {"samples.sample_type": ["Primary Tumor"]}}]}, None),
    ("files.experimental_strategy", {"and": [{"in": {"files.experimental_strategy": ["RNA-Seq"]}}]}, None),
    ("ES-only: available_variation_data", {"and": [{"in": {"available_variation_data": ["ssm"]}}]}, None),
    ("ES-only: gene.symbol", {"and": [{"in": {"gene.symbol": ["KRAS"]}}]}, None),
    ("ES-only: summary.experimental_strategies", {"and": [{"in": {"summary.experimental_strategies.experimental_strategy": ["RNA-Seq"]}}]}, None),
    ("unknown field", {"and": [{"in": {"nope.field": ["x"]}}]}, None),
    ("not", {"and": [{"not": {"in": {"demographic.gender": ["female"]}}}]}, None),
    ("exclude", {"and": [{"exclude": {"demographic.gender": ["female"]}}]}, None),
    ("excludes (FilterSet spelling)", {"and": [{"excludes": {"demographic.gender": ["female"]}}]}, None),
    ("case-ID list, 1", case_id_filter(IDS[:1]), 1),
    ("case-ID list, 10", case_id_filter(IDS[:10]), 10),
    ("case-ID list, 100", case_id_filter(IDS[:100]), 100),
    ("case-ID list, 1,000", case_id_filter(IDS), 1000),
    ("case-ID list, 5,000 (1,000 match)", case_id_filter(IDS + UNKNOWN_IDS[:4000]), 1000),
    ("case-ID list, 10,000 (1,000 match)", case_id_filter(IDS + UNKNOWN_IDS), 1000),
    ("case-ID list, empty", {"and": [{"in": {"case_id": []}}]}, 0),
    ("cases.case_id key, 10", {"and": [{"in": {"cases.case_id": IDS[:10]}}]}, 10),
    ("legacy operands {cases.case_id: id}, 10", {"and": [{"in": {"case_id": [{"cases.case_id": i} for i in IDS[:10]]}}]}, 10),
]


def post(path: str, body: Any) -> tuple[int, Any, int]:
    request = urllib.request.Request(LIVE_ORIGIN + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    return status, parsed, round((time.monotonic() - started) * 1000)


def gen3_count(filter0: Any) -> int | str:
    status, body, _ = post("/guppy/graphql", {"query": COUNT, "variables": {"filter": filter0}})
    if status != 200 or not isinstance(body, dict) or body.get("errors"):
        message = body["errors"][0]["message"] if isinstance(body, dict) and body.get("errors") else f"HTTP {status}"
        return f"error: {str(message)[:48]}"
    return int(body["data"]["CaseCentric__aggregation"]["case_centric"]["_totalCount"])


def bridge_count(filter0: Any) -> tuple[int | str, int]:
    status, body, ms = post("/protein-paint/termdb", {"genome": "hg38", "dslabel": "MMRF", "getsamplelist": 1, "filter": EMPTY_TVSLST, "filter0": filter0,
                                                      "mapParent2Children": True})
    return (len(body) if status == 200 and isinstance(body, list) else f"HTTP {status}"), ms


def bridge_ranking(filter0: Any) -> int | str:
    status, body, _ = post("/protein-paint/termdb/topMutatedGenes", {"genome": "hg38", "dslabel": "MMRF", "maxGenes": 50, "geneFilter": "CGC", "filter0": filter0})
    return len(body["genes"]) if status == 200 and isinstance(body, dict) else f"HTTP {status}"


@unittest.skipUnless(live_stack_reachable(), f"the local stack is not reachable at {LIVE_ORIGIN}")
@unittest.skipUnless(FIELDS.is_file(), f"{FIELDS} is not mounted")
class BridgeFilterTests(unittest.TestCase):
    rows: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        vocab = Vocabulary.load(FIELDS)
        cls.rows = []
        for name, filter0, expected in FORMS:
            gen3 = gen3_count(filter0)
            bridge, ms = bridge_count(filter0)
            ranking = bridge_ranking(filter0)
            target = expected if expected is not None else gen3
            cls.rows.append({
                "name": name, "gen3": gen3, "bridge": bridge, "ranking": ranking, "ms": ms,
                "same": isinstance(bridge, int) and isinstance(target, int) and bridge == target,
                "tool": filter0_bridge_safe(filter0) is None, "executor": bridge_safe_with_fields(filter0, vocab) is None,
            })
        print("\n| Filter0 form | Gen3 (Guppy) | Bridge samples | Bridge genes | ms | Same | Tool guard | Executor guard |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in cls.rows:
            print(f"| {r['name']} | {r['gen3']} | {r['bridge']} | {r['ranking']} | {r['ms']} | {'yes' if r['same'] else 'no'} | "
                  f"{'pass' if r['tool'] else 'UNSUPPORTED_FIELD'} | {'pass' if r['executor'] else 'UNSUPPORTED_FIELD'} |")

    def test_every_filter_the_executor_passes_gives_the_bridge_the_same_count(self) -> None:
        for row in self.rows:
            if row["executor"]:
                self.assertTrue(row["same"], row)
                self.assertIsInstance(row["ranking"], int, row)

    def test_every_filter_the_tools_refuse_fails_or_differs(self) -> None:
        for row in self.rows:
            if not row["tool"]:
                self.assertFalse(row["same"] and isinstance(row["gen3"], int), row)

    def test_the_executor_check_is_the_stricter(self) -> None:
        for row in self.rows:
            if row["executor"]:
                self.assertTrue(row["tool"], row)

    def test_a_numeric_field_as_strings_is_the_silent_failure(self) -> None:
        row = next(r for r in self.rows if r["name"].startswith("numeric enum as strings"))
        self.assertEqual((row["bridge"], row["tool"], row["executor"]), (0, True, False))
        self.assertIsInstance(row["gen3"], int)
        self.assertGreater(row["gen3"], 0)

    def test_the_largest_case_id_list_is_quick(self) -> None:
        row = next(r for r in self.rows if r["name"].startswith("case-ID list, 10,000"))
        self.assertTrue(row["same"])
        self.assertLess(row["ms"], 5000)

    def test_de_pre_analysis_ignores_filter0(self) -> None:
        # Why the DE tools carry no cohort filter: the groups are sample lists; filter0 does not narrow them.
        body: dict[str, Any] = obj(goldens().payload(DE_PREANALYSIS_REQUEST))
        for group, name in zip(body["samplelst"]["groups"], ("control", "case")):
            group["name"] = name
        counts = []
        filters: list[Any] = [{"and": []}, None, case_id_filter(IDS[:100])]
        for filter0 in filters:
            body["filter0"] = filter0
            status, answer, _ = post("/protein-paint/termdb/DE", body)
            self.assertEqual(status, 200)
            counts.append(answer["data"])
        print(f"\nDE pre-analysis eligible counts with filter0 all cases, null, 100 case IDs: {counts}")
        self.assertEqual(counts[0], counts[1])
        self.assertEqual(counts[0], counts[2])


if __name__ == "__main__":
    unittest.main()
