"""The tools against the local stack (read-only, through the identity gateway): the J3 handlers in the
revised order (gene_cases, cases_query, the set functions, sample_lists on case-ID lists,
de_preanalysis, de_run), the J1 and J2 inputs, and the parity of each viz-feeding call. Every count
observed is printed with the test output. Skips cleanly when the stack is not reachable.
"""

import asyncio
import json
import sys
import unittest
from collections.abc import Coroutine
from typing import Any, TypeVar

from pydantic import JsonValue
from test_tools_fakes import LIVE_ORIGIN, RUN_ID, goldens, live_stack_reachable, obj

from orion_agent.models import derive_request_pinned, pinned_hash
from orion_agent.settings import Settings
from orion_agent.tools.base import STEP_TIMEOUT_S, ServerToolName, ToolCallOutcome
from orion_agent.tools.client import make_tool_client
from orion_agent.tools.config import ToolConfig
from orion_agent.tools.functions import run_function
from orion_agent.tools.records import call_records
from orion_agent.tools.schemas import FixtureFingerprintObserved, resolve_pointer, resolve_step_refs

T = TypeVar("T")
CGS = "cgs_risk_key_criteria.cgs_risk_category"


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def report(label: str, value: object) -> None:
    print(f"  live {label}: {value}", file=sys.stderr)


class Steps:
    """A miniature executor: runs steps in order and resolves StepRefs from earlier outcomes."""

    def __init__(self) -> None:
        self.client = make_tool_client(Settings(vlab_origin=LIVE_ORIGIN), config=ToolConfig(), goldens=goldens())
        self.observed: dict[str, JsonValue] = {}
        self.outcomes: dict[str, ToolCallOutcome] = {}

    def read(self, step_id: str, pointer: str) -> JsonValue:
        return resolve_pointer({"observed": self.observed[step_id]}, pointer)

    def server(self, step_id: str, name: ServerToolName, args: Any) -> ToolCallOutcome:
        request = resolve_step_refs(args, self.read)
        outcome = run(self.client.call(name, request, timeout_s=STEP_TIMEOUT_S, run_id=RUN_ID, plan_version=1, step_id=step_id, first_call=1))
        self.outcomes[step_id] = outcome
        self.observed[step_id] = outcome.result.observed
        call_records(outcome, plan_version=1, step_id=step_id)  # every attempt records cleanly
        return outcome

    def function(self, step_id: str, name: Any, args: Any) -> dict[str, Any]:
        result = run_function(name, args, self.read)
        if result.status == "error":
            raise AssertionError(result.error)
        self.observed[step_id] = result.observed
        return obj(result.observed)


@unittest.skipUnless(live_stack_reachable(), f"the local stack is not reachable at {LIVE_ORIGIN}")
class LiveJourneyTests(unittest.TestCase):
    def test_j3_in_the_revised_order(self) -> None:
        steps = Steps()
        s1 = steps.server("s1", "fixture_fingerprint", {})
        fingerprint = FixtureFingerprintObserved.model_validate(s1.result.observed)
        report("s1 fingerprint", f"{fingerprint.fingerprint_sha256[:12]}… cases={fingerprint.document.sandbox.cases} "
               f"ranking={[(g.gene, g.cases) for g in fingerprint.document.ranking]}")
        self.assertEqual((s1.result.status, len(s1.result.provenance)), ("ok", 4))

        s2 = steps.server("s2", "gene_cases", {"gene": "KRAS", "cohortFilter": {"and": []}})
        s3 = steps.server("s3", "cases_query", {"filter": {"and": [{"in": {"available_variation_data": ["ssm"]}}]}, "pageSize": 1000, "maxIds": 10000})
        self.assertEqual((s2.result.status, s3.result.status), ("ok", "ok"))
        s4 = steps.function("s4", "check_subset", {"inner": "s2", "outer": "s3"})
        s5 = steps.function("s5", "set_operation", {"op": "difference", "a": "s3", "b": "s2"})
        report("s2 KRAS-mutated", obj(s2.result.observed)["count"])
        report("s3 mutation-tested", f"{obj(s3.result.observed)['count']} in {obj(s3.result.observed)['pages']} page(s)")
        report("s4 outside the tested cases", s4["outsideCount"])
        report("s5 KRAS not detected", s5["count"])
        self.assertTrue(s4["isSubset"])
        self.assertEqual(s5["count"], obj(s3.result.observed)["count"] - obj(s2.result.observed)["count"])

        s6 = steps.server("s6", "sample_lists", {"groups": [
            {"key": "control", "caseIds": {"ref": "s5", "pointer": "/observed/caseIds"}},
            {"key": "case", "caseIds": {"ref": "s2", "pointer": "/observed/caseIds", "intersect": "s3"}},
        ]})
        samples = [g["count"] for g in obj(s6.result.observed)["groups"]]
        report("s6 samples control, case", samples)
        self.assertEqual(samples, [s5["count"], obj(s2.result.observed)["count"]])

        s7 = steps.server("s7", "de_preanalysis", {"groups": [
            {"key": "control", "sampleIds": {"ref": "s6", "pointer": "/observed/groups/0/sampleIds"}},
            {"key": "case", "sampleIds": {"ref": "s6", "pointer": "/observed/groups/1/sampleIds"}},
        ]})
        report("s7 RNA-seq eligible control, case", [g["eligible"] for g in obj(s7.result.observed)["groups"]])
        self.assertEqual((s7.result.status, s7.result.limitation), ("ok", None))

        thresholds: dict[str, Any] = {"pValueCutoff": 0.05, "pValueType": "adjusted", "foldChangeCutoff": 0.3}
        s9 = steps.server("s9", "de_run", {"groups": [
            {"name": "KRAS not detected (ORION)", "sampleIds": {"ref": "s6", "pointer": "/observed/groups/0/sampleIds"}},
            {"name": "KRAS mutated (ORION)", "sampleIds": {"ref": "s6", "pointer": "/observed/groups/1/sampleIds"}},
        ], "method": "wilcoxon", "thresholds": thresholds})
        report("s9 DE", f"{obj(s9.result.observed)['totals']} in {s9.elapsed_ms} ms, cacheId {obj(s9.result.observed)['cacheId']}")
        self.assertEqual(s9.result.status, "ok")
        s10 = steps.function("s10", "rank_top_n", {"rows": "s9", "key": "gene_name", "rankBy": "adjusted_p_value", "n": 10, "ascending": True,
                                                    "tieBreak": "gene_name", "thresholds": thresholds})
        report("s10 top genes", [(r["key"], f"{r['row']['fold_change']:.2f}", f"{r['value']:.1e}") for r in s10["rows"]])
        self.assertEqual(s10["count"], min(10, obj(s9.result.observed)["totals"]["significant"]))
        pinned = derive_request_pinned("deDirect", obj(s9.request))
        report("s9 requestPinnedHash", pinned_hash(pinned)[:12] + "…")
        self.assertEqual([g.name for g in pinned.groups], ["KRAS not detected (ORION)", "KRAS mutated (ORION)"])  # type: ignore[union-attr]

    def test_j1_survival_by_cgs_risk(self) -> None:
        steps = Steps()
        groups: list[dict[str, Any]] = []
        for step_id, value in (("s2", "High risk"), ("s3", "Standard risk")):
            filter_: dict[str, Any] = {"and": [{"in": {CGS: [value]}}]}
            outcome = steps.server(step_id, "cohort_count", {"filter": filter_})
            report(f"{step_id} {value}", obj(outcome.result.observed)["count"])
            groups.append({"label": value, "filter": filter_})
        s4 = steps.server("s4", "survival_compare", {"scope": None, "term": "overall_survival", "split": {"field": CGS, "overlay": "cgs_risk_category"},
                                                     "groups": groups})
        report("s4 survival", [(g["label"], g["cases"], g["events"]) for g in obj(s4.result.observed)["groups"]] + [("p", obj(s4.result.observed)["pValue"])])
        self.assertEqual((s4.result.status, len(s4.result.provenance)), ("ok", 3))
        for group in obj(s4.result.observed)["groups"]:
            values = [p["survival"] for p in group["curve"]]
            self.assertEqual(values, sorted(values, reverse=True))
        s5 = steps.function("s5", "derive_km_median", {"curves": "s4"})
        report("s5 medians (days)", [(g["label"], g["median"]) for g in s5["groups"]])
        self.assertEqual(derive_request_pinned("survival", obj(s4.request)).model_dump(mode="json"),
                         {"kind": "survival", "filter0": None, "term": "overall_survival", "overlay": "cgs_risk_category"})

    def test_j2_top_mutated_genes_in_a_planned_cohort(self) -> None:
        steps = Steps()
        s2 = steps.server("s2", "cases_query", {"filter": {"and": [{"in": {"demographic.race": ["black or african american"]}}]}, "pageSize": 1000, "maxIds": 10000})
        report("s2 black or african american", obj(s2.result.observed)["count"])
        s3 = steps.server("s3", "top_mutated_genes", {"filter0": {"and": [{"in": {"case_id": {"ref": "s2", "pointer": "/observed/caseIds"}}}]}, "geneCount": 3})
        report("s3 top genes (+ next)", [(g["gene"], g["cases"]) for g in obj(s3.result.observed)["genes"]] + [("next", obj(s3.result.observed).get("nextGene"))])
        s4 = steps.function("s4", "rank_top_n", {"rows": "s3", "key": "gene", "rankBy": "cases", "n": 3, "ascending": False, "tieBreak": "gene"})
        report("s4 ranking", [(r["rank"], r["key"], r["value"]) for r in s4["rows"]] + [("ties", s4["tiesAtCutoff"])])
        pinned = derive_request_pinned("oncomatrix", obj(s3.request), s3.result.observed)
        self.assertEqual(pinned.genes, [g["gene"] for g in obj(s3.result.observed)["genes"]])  # type: ignore[union-attr]
        count = steps.server("s9", "cohort_count", {"filter": {"and": [{"in": {"demographic.race": ["black or african american"]}}]}})
        self.assertEqual(count.result.observed, {"count": obj(s2.result.observed)["count"]})

    def test_the_same_filter_counts_alike_in_guppy_and_gen3_analysis(self) -> None:
        steps = Steps()
        clause_sets: list[list[Any]] = [[{"in": {"diagnoses.iss_stage": ["Stage II"]}}], [{">=": {"diagnoses.age_at_diagnosis": 60}}], []]
        for clauses in clause_sets:
            filter_ = {"and": clauses}
            counted = steps.server("s2", "cohort_count", {"filter": filter_})
            listed = steps.server("s3", "cases_query", {"filter": filter_})
            report(f"count vs list {json.dumps(clauses)}", f"{counted.result.observed} / {obj(listed.result.observed)['count']}")
            self.assertEqual(counted.result.observed, {"count": obj(listed.result.observed)["count"]})


if __name__ == "__main__":
    unittest.main()
