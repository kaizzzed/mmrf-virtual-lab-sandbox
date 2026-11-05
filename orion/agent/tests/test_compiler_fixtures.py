"""The compiler against the frozen fixtures: each reference plan, pause and pending the fixtures show
is what the compiler produces for that question (signatures aside: the fixtures' are illustrative)."""

import json
import unittest
from typing import Any

from pydantic import JsonValue
from support import CONTRACTS, context, vocabulary

from orion_agent import ir
from orion_agent.compiler import (
    CompiledPlan,
    Compiler,
    CompileStop,
    build_confirm_pending,
    step_input_hashes,
)
from orion_agent.models import (
    ClarifyPending,
    ConfirmPending,
    ConsentPending,
    EditCohortNameAction,
    PlanView,
    RunView,
    StateSnapshotFrame,
    def_sha256,
    iter_sse_data,
    parse_frame,
)
from orion_agent.stubs import J3_OPEN, plan_question
from orion_agent.tools.schemas import resolve_pointer

FIXTURES = CONTRACTS / "fixtures"
J3_IR = {
    "kind": "plan",
    "cohorts": [
        {"ref": "tested", "def": {"kind": "mutation_tested", "within": "base"}},
        {"ref": "kras_mut", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "tested"}},
        {"ref": "kras_wt", "def": {"kind": "difference", "a": "tested", "b": "kras_mut"}},
    ],
    "analyses": [{"kind": "differential_expression", "group1": "kras_wt", "group2": "kras_mut", "top_n": 10}],
    "named_tools": [{"tool": "gen3_analysis_api"}, {"tool": "differential_expression"}],
}


def views(name: str) -> list[RunView]:
    text = (FIXTURES / f"{name}.events.jsonl").read_text(encoding="utf-8")
    return [f.snapshot for f in map(parse_frame, iter_sse_data(text)) if isinstance(f, StateSnapshotFrame)]


def last_view(name: str) -> RunView:
    return views(name)[-1]


def first_plan(name: str) -> RunView:
    return next(v for v in views(name) if v.plan is not None)


def plan_dump(plan: PlanView) -> dict[str, Any]:  # Any: comparisons walk arbitrary JSON.
    dumped: dict[str, Any] = plan.model_dump(mode="json", exclude={"signature"})
    return dumped


def compiled(result: CompiledPlan | CompileStop) -> CompiledPlan:
    assert isinstance(result, CompiledPlan), result
    return result


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        if not FIXTURES.is_dir():
            self.skipTest(f"{FIXTURES} is not mounted")
        self.compiler = Compiler(vocabulary(), "active_cohort")

    def j3(self) -> CompiledPlan:
        return compiled(self.compiler.compile(ir.Plan.model_validate(J3_IR), context()))


class J1FixtureTests(FixtureCase):
    def test_j1_plan_view_is_the_j1_complete_plan(self) -> None:
        plan = compiled(self.compiler.compile(ir.Plan.model_validate(_record("j1-complete")["planHistory"][0]["ir"]), context()))
        self.assertEqual(plan_dump(plan.view()), plan_dump(_plan(first_plan("j1-complete"))))

    def test_j1_steps_are_the_recorded_plan_version(self) -> None:
        record = _record("j1-complete")
        plan = compiled(self.compiler.compile(ir.Plan.model_validate(record["planHistory"][0]["ir"]), context()))
        recorded = record["planVersions"][0]
        self.assertEqual([s.model_dump(mode="json") for s in plan.steps], recorded["steps"])
        self.assertEqual(plan.scope.model_dump(mode="json"), recorded["scope"])
        self.assertEqual([r.model_dump(mode="json") for r in plan.rows], recorded["rows"])

    def test_j1_scope_cohort_is_the_fixtures_base(self) -> None:
        plan = compiled(self.compiler.compile(ir.Plan.model_validate(_record("j1-complete")["planHistory"][0]["ir"]), context()))
        expected = _record("j1-complete")["cohorts"][0]
        base = plan.run_cohorts()[0].model_dump(mode="json")
        # Sized by s1 once it ran: the fact is the run's, not the plan's.
        self.assertEqual({**base, "caseCountFactId": "f.s1.cases"}, expected)
        self.assertEqual(plan.cohort("base").count_fact_id, "f.s1.cases")


class ConsentFixtureTests(FixtureCase):
    def test_use_oncomatrix_to_plot_survival_waits_for_consent_as_the_fixture_shows(self) -> None:
        fixture = last_view("consent")
        output = plan_question(fixture.question, context())
        assert isinstance(output, ir.Plan)
        plan = compiled(self.compiler.compile(output, context()))
        assert fixture.plan is not None
        # The whole plan, the scope edit included (TOOLS.md 6.4: every plan offers it; the card offers
        # edits only while the status accepts edit_plan, which AwaitingConsent does not).
        self.assertEqual(plan_dump(plan.view()), plan_dump(fixture.plan))
        self.assertIsInstance(fixture.pending, ConsentPending)
        self.assertEqual(plan.consent, fixture.pending)
        self.assertEqual(plan.limitations, fixture.limitations)
        self.assertEqual([s.queued() for s in plan.steps], fixture.steps)
        j1 = compiled(self.compiler.compile(ir.Plan.model_validate(_record("j1-complete")["planHistory"][0]["ir"]), context()))
        self.assertEqual(plan.signature, j1.signature, "a named tool never enters the signature")


class ClarifyFixtureTests(FixtureCase):
    def test_high_risk_patients_get_the_fixtures_clarification(self) -> None:
        fixture = last_view("clarify")
        output = plan_question(fixture.question, context())
        assert isinstance(output, ir.Clarify)
        pending = self.compiler.clarify(output, fixture.question, context())
        self.assertIsInstance(fixture.pending, ClarifyPending)
        self.assertEqual(pending, fixture.pending)


class J3FixtureTests(FixtureCase):
    def test_j3_plan_view_and_queued_steps_are_the_save_pause_fixtures(self) -> None:
        fixture = first_plan("j3-save-pause")
        plan = self.j3()
        assert fixture.plan is not None
        self.assertEqual(plan_dump(plan.view()), plan_dump(fixture.plan))
        self.assertEqual([s.queued() for s in plan.steps], fixture.steps)

    def test_both_phrasings_compile_the_fixture_plan(self) -> None:
        output = plan_question(J3_OPEN, context())
        assert isinstance(output, ir.Plan)
        self.assertEqual(compiled(self.compiler.compile(output, context())).signature, self.j3().signature)

    def test_run_cohorts_are_the_fixtures_before_sizing(self) -> None:
        fixture = first_plan("j3-save-pause")
        self.assertEqual([c.to_view() for c in self.j3().run_cohorts()], fixture.cohorts)

    def test_cohorts_size_from_the_steps_the_fixture_cites(self) -> None:
        paused = last_view("j3-save-pause")
        plan = self.j3()
        for cohort in paused.cohorts:
            with self.subTest(ref=cohort.ref):
                self.assertEqual(plan.cohort(cohort.ref).count_fact_id, cohort.case_count_fact_id)
                planned = plan.cohort(cohort.ref)
                if planned.definition is not None:
                    self.assertEqual(def_sha256(planned.definition), cohort.def_sha256)

    def test_the_pending_payload_is_the_fixtures_save_cohorts_args(self) -> None:
        args = _save_args("j3-save-pause")
        paused = last_view("j3-save-pause")
        pending = paused.pending
        assert isinstance(pending, ConfirmPending) and paused.plan is not None
        reads = _reads_for(args)
        plan = self.j3()
        built = build_confirm_pending(plan, nonce=args["nonce"], read=reads, context=context())
        expected = json.loads(json.dumps(args))
        expected["planSignature"] = plan.signature
        self.assertEqual(built.model_dump(mode="json"), expected)

    def test_rename_at_the_pause_is_version_two_reusing_s1_to_s7(self) -> None:
        plan = self.j3()
        edited = self.compiler.edit(
            plan, ir.Plan.model_validate(J3_IR),
            EditCohortNameAction(action="edit_plan", field="cohortName", value="KRAS wild-type (ORION)", ref="kras_wt"), context(),
        )
        result = edited.result
        assert isinstance(result, CompiledPlan)
        fixture = last_view("j3-edit-at-pause")
        assert fixture.plan is not None
        self.assertEqual(plan_dump(result.view()), plan_dump(fixture.plan))
        self.assertEqual([s.input_summary for s in result.steps], [s.input_summary for s in fixture.steps])
        before, after = step_input_hashes(plan), step_input_hashes(result)
        reused = [sid for sid in after if after[sid] == before.get(sid)]
        self.assertEqual(reused, [s.id for s in fixture.steps if s.status == "Reused"])
        self.assertNotEqual(result.signature, plan.signature)
        self.assertEqual(edited.ir.cohorts[2].name, "KRAS wild-type (ORION)")


class OncoMatrixFixtureTests(FixtureCase):
    def test_top_three_in_cgs_high_risk_is_the_fixtures_layout_plus_its_save(self) -> None:
        fixture = last_view("oncomatrix")
        output = plan_question(fixture.question, context())
        assert isinstance(output, ir.Plan) and fixture.plan is not None
        plan = compiled(self.compiler.compile(output, context()))
        # TOOLS.md choice 1: the fixture predates C-09; the question now compiles with a fifth step, the save.
        self.assertEqual([r.model_dump(mode="json") for r in plan.rows[:2]], [r.model_dump(mode="json") for r in fixture.plan.rows])
        self.assertEqual([s.queued().model_dump(include={"id", "row", "tool", "kind", "folded", "input_summary"}) for s in plan.steps[:4]],
                         [s.model_dump(include={"id", "row", "tool", "kind", "folded", "input_summary"}) for s in fixture.steps])
        self.assertEqual((plan.steps[4].tool, plan.rows[2].label, plan.writes), ("save_cohorts", "Save 1 cohort", True))
        self.assertEqual(plan.assumptions, fixture.plan.assumptions)
        high = next(c for c in fixture.cohorts if c.ref == "high")
        planned = plan.cohort("high")
        assert planned.definition is not None
        self.assertEqual((planned.definition_text, def_sha256(planned.definition), planned.count_fact_id),
                         (high.definition_text, high.def_sha256, high.case_count_fact_id))
        self.assertEqual(planned.name, "CGS high-risk (ORION)")


def _record(name: str) -> dict[str, Any]:  # Any: the record is read as plain JSON.
    value: dict[str, Any] = json.loads((FIXTURES / f"run-record.{name}.json").read_text(encoding="utf-8"))
    return value


def _plan(view: RunView) -> PlanView:
    assert view.plan is not None
    return view.plan


def _save_args(name: str) -> dict[str, Any]:  # Any: TOOL_CALL_ARGS JSON.
    text = (FIXTURES / f"{name}.events.jsonl").read_text(encoding="utf-8")
    deltas = [json.loads(d) for d in iter_sse_data(text)]
    value: dict[str, Any] = json.loads("".join(f["delta"] for f in deltas if f["type"] == "TOOL_CALL_ARGS"))
    return value


def _reads_for(args: dict[str, Any]) -> Any:  # Any: a StepValueReader over synthetic calls.
    """The step outputs the J3 pause read: s2 KRAS-mutated, s3 tested (every case), s5 the difference,
    s7 eligible counts, consistent with the fixture's pending definitions."""
    by_ref = {c["ref"]: c for c in args["cohorts"]}
    mutated = by_ref["kras_mut"]["definition"]["caseIds"]
    rest = by_ref["kras_wt"]["definition"]["caseIds"]
    tested = sorted(mutated + rest)
    calls: dict[str, JsonValue] = {
        "s2": {"observed": {"gene": "KRAS", "count": len(mutated), "caseIds": mutated}},
        "s3": {"observed": {"count": len(tested), "caseIds": tested, "pages": 1, "complete": True}},
        "s5": {"observed": {"count": len(rest), "caseIds": rest}},
        "s7": {"observed": {"groups": [{"key": "control", "eligible": by_ref["kras_wt"]["eligibleCount"]},
                                       {"key": "case", "eligible": by_ref["kras_mut"]["eligibleCount"]}]}},
    }

    def read(step_id: str, pointer: str) -> JsonValue:
        return resolve_pointer(calls[step_id], pointer)

    return read


if __name__ == "__main__":
    unittest.main()
