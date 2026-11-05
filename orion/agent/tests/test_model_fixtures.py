"""Shared inputs for the model-role and verifier tests, built from the contract's frozen fixtures.

J1 comes from `run-record.j1-complete.json` (its compiled plan, facts and answer). J3 and the
OncoMatrix follow-up come from the last snapshot of `j3-after-confirm` and `oncomatrix`; their
compiled steps follow TOOLS.md 5.3 and 5.2 (the views carry rows, not step args). The tests at the
end check that these inputs match the fixtures they stand for.
"""

import json
import unittest
from functools import cache
from typing import Any

from pydantic import JsonValue

from orion_agent.compiler import CompiledPlan, StepSpec
from orion_agent.interfaces import ComposerFollowUp, ComposerHypothesis, ComposerOutput, ComposerStatement, ObservedResult
from orion_agent.models import FactRecord, Limitation, RunView, Sentence, VizSpec
from support import CONTRACTS

FIXTURES = CONTRACTS / "fixtures"
ZERO_SHA = "0" * 64


@cache
def last_view(name: str) -> RunView:
    snapshots = [
        json.loads(line[len("data: "):])
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.startswith("data: ")
    ]
    last = [frame for frame in snapshots if frame["type"] == "STATE_SNAPSHOT"][-1]
    return RunView.model_validate(last["snapshot"])


@cache
def j1_record() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "run-record.j1-complete.json").read_text(encoding="utf-8"))
    return data


def j1_plan() -> CompiledPlan:
    return CompiledPlan.model_validate(j1_record()["planVersions"][0])


def j1_facts() -> list[FactRecord]:
    return [FactRecord.model_validate(f) for f in j1_record()["facts"]]


def j1_observed() -> ObservedResult:
    answer = j1_record()["answer"]
    return ObservedResult(
        template_version="observed@1",
        observed=[Sentence.model_validate(s) for s in answer["observed"]],
        limitations=[Limitation.model_validate(lim) for lim in answer["limitations"]],
        viz_specs=[VizSpec.model_validate(v) for v in answer["vizSpecs"]],
    )


def view_facts(view: RunView) -> list[FactRecord]:
    return [
        FactRecord.model_validate(
            {
                **fact.model_dump(mode="json"),
                "planVersion": 1,
                "callId": f"{view.run_id}.v1.{fact.step_id}.c1",
                "requestSha256": ZERO_SHA,
                "responseSha256": ZERO_SHA,
                "engine": None,
            }
        )
        for fact in view.facts
    ]


def view_observed(view: RunView) -> ObservedResult:
    return ObservedResult(
        template_version="observed@1", observed=list(view.observed), limitations=list(view.limitations), viz_specs=list(view.viz_specs)
    )


def _step(step_id: str, row: int, tool: str, kind: str, args: dict[str, JsonValue], depends_on: list[str], summary: str = "") -> StepSpec:
    return StepSpec.model_validate(
        {"id": step_id, "row": row, "tool": tool, "kind": kind, "folded": step_id == "s1", "inputSummary": summary, "args": args,
         "dependsOn": depends_on}
    )


def _ref(step: str, pointer: str, intersect: str | None = None) -> dict[str, JsonValue]:
    ref: dict[str, JsonValue] = {"ref": step, "pointer": pointer}
    if intersect:
        ref["intersect"] = intersect
    return ref


THRESHOLDS: dict[str, JsonValue] = {"pValueCutoff": 0.05, "pValueType": "adjusted", "foldChangeCutoff": 0.3}


def j3_plan() -> CompiledPlan:
    """TOOLS.md 5.3: the J3 steps, unfiltered scope."""
    view = last_view("j3-after-confirm.events.jsonl")
    assert view.plan is not None
    wt, mut = "KRAS not detected (ORION)", "KRAS mutated (ORION)"
    steps = [
        _step("s1", 1, "fixture_fingerprint", "server", {}, [], "sandbox status, versions, ranking"),
        _step("s2", 1, "gene_cases", "server", {"gene": "KRAS", "cohortFilter": {"and": []}}, [], "KRAS-mutated cases"),
        _step("s3", 2, "cases_query", "server",
              {"filter": {"and": [{"in": {"available_variation_data": ["ssm"]}}]}, "pageSize": 1000, "maxIds": 10000}, [],
              "Mutation-tested cases (available variation data = ssm)"),
        _step("s4", 3, "check_subset", "exec", {"inner": "s2", "outer": "s3"}, ["s2", "s3"]),
        _step("s5", 3, "set_operation", "exec", {"op": "difference", "a": "s3", "b": "s2"}, ["s2", "s3", "s4"]),
        _step("s6", 4, "sample_lists", "server", {"groups": [
            {"key": "control", "caseIds": _ref("s5", "/observed/caseIds")},
            {"key": "case", "caseIds": _ref("s2", "/observed/caseIds", "s3")}]}, ["s5", "s2", "s3"]),
        _step("s7", 4, "de_preanalysis", "server", {"groups": [
            {"key": "control", "sampleIds": _ref("s6", "/observed/groups/0/sampleIds")},
            {"key": "case", "sampleIds": _ref("s6", "/observed/groups/1/sampleIds")}]}, ["s6"]),
        _step("s8", 5, "save_cohorts", "client", {"cohorts": [
            {"ref": "kras_wt", "name": wt, "eligible": _ref("s7", "/observed/groups/0/eligible")},
            {"ref": "kras_mut", "name": mut, "eligible": _ref("s7", "/observed/groups/1/eligible")}]}, ["s2", "s5", "s7"]),
        _step("s9", 6, "de_run", "server", {"groups": [
            {"name": wt, "cohort": "kras_wt", "sampleIds": _ref("s6", "/observed/groups/0/sampleIds")},
            {"name": mut, "cohort": "kras_mut", "sampleIds": _ref("s6", "/observed/groups/1/sampleIds")}],
            "method": "wilcoxon", "thresholds": THRESHOLDS}, ["s6", "s8"], f"Wilcoxon, {wt} vs. {mut}"),
        _step("s10", 6, "rank_top_n", "exec", {"rows": "s9", "key": "gene_name", "rankBy": "adjusted_p_value", "n": 10,
                                               "ascending": True, "tieBreak": "gene_name", "thresholds": THRESHOLDS}, ["s9"]),
    ]
    plan = view.plan
    return CompiledPlan.model_validate(
        {
            "version": plan.version, "signature": plan.signature,
            "scope": {"source": plan.scope.source, "name": plan.scope.name, "caseCount": plan.scope.case_count, "clauses": []},
            "rows": [r.model_dump(mode="json") for r in plan.rows], "steps": [s.model_dump(mode="json") for s in steps],
            "assumptions": [a.model_dump(mode="json") for a in plan.assumptions], "limitations": [], "writes": True,
            "paraphrase": plan.paraphrase, "editable": [e.model_dump(mode="json") for e in plan.editable],
        }
    )


def genes_plan() -> CompiledPlan:
    """TOOLS.md 5.2 as the `oncomatrix` fixture lays it out (s1 to s4)."""
    view = last_view("oncomatrix.events.jsonl")
    assert view.plan is not None
    clause: JsonValue = {"in": {"cgs_risk_key_criteria.cgs_risk_category": ["High risk"]}}
    steps = [
        _step("s1", 1, "fixture_fingerprint", "server", {}, []),
        _step("s2", 1, "cases_query", "server", {"filter": {"and": [clause]}, "pageSize": 1000, "maxIds": 10000}, [],
              "CGS risk category = High risk"),
        _step("s3", 2, "top_mutated_genes", "server",
              {"filter0": {"and": [{"in": {"case_id": _ref("s2", "/observed/caseIds")}}]}, "geneCount": 3}, ["s2"]),
        _step("s4", 2, "rank_top_n", "exec", {"rows": "s3", "key": "gene", "rankBy": "cases", "n": 3, "ascending": False,
                                              "tieBreak": "gene"}, ["s3"]),
    ]
    plan = view.plan
    return CompiledPlan.model_validate(
        {
            "version": 1, "signature": plan.signature,
            "scope": {"source": plan.scope.source, "name": plan.scope.name, "caseCount": plan.scope.case_count, "clauses": []},
            "rows": [r.model_dump(mode="json") for r in plan.rows], "steps": [s.model_dump(mode="json") for s in steps],
            "assumptions": [a.model_dump(mode="json") for a in plan.assumptions], "limitations": [], "writes": False,
            "paraphrase": plan.paraphrase,
        }
    )


def placeholder_text(sentence: Sentence) -> str:
    return "".join("{{" + s.fact_id + "}}" if s.fact_id else s.text for s in sentence.segments)


def fixture_model_output(view: RunView) -> ComposerOutput:
    """The fixture's model text turned back into the composer output that would have produced it."""
    return ComposerOutput(
        interpretation=[ComposerStatement(text=placeholder_text(s), cites=list(s.cites or [])) for s in view.interpretation or []],
        hypotheses=[
            ComposerHypothesis(text=placeholder_text(s), cites=list(s.cites or []), form="question") for s in view.hypotheses or []
        ],
        follow_ups=[ComposerFollowUp(kind=f.kind, label=f.label, question=f.question) for f in view.follow_ups],
        open_app_ids=[link.app_id for link in view.open_links],
    )


class FixtureInputsTest(unittest.TestCase):
    def test_j1_inputs_match_the_record(self) -> None:
        plan = j1_plan()
        self.assertEqual([s.tool for s in plan.steps], ["fixture_fingerprint", "cohort_count", "cohort_count", "survival_compare", "derive_km_median"])
        self.assertIn("f.s4.p", {f.id for f in j1_facts()})
        self.assertEqual(len(j1_observed().observed), 4)

    def test_j3_steps_follow_the_catalog(self) -> None:
        plan = j3_plan()
        view = last_view("j3-after-confirm.events.jsonl")
        self.assertEqual([s.tool for s in plan.steps], [s.tool for s in view.steps])
        self.assertEqual({f.step_id for f in view_facts(view)} - {s.id for s in plan.steps}, set())

    def test_oncomatrix_steps_follow_the_fixture(self) -> None:
        view = last_view("oncomatrix.events.jsonl")
        self.assertEqual([s.tool for s in genes_plan().steps], [s.tool for s in view.steps])


if __name__ == "__main__":
    unittest.main()
