"""Plan edits (contract "The edit sequence", TOOLS.md 6.4): each edit compiles the patched IR into plan
version n+1 with no model call, every real edit changes the signature, the input hashes say which
steps are Reused, a value equal to the current one is a no-op, and a field the plan cannot take is
INVALID_INPUT. E15: the switch chip after a follow-up names the scope it left."""

import unittest

from pydantic import JsonValue
from support import context, vocabulary
from test_compiler_cases import (
    CGS,
    J1,
    J2,
    J3,
    J3_RUN,
    RACE,
    RACE_ROOT,
    compiled,
    compiler,
    j3_ledger,
    plan,
    race_context,
    saved_j3_cohorts,
)

from orion_agent import ir
from orion_agent.compiler import CompiledPlan, Compiler, EditRejected, PlanEdit, SessionLedger, step_input_hashes
from orion_agent.models import (
    CohortContext,
    DeThresholds,
    EditCohortNameAction,
    EditMethodAction,
    EditPlanAction,
    EditScopeAction,
    EditThresholdsAction,
    EditTopNAction,
    ScopeChoice,
)


def scope_edit(source: str, ctx: CohortContext, **choice: str) -> EditScopeAction:
    return EditScopeAction(action="edit_plan", field="scope", value=ScopeChoice.model_validate({"source": source, **choice}), cohort_context=ctx)


class EditCase(unittest.TestCase):
    def setUp(self) -> None:
        self.compiler = compiler()

    def start(self, data: dict[str, JsonValue] | ir.Plan, ctx: CohortContext | None = None,
              ledger: SessionLedger | None = None) -> tuple[CompiledPlan, ir.Plan]:
        output = data if isinstance(data, ir.Plan) else plan(data)
        result = compiled(self.compiler.compile(output, ctx or context(), ledger))
        return result, output

    def edit(self, current: CompiledPlan, current_ir: ir.Plan, action: EditPlanAction, ctx: CohortContext | None = None,
             ledger: SessionLedger | None = None) -> tuple[CompiledPlan, PlanEdit]:
        edited = self.compiler.edit(current, current_ir, action, ctx or context(), ledger)
        assert isinstance(edited.result, CompiledPlan), edited.result
        self.assertEqual(edited.result.version, current.version + 1)
        self.assertNotEqual(edited.result.signature, current.signature, "every real edit changes the signature")
        return edited.result, edited

    def reused(self, before: CompiledPlan, after: CompiledPlan) -> list[str]:
        old, new = step_input_hashes(before), step_input_hashes(after)
        return [sid for sid in new if new[sid] == old.get(sid)]


class DeEditTests(EditCase):
    def test_method_is_an_argument_of_de_run(self) -> None:
        v1, ir1 = self.start(J3)
        v2, edited = self.edit(v1, ir1, EditMethodAction(action="edit_plan", field="method", value="edgeR"))
        self.assertEqual(v2.step("s9").args["method"], "edgeR")
        self.assertEqual((v2.rows[5].label, v2.step("s9").input_summary),
                         ("Run DE: edgeR, rank by adjusted p", "edgeR, KRAS not detected (ORION) vs. KRAS mutated (ORION)"))
        self.assertNotIn("DE_METHOD_WILCOXON", [a.code for a in v2.assumptions])
        self.assertEqual(self.reused(v1, v2), [f"s{i}" for i in range(1, 9)])
        analysis = edited.ir.analyses[0]
        assert isinstance(analysis, ir.DifferentialExpression)
        self.assertEqual(analysis.method, "edgeR")
        self.assertEqual([e.value for e in v2.editable if e.field == "method"], ["edgeR"])

    def test_top_n_is_the_rankings_n(self) -> None:
        v1, ir1 = self.start(J3)
        v2, _ = self.edit(v1, ir1, EditTopNAction(action="edit_plan", field="topN", value=20))
        self.assertEqual((v2.step("s10").args["n"], v2.step("s10").input_summary), (20, "Top 20 by adjusted p"))
        self.assertEqual(self.reused(v1, v2), [f"s{i}" for i in range(1, 10)])

    def test_thresholds_are_arguments_of_de_run_and_the_ranking(self) -> None:
        v1, ir1 = self.start(J3)
        thresholds = DeThresholds(p_value_cutoff=0.01, p_value_type="adjusted", fold_change_cutoff=1.0)
        v2, _ = self.edit(v1, ir1, EditThresholdsAction(action="edit_plan", field="thresholds", value=thresholds))
        expected = {"pValueCutoff": 0.01, "pValueType": "adjusted", "foldChangeCutoff": 1.0}
        self.assertEqual((v2.step("s9").args["thresholds"], v2.step("s10").args["thresholds"]), (expected, expected))
        self.assertNotIn("DE_THRESHOLDS_VIEWER", [a.code for a in v2.assumptions])
        self.assertEqual(self.reused(v1, v2), [f"s{i}" for i in range(1, 9)])

    def test_a_rename_reaches_the_save_and_de_run_only(self) -> None:
        v1, ir1 = self.start(J3)
        v2, _ = self.edit(v1, ir1, EditCohortNameAction(action="edit_plan", field="cohortName", value="Wild type", ref="kras_wt"))
        cohorts = v2.step("s8").args["cohorts"]
        assert isinstance(cohorts, list) and isinstance(cohorts[0], dict)
        self.assertEqual(cohorts[0]["name"], "Wild type")
        groups = v2.step("s9").args["groups"]
        assert isinstance(groups, list) and isinstance(groups[0], dict)
        self.assertEqual(groups[0]["name"], "Wild type")
        self.assertEqual(self.reused(v1, v2), [f"s{i}" for i in range(1, 8)])
        self.assertEqual(v2.cohort("kras_wt").name, "Wild type")

    def test_values_equal_to_the_current_ones_are_no_ops(self) -> None:
        v1, ir1 = self.start(J3)
        same: list[EditPlanAction] = [
            EditMethodAction(action="edit_plan", field="method", value="wilcoxon"),
            EditTopNAction(action="edit_plan", field="topN", value=10),
            EditThresholdsAction(action="edit_plan", field="thresholds", value=DeThresholds(p_value_cutoff=0.05, p_value_type="adjusted", fold_change_cutoff=0.3)),
            EditCohortNameAction(action="edit_plan", field="cohortName", value="KRAS mutated (ORION)", ref="kras_mut"),
            scope_edit("active_cohort", context()),
        ]
        for action in same:
            with self.subTest(field=action.field):
                edited = self.compiler.edit(v1, ir1, action, context())
                self.assertTrue(edited.no_op)
                self.assertIsNone(edited.result)
                self.assertIs(edited.ir, ir1)


class RejectedEditTests(EditCase):
    def test_fields_the_plan_does_not_offer_are_invalid_input(self) -> None:
        v1, ir1 = self.start(J1)
        cases: list[EditPlanAction] = [
            EditMethodAction(action="edit_plan", field="method", value="limma"),
            EditTopNAction(action="edit_plan", field="topN", value=5),
            EditThresholdsAction(action="edit_plan", field="thresholds", value=DeThresholds(p_value_cutoff=0.1, p_value_type="original", fold_change_cutoff=0)),
            EditCohortNameAction(action="edit_plan", field="cohortName", value="x", ref="kras_wt"),
        ]
        for action in cases:
            with self.subTest(field=action.field), self.assertRaises(EditRejected) as raised:
                self.compiler.edit(v1, ir1, action, context())
            self.assertEqual(raised.exception.code, "INVALID_INPUT")

    def test_top_n_above_the_editables_max_and_a_ref_that_is_not_proposed(self) -> None:
        genes, genes_ir = self.start(J2)
        with self.assertRaises(EditRejected):
            self.compiler.edit(genes, genes_ir, EditTopNAction(action="edit_plan", field="topN", value=51), context())
        de, de_ir = self.start(J3)
        with self.assertRaises(EditRejected):
            self.compiler.edit(de, de_ir, EditCohortNameAction(action="edit_plan", field="cohortName", value="x", ref="tested"), context())

    def test_a_compiler_that_offers_no_edits_takes_none(self) -> None:
        quiet = Compiler(vocabulary(), "active_cohort", offer_edits=False)
        v1 = compiled(quiet.compile(plan(J1), context()))
        self.assertEqual(v1.editable, [])
        self.assertEqual(v1.signature, self.start(J1)[0].signature, "editables are not part of the signature")
        with self.assertRaises(EditRejected):
            quiet.edit(v1, plan(J1), scope_edit("all_cases", context()), context())


class GenesEditTests(EditCase):
    def test_top_n_is_also_the_gene_count(self) -> None:
        v1, ir1 = self.start(J2)
        v2, _ = self.edit(v1, ir1, EditTopNAction(action="edit_plan", field="topN", value=5))
        self.assertEqual((v2.step("s3").args["geneCount"], v2.step("s4").args["n"]), (5, 5))
        self.assertEqual(v2.rows[1].label, "Rank mutated genes: top 5")
        self.assertEqual(self.reused(v1, v2), ["s1", "s2", "s5"])

    def test_renaming_the_race_cohort(self) -> None:
        v1, ir1 = self.start(J2)
        v2, _ = self.edit(v1, ir1, EditCohortNameAction(action="edit_plan", field="cohortName", value="Black patients (ORION)", ref="black"))
        self.assertEqual(v2.step("s5").args, {"cohorts": [{"ref": "black", "name": "Black patients (ORION)"}]})
        self.assertEqual(v2.step("s5").input_summary, "Black patients (ORION)")


class ScopeEditTests(EditCase):
    def test_a_scope_edit_recompiles_every_step_and_names_the_scope_it_left(self) -> None:
        ctx = race_context()
        v1, ir1 = self.start(J1, ctx)
        v2, edited = self.edit(v1, ir1, scope_edit("all_cases", ctx), ctx)
        self.assertEqual((v2.scope.source, v2.scope.name), ("all_cases", "All cases"))
        self.assertNotIn("cases_query", [s.tool for s in v2.steps])
        self.assertEqual(self.reused(v1, v2), ["s1"])
        assert v2.alt_scope is not None
        self.assertEqual((v2.alt_scope.source, v2.alt_scope.name, v2.alt_scope_choice), ("active_cohort", "Black or African American", ScopeChoice(source="active_cohort")))
        self.assertEqual(edited.ir.base_cohort, ir.BaseCohort(source="all_cases"))
        self.assertEqual([e.value for e in v2.editable if e.field == "scope"], [ScopeChoice(source="all_cases")])
        # The chip switches back: version 3 is on the active cohort again and offers all cases.
        assert v2.alt_scope_choice is not None
        v3, _ = self.edit(v2, edited.ir, scope_edit(v2.alt_scope_choice.source, ctx), ctx)
        self.assertEqual((v3.scope.source, v3.alt_scope.source if v3.alt_scope else None), ("active_cohort", "all_cases"))
        self.assertEqual([s.args for s in v3.steps], [s.args for s in v1.steps])

    def test_a_saved_cohort_scope(self) -> None:
        saved: list[dict[str, JsonValue]] = [{"id": "c-iii", "name": "Stage III", "caseCount": 300, "filters": {
            "mode": "and", "root": {"diagnoses.iss_stage": {"operator": "in", "field": "diagnoses.iss_stage", "operands": ["Stage III"]}}}}]
        ctx = context(saved=saved)
        v1, ir1 = self.start(J1, ctx)
        v2, _ = self.edit(v1, ir1, scope_edit("saved_cohort", ctx, cohortName="Stage III"), ctx)
        self.assertEqual((v2.scope.source, v2.scope.name, v2.scope.cohort_id, v2.scope.case_count), ("saved_cohort", "Stage III", "c-iii", 300))
        self.assertEqual(v2.cohort("base").cohort_id, "c-iii")
        self.assertEqual([e.value for e in v2.editable if e.field == "scope"], [ScopeChoice(source="saved_cohort", cohort_name="Stage III")])

    def test_e15_the_switch_chip_after_a_follow_up(self) -> None:
        """J3, then an active-cohort switch, then "Show the top 5 instead": the plan reuses the thread
        cohorts on the thread scope and offers the new active cohort; the chip makes version 2 on the
        active cohort, which offers the thread scope back."""
        ctx = race_context(saved_j3_cohorts())
        top5 = plan(
            {"kind": "plan", "analyses": [{"kind": "differential_expression", "group1": "kras_wt", "group2": "kras_mut", "top_n": 5}]},
            follows=J3_RUN,
            cohorts=[{"ref": "kras_wt", "def": {"kind": "thread", "ledger_ref": "kras_wt"}},
                     {"ref": "kras_mut", "def": {"kind": "thread", "ledger_ref": "kras_mut"}}],
        )
        ledger = j3_ledger()
        v1, ir1 = self.start(top5, ctx, ledger)
        self.assertEqual((v1.scope.source, v1.alt_scope.source if v1.alt_scope else None, v1.writes), ("thread_cohort", "active_cohort", False))
        assert v1.alt_scope_choice is not None
        v2, edited = self.edit(v1, ir1, EditScopeAction(action="edit_plan", field="scope", value=v1.alt_scope_choice, cohort_context=ctx), ctx, ledger)
        self.assertEqual((v2.scope.source, v2.scope.name), ("active_cohort", "Black or African American"))
        assert v2.alt_scope is not None
        self.assertEqual((v2.alt_scope.source, v2.alt_scope.name, v2.alt_scope_choice), ("thread_cohort", "All cases", ScopeChoice(source="thread_cohort")))
        # The thread cohorts rebuild within the new scope: new case lists, so they are proposed for saving.
        scope = {"in": {"case_id": {"ref": "s2", "pointer": "/observed/caseIds"}}}
        query = v2.step("s3").args["filter"]
        assert isinstance(query, dict) and isinstance(query["and"], list)
        self.assertEqual(query["and"][0], scope)
        self.assertTrue(v2.writes)
        self.assertEqual(edited.ir.follows, J3_RUN)
        self.assertEqual([s.tool for s in v2.steps][:3], ["fixture_fingerprint", "cases_query", "cases_query"])

    def test_a_scope_edit_can_fail_to_compile_without_raising(self) -> None:
        v1, ir1 = self.start(J1)
        edited = self.compiler.edit(v1, ir1, scope_edit("saved_cohort", context(), cohortName="Gone"), context())
        self.assertFalse(edited.no_op)
        assert edited.result is not None and not isinstance(edited.result, CompiledPlan)
        self.assertEqual(edited.result.code, "UNKNOWN_VALUE")

    def test_the_same_scope_choice_is_a_no_op_even_when_the_cohort_behind_it_changed(self) -> None:
        # Contract: an edit is a no-op when the value equals the current editable value, whatever the signature.
        v1, ir1 = self.start(J1)
        bad = context({RACE: {"operator": "excludes", "field": RACE, "operands": ["asian"]}})
        self.assertTrue(self.compiler.edit(v1, ir1, scope_edit("active_cohort", bad), bad).no_op)


class SignatureCoverageTests(EditCase):
    """Every editable value is an argument of the steps it affects (contract "What an edit changes")."""

    def test_each_editable_value_appears_in_some_steps_args(self) -> None:
        for data in (J2, J3):
            result, _ = self.start(data)
            args = str([s.args for s in result.steps])
            for entry in result.editable:
                with self.subTest(field=entry.field):
                    if entry.field == "scope":
                        continue  # the scope is the signature's own part
                    value = entry.value
                    shown = value.model_dump(mode="json", by_alias=True) if isinstance(value, DeThresholds) else value
                    self.assertIn(str(shown) if not isinstance(shown, dict) else str(shown["pValueCutoff"]), args)

    def test_the_scope_is_in_the_signature_but_its_name_and_count_are_not(self) -> None:
        a, _ = self.start(J1, context(RACE_ROOT, name="Black patients", case_count=334))
        b, _ = self.start(J1, context(RACE_ROOT, name="Renamed", case_count=None))
        c, _ = self.start(J1, context({CGS: {"operator": "in", "field": CGS, "operands": ["High risk"]}}, name="Black patients"))
        self.assertEqual(a.signature, b.signature)
        self.assertNotEqual(a.signature, c.signature)


if __name__ == "__main__":
    unittest.main()
