"""Compiler rules C-SCOPE, C-VOCAB, C-PANEL and C-UNTESTED, and the plan signature (the Day 1 rules;
test_compiler_rules.py has the rest, test_compiler_edits.py the edits, test_compiler_tools_md.py the
step tables and test_compiler_fixtures.py the fixtures)."""

import json
import os
import unittest
import urllib.error
import urllib.request

from pydantic import JsonValue, ValidationError
from support import context, vocabulary

from orion_agent import ir
from orion_agent.compiler import (
    CompiledPlan,
    Compiler,
    CompileStop,
    LedgerCohort,
    LedgerRun,
    ResolvedScope,
    SessionLedger,
    apply_untested_rule,
)
from orion_agent.ids import new_id
from orion_agent.models import CohortContext
from orion_agent.settings import DefaultScope
from orion_agent.stubs import CGS_RISK, j1_plan
from orion_agent.vocab import VocabProblem, clauses_from_filter_set

RACE_FILTER: dict[str, dict[str, JsonValue]] = {
    "demographic.race": {"operator": "in", "field": "demographic.race", "operands": ["Black or African American"]}
}
AGE = "diagnoses.age_at_diagnosis"


def _age(operator: str, operand: int) -> dict[str, JsonValue]:
    return {"operator": operator, "field": AGE, "operand": operand}


# How @gen3/core's convertNumericFromToArrayToFilters stores a from-to range (isNested false).
AGE_RANGE_FILTER: dict[str, dict[str, JsonValue]] = {AGE: {"operator": "and", "operands": [_age(">=", 50), _age("<", 70)]}}


def compile_plan(
    plan: ir.Plan, ctx: CohortContext | None = None, ledger: SessionLedger | None = None, default_scope: DefaultScope = "active_cohort"
) -> CompiledPlan | CompileStop:
    return Compiler(vocabulary(), default_scope).compile(plan, ctx or context(), ledger)


def compiled(result: CompiledPlan | CompileStop) -> CompiledPlan:
    assert isinstance(result, CompiledPlan), result
    return result


def stopped(result: CompiledPlan | CompileStop) -> CompileStop:
    assert isinstance(result, CompileStop), result
    return result


def plan_with(**fields: JsonValue) -> ir.Plan:
    base: dict[str, JsonValue] = {"kind": "plan", "analyses": [{"kind": "cohort_size", "cohort": "base"}]}
    return ir.Plan.model_validate({**base, **fields})


class VocabTests(unittest.TestCase):
    def test_cgs_values_normalize_to_the_stored_spelling(self) -> None:
        clause = ir.Clause(field=CGS_RISK, op="in", values=["high risk", "STANDARD   RISK"])
        checked = vocabulary().check_clause(clause)
        assert isinstance(checked, ir.Clause)
        self.assertEqual(checked.values, ["High risk", "Standard risk"])

    def test_race_normalizes_to_the_lowercase_stored_spelling(self) -> None:
        checked = vocabulary().check_clause(ir.Clause(field="demographic.race", op="=", values=["Black or African American"]))
        assert isinstance(checked, ir.Clause)
        self.assertEqual(checked.values, ["black or african american"])

    def test_equals_is_a_one_value_in(self) -> None:
        checked = vocabulary().check_clause(ir.Clause(field="demographic.race", op="=", values=["Asian"]))
        assert isinstance(checked, ir.Clause)
        self.assertEqual((checked.op, checked.values), ("in", ["asian"]))

    def test_cohort_names_and_value_previews_come_from_the_vocabulary(self) -> None:
        vocab = vocabulary()
        cgs, race, iss = vocab.field(CGS_RISK), vocab.field("demographic.race"), vocab.field("diagnoses.iss_stage")
        assert cgs is not None and race is not None and iss is not None
        self.assertEqual((cgs.display("High risk"), race.display("black or african american"), iss.display("Stage III")),
                         ("CGS high-risk", "Black or African American", "Stage III"))
        self.assertEqual((cgs.preview(), iss.preview()), ("High risk, Standard risk", "Stage I, Stage II, Stage III"))
        other = vocab.field("cgs_risk_key_criteria.other_criteria")
        assert other is not None
        self.assertTrue(other.preview().endswith(" and 15 more"))
        self.assertIsNone(vocab.categorical("diagnoses.age_at_diagnosis"))
        self.assertIsNone(vocab.categorical("gene.symbol"), "gene fields are C-PANEL's, never a split")

    def test_values_keep_the_stored_json_type(self) -> None:
        ecog = vocabulary().check_clause(ir.Clause(field="follow_ups.ecog_performance_status", op="in", values=[0]))
        censor = vocabulary().check_clause(ir.Clause(field="outcomes.censor_os", op="in", values=["1"]))
        assert isinstance(ecog, ir.Clause) and isinstance(censor, ir.Clause)
        self.assertEqual((ecog.values, censor.values), (["0"], [1]))

    def test_unknown_field_value_and_operator(self) -> None:
        vocab = vocabulary()
        cases = {
            "UNKNOWN_FIELD": ir.Clause(field="demographic.shoe_size", op="in", values=["9"]),
            "UNKNOWN_VALUE": ir.Clause(field=CGS_RISK, op="in", values=["very high risk"]),
        }
        for code, clause in cases.items():
            problem = vocab.check_clause(clause)
            assert isinstance(problem, VocabProblem)
            self.assertEqual(problem.code, code)
        ranged = vocab.check_clause(ir.Clause(field=CGS_RISK, op=">", values=["High risk"]))
        assert isinstance(ranged, VocabProblem)
        self.assertEqual(ranged.code, "UNSUPPORTED_FIELD")
        numeric = vocab.check_clause(ir.Clause(field="diagnoses.age_at_diagnosis", op=">=", values=["65"]))
        assert isinstance(numeric, ir.Clause)
        self.assertEqual(numeric.values, [65])

    def test_elasticsearch_only_fields_are_unsupported_in_the_proteinpaint_bridge(self) -> None:
        vocab = vocabulary()
        for field, value in (("available_variation_data", "ssm"), ("gene.symbol", "KRAS"), ("summary.experimental_strategies.experimental_strategy", "WGS")):
            clause = ir.Clause(field=field, op="in", values=[value])
            self.assertIsInstance(vocab.check_clause(clause), ir.Clause, field)
            problem = vocab.check_clause(clause, engine="pp_bridge")
            assert isinstance(problem, VocabProblem)
            self.assertEqual(problem.code, "UNSUPPORTED_FIELD", field)
        self.assertIsInstance(vocab.check_clause(ir.Clause(field=CGS_RISK, op="in", values=["High risk"]), engine="pp_bridge"), ir.Clause)

    def test_proteinpaint_twins(self) -> None:
        spec = vocabulary().field(CGS_RISK)
        assert spec is not None
        self.assertEqual(spec.pp_twin, "cgs_risk_category")
        race = vocabulary().field("demographic.race")
        assert race is not None
        self.assertEqual(race.pp_twin, "race")

    def test_browser_filter_sets_are_read_into_clauses(self) -> None:
        clauses, problems = clauses_from_filter_set(
            context(
                {
                    **RACE_FILTER,
                    "diagnoses.age_at_diagnosis": {"operator": ">=", "field": "diagnoses.age_at_diagnosis", "operand": 60},
                    "case_id": {"operator": "in", "field": "case_id", "operands": [{"cases.case_id": "SYNTH-CASE-0001"}]},
                }
            ).active.filters
        )
        self.assertEqual(problems, [])
        self.assertEqual([(c.field, c.op, c.values) for c in clauses][1:], [("diagnoses.age_at_diagnosis", ">=", [60]), ("case_id", "in", ["SYNTH-CASE-0001"])])

    def test_and_groups_are_flattened_into_clauses(self) -> None:
        nested: dict[str, dict[str, JsonValue]] = {
            AGE: {"operator": "and", "operands": [_age(">=", 50), {"operator": "and", "operands": [_age("<", 70), _age("!=", 60)]}]}
        }
        clauses, problems = clauses_from_filter_set(context(nested).active.filters)
        self.assertEqual(problems, [])
        self.assertEqual([(c.field, c.op, c.values) for c in clauses], [(AGE, ">=", [50]), (AGE, "<", [70]), (AGE, "!=", [60])])

    def test_or_nested_and_exclusion_operations_inside_an_and_stay_problems(self) -> None:
        parts: list[tuple[str, dict[str, JsonValue]]] = [
            ("or", {"operator": "or", "operands": [_age(">=", 50), _age("<", 30)]}),
            ("nested", {"operator": "nested", "path": "diagnoses", "operand": _age(">=", 50)}),
            ("excludes", {"operator": "excludes", "field": CGS_RISK, "operands": ["High risk"]}),
            ("excludeifany", {"operator": "excludeifany", "field": CGS_RISK, "operands": ["High risk"]}),
        ]
        for operator, part in parts:
            with self.subTest(operator):
                root: dict[str, dict[str, JsonValue]] = {AGE: {"operator": "and", "operands": [_age(">=", 50), part]}}
                _, problems = clauses_from_filter_set(context(root).active.filters)
                self.assertEqual([(p.code, f"The {operator} filter" in p.text) for p in problems], [("UNSUPPORTED_FIELD", True)])


class J1CompileTests(unittest.TestCase):
    def test_j1_compiles_to_the_fixture_layout(self) -> None:
        plan = compiled(compile_plan(j1_plan()))
        self.assertEqual([(s.id, s.row, s.tool, s.kind, s.folded) for s in plan.steps], [
            ("s1", 1, "fixture_fingerprint", "server", True),
            ("s2", 1, "cohort_count", "server", False),
            ("s3", 1, "cohort_count", "server", False),
            ("s4", 2, "survival_compare", "server", False),
            ("s5", 2, "derive_km_median", "exec", False),
        ])
        self.assertEqual(plan.steps[1].args, {"filter": {"and": [{"in": {CGS_RISK: ["High risk"]}}]}})
        self.assertEqual([a.code for a in plan.assumptions], ["DEFAULT_SCOPE_ACTIVE", "SPLIT_CGS_RISK"])
        self.assertEqual(plan.scope.view().model_dump(), {"source": "active_cohort", "name": "All cases", "caseCount": 1000})
        self.assertFalse(plan.writes)

    def test_split_values_default_to_every_value_of_the_field(self) -> None:
        plan = compiled(compile_plan(ir.Plan(kind="plan", analyses=[ir.SurvivalCompare(kind="survival_compare", split_term="diagnoses.iss_stage")])))
        self.assertEqual([s.input_summary for s in plan.steps if s.tool == "cohort_count"], ["ISS stage = Stage I", "ISS stage = Stage II", "ISS stage = Stage III"])
        self.assertEqual(plan.rows[0].label, "Count the 3 ISS stage groups")

    def test_every_analysis_kind_compiles(self) -> None:
        cohorts: list[JsonValue] = [{"ref": "m", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "base"}},
                                    {"ref": "f", "def": {"kind": "filter", "clauses": [{"field": CGS_RISK, "op": "in", "values": ["High risk"]}]}}]
        cases: dict[str, JsonValue] = {
            "top_mutated_genes": {"kind": "top_mutated_genes", "cohort": "base"},
            "de_run": {"kind": "differential_expression", "group1": "f", "group2": "m"},
            "survival_compare": {"kind": "survival_compare", "groups": ["f", "m"]},
            "cohort_count": {"kind": "cohort_size", "cohort": "f"},
        }
        for tool, analysis in cases.items():
            with self.subTest(tool=tool):
                plan = compiled(compile_plan(plan_with(cohorts=cohorts, analyses=[analysis])))
                self.assertIn(tool, [s.tool for s in plan.steps])
                self.assertEqual(plan.steps[0].tool, "fixture_fingerprint")

    def test_split_on_a_numeric_or_unknown_field_stops(self) -> None:
        numeric = stopped(compile_plan(ir.Plan(kind="plan", analyses=[ir.SurvivalCompare(kind="survival_compare", split_term="diagnoses.age_at_diagnosis")])))
        self.assertEqual(numeric.code, "UNSUPPORTED_FIELD")
        unknown = stopped(compile_plan(ir.Plan(kind="plan", analyses=[ir.SurvivalCompare(kind="survival_compare", split_term="nope.field")])))
        self.assertEqual(unknown.code, "UNKNOWN_FIELD")


class PanelTests(unittest.TestCase):
    def test_other_gene_stops_out_of_panel_with_zero_steps(self) -> None:
        plan = plan_with(cohorts=[{"ref": "m", "def": {"kind": "gene_mutated", "gene": "OTHER", "within": "base"}}],
                         analyses=[{"kind": "cohort_size", "cohort": "m"}])
        stop = stopped(compile_plan(plan))
        self.assertEqual(stop.code, "OUT_OF_PANEL")
        self.assertEqual([(lim.code, lim.nearest_apps) for lim in stop.limitations], [("OUT_OF_PANEL", [])])
        self.assertFalse(hasattr(stop, "steps"))

    def test_idh1_filter_stops_out_of_panel_before_vocabulary(self) -> None:
        plan = plan_with(cohorts=[{"ref": "m", "def": {"kind": "filter", "clauses": [{"field": "gene.symbol", "op": "in", "values": ["IDH1"]}]}}])
        stop = stopped(compile_plan(plan))
        self.assertEqual(stop.code, "OUT_OF_PANEL")
        self.assertIn("IDH1", stop.limitations[0].text)

    def test_idh1_cannot_enter_the_ir_as_a_gene(self) -> None:
        with self.assertRaises(ValidationError):
            plan_with(cohorts=[{"ref": "m", "def": {"kind": "gene_mutated", "gene": "IDH1", "within": "base"}}])

    def test_panel_genes_compile(self) -> None:
        plan = plan_with(cohorts=[{"ref": "m", "def": {"kind": "gene_mutated", "gene": "DIS3", "within": "base"}}],
                         analyses=[{"kind": "cohort_size", "cohort": "m"}])
        self.assertIn("gene_cases", [s.tool for s in compiled(compile_plan(plan)).steps])


class ScopeTests(unittest.TestCase):
    saved: list[dict[str, JsonValue]] = [{"id": "c1", "name": "KRAS cohort", "filters": {"mode": "and", "root": {"demographic.gender": {"operator": "in", "field": "demographic.gender", "operands": ["Female"]}}}, "caseCount": 500}]

    def _ledger(self, run_id: str) -> SessionLedger:
        scope = ResolvedScope(source="active_cohort", name="Stage III", case_count=345,
                              clauses=[ir.Clause(field="diagnoses.iss_stage", op="in", values=["Stage III"])])
        cohort = LedgerCohort(ref="kras_wt", name="KRAS not detected", clauses=[ir.Clause(field="demographic.race", op="in", values=["asian"])])
        return SessionLedger(runs=[LedgerRun(run_id=run_id, scope=scope, cohorts=[cohort])])

    def test_named_saved_cohort_wins_over_everything(self) -> None:
        run_id = new_id()
        plan = plan_with(base_cohort={"source": "saved_cohort", "cohort_name": "kras COHORT"}, follows=run_id)
        result = compiled(compile_plan(plan, context(RACE_FILTER, name="Black patients", saved=self.saved), self._ledger(run_id)))
        self.assertEqual(result.scope.view().model_dump(), {"source": "saved_cohort", "name": "KRAS cohort", "caseCount": 500})
        self.assertEqual(result.scope.clauses[0].values, ["female"])
        self.assertEqual([s.tool for s in result.steps].count("cases_query"), 1)
        self.assertEqual([a.code for a in result.assumptions], [])

    def test_missing_saved_cohort_stops_with_a_limitation(self) -> None:
        stop = stopped(compile_plan(plan_with(base_cohort={"source": "saved_cohort", "cohort_name": "Nope"})))
        self.assertEqual(stop.code, "UNKNOWN_VALUE")

    def test_follow_up_uses_the_thread_cohort_from_the_ledger(self) -> None:
        run_id = new_id()
        follows = compiled(compile_plan(plan_with(follows=run_id), context(RACE_FILTER), self._ledger(run_id)))
        self.assertEqual((follows.scope.source, follows.scope.name), ("thread_cohort", "Stage III"))
        by_ref = compiled(compile_plan(plan_with(base_cohort={"source": "thread_cohort", "ledger_ref": "kras_wt"}, follows=run_id), None, self._ledger(run_id)))
        self.assertEqual((by_ref.scope.name, by_ref.scope.clauses[0].values), ("KRAS not detected", ["asian"]))

    def test_follow_up_without_a_ledger_is_a_limitation_then_the_default(self) -> None:
        result = compiled(compile_plan(plan_with(follows=new_id()), context(RACE_FILTER, name="Black patients")))
        self.assertEqual([lim.code for lim in result.limitations], ["UNKNOWN_VALUE"])
        self.assertIn("so the default scope, Black patients, is used", result.limitations[0].text)
        self.assertEqual((result.scope.source, [a.code for a in result.assumptions]), ("active_cohort", ["DEFAULT_SCOPE_ACTIVE"]))

    def test_a_scope_the_question_states_wins_over_the_follow_up(self) -> None:
        run_id = new_id()
        ctx = context(RACE_FILTER, name="Black patients", case_count=334)
        for source, name in (("all_cases", "All cases"), ("active_cohort", "Black patients")):
            plan = plan_with(base_cohort={"source": source}, follows=run_id)
            for ledger in (self._ledger(run_id), None):
                with self.subTest(source=source, ledger=ledger is not None):
                    result = compiled(compile_plan(plan, ctx, ledger))
                    self.assertEqual((result.scope.source, result.scope.name), (source, name))
                    self.assertEqual((result.limitations, result.assumptions), ([], []))

    def test_a_named_thread_cohort_missing_from_the_ledger_stops(self) -> None:
        plan = plan_with(base_cohort={"source": "thread_cohort", "ledger_ref": "kras_wt"}, follows=new_id())
        stop = stopped(compile_plan(plan))
        self.assertEqual((stop.code, [lim.code for lim in stop.limitations]), ("UNKNOWN_VALUE", ["UNKNOWN_VALUE"]))

    def test_explicit_all_cases_ignores_a_filtered_active_cohort(self) -> None:
        result = compiled(compile_plan(plan_with(base_cohort={"source": "all_cases"}), context(RACE_FILTER, name="Black patients", case_count=334)))
        self.assertEqual(result.scope.view().model_dump(), {"source": "all_cases", "name": "All cases", "caseCount": None})
        self.assertNotIn("cases_query", [s.tool for s in result.steps])
        self.assertEqual(result.assumptions, [])

    def test_default_scope_is_the_filtered_active_cohort_with_one_cases_query(self) -> None:
        result = compiled(compile_plan(j1_plan(), context(RACE_FILTER, name="Black patients", case_count=334)))
        self.assertEqual(result.scope.view().model_dump(), {"source": "active_cohort", "name": "Black patients", "caseCount": 334})
        queries = [s for s in result.steps if s.tool == "cases_query"]
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].args["filter"], {"and": [{"in": {"demographic.race": ["black or african american"]}}]})
        self.assertEqual(result.rows[0].label, "Find cases in Black patients")
        self.assertEqual(result.assumptions[0].text, "No cohort named: using the active cohort, Black patients")
        # C-23: the browser filter is read once; every later filter names the scope's case IDs.
        counts = [s for s in result.steps if s.tool == "cohort_count"]
        scope = {"in": {"case_id": {"ref": queries[0].id, "pointer": "/observed/caseIds"}}}
        self.assertEqual(counts[0].args["filter"], {"and": [scope, {"in": {CGS_RISK: ["High risk"]}}]})
        self.assertEqual(counts[0].depends_on, [queries[0].id])

    def test_default_scope_all_cases_setting(self) -> None:
        result = compiled(compile_plan(j1_plan(), context(RACE_FILTER), default_scope="all_cases"))
        self.assertEqual((result.scope.source, [a.code for a in result.assumptions][0]), ("all_cases", "DEFAULT_SCOPE_ALL"))

    def test_unfiltered_active_cohort_needs_no_scope_step(self) -> None:
        self.assertNotIn("cases_query", [s.tool for s in compiled(compile_plan(j1_plan())).steps])

    def test_unfiltered_active_cohort_is_named_all_cases_with_the_browser_count(self) -> None:
        for count in (1000, None):
            with self.subTest(count=count):
                result = compiled(compile_plan(j1_plan(), context(name="Unsaved cohort", case_count=count)))
                self.assertEqual(result.scope.view().model_dump(), {"source": "active_cohort", "name": "All cases", "caseCount": count})
                self.assertEqual(result.assumptions[0].text, "No cohort named: using the active cohort, All cases")

    def test_a_filtered_active_cohort_without_a_count_is_pending(self) -> None:
        result = compiled(compile_plan(j1_plan(), context(RACE_FILTER, name="Black patients", case_count=None)))
        self.assertEqual(result.scope.view().model_dump(), {"source": "active_cohort", "name": "Black patients", "caseCount": None})

    def test_active_cohort_with_an_age_range_compiles_to_two_clauses(self) -> None:
        result = compiled(compile_plan(j1_plan(), context(AGE_RANGE_FILTER, name="Age 50-69", case_count=400)))
        queries = [s for s in result.steps if s.tool == "cases_query"]
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].args["filter"], {"and": [{">=": {AGE: 50}}, {"<": {AGE: 70}}]})
        self.assertEqual(result.scope.view().model_dump(), {"source": "active_cohort", "name": "Age 50-69", "caseCount": 400})

    def test_unsupported_browser_fields_become_limitations_not_errors(self) -> None:
        cases: list[tuple[str, dict[str, dict[str, JsonValue]]]] = [
            ("UNKNOWN_FIELD", {"files.data_format": {"operator": "in", "field": "files.data_format", "operands": ["BAM"]}}),
            ("UNKNOWN_VALUE", {CGS_RISK: {"operator": "in", "field": CGS_RISK, "operands": ["Ultra risk"]}}),
            ("UNSUPPORTED_FIELD", {"gene": {"operator": "nested", "path": "gene", "operand": {}}}),
            ("UNSUPPORTED_FIELD", {AGE: {"operator": "and", "operands": [{"operator": "or", "operands": [_age(">=", 50), _age("<", 30)]}]}}),
        ]
        for code, root in cases:
            with self.subTest(code=code, root=root):
                stop = stopped(compile_plan(j1_plan(), context(root, name="Odd cohort")))
                self.assertEqual(stop.code, code)
                self.assertTrue(stop.limitations[0].text.startswith("Your active cohort, Odd cohort: "))
                self.assertIsNotNone(stop.limitations[0].unblock)


class UntestedTests(unittest.TestCase):
    without_kras: list[JsonValue] = [
        {"ref": "kras_mut", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "base"}},
        {"ref": "kras_wt", "def": {"kind": "difference", "a": "base", "b": "kras_mut"}},
    ]

    def test_without_mutation_is_rewritten_to_mutation_tested_minus_mutated(self) -> None:
        plan = plan_with(cohorts=self.without_kras, analyses=[{"kind": "cohort_size", "cohort": "kras_wt"}])
        cohorts, tested = apply_untested_rule(plan.cohorts)
        self.assertEqual(tested, ["base_tested"])
        by_ref = {c.ref: c.definition for c in cohorts}
        self.assertEqual(by_ref["base_tested"], ir.MutationTestedDef(kind="mutation_tested", within="base"))
        self.assertEqual(by_ref["kras_wt"], ir.DifferenceDef(kind="difference", a="base_tested", b="kras_mut"))

    def test_already_tested_difference_is_kept_but_still_counted(self) -> None:
        plan = ir.parse_planner_output(json.dumps({
            "kind": "plan",
            "cohorts": [
                {"ref": "tested", "def": {"kind": "mutation_tested", "within": "base"}},
                {"ref": "kras_mut", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "tested"}},
                {"ref": "kras_wt", "def": {"kind": "difference", "a": "tested", "b": "kras_mut"}},
            ],
            "analyses": [{"kind": "cohort_size", "cohort": "kras_wt"}],
        }))
        assert isinstance(plan, ir.Plan)
        cohorts, tested = apply_untested_rule(plan.cohorts)
        self.assertEqual((cohorts, tested), (plan.cohorts, ["tested"]))

    def test_rewrite_adds_no_step_and_states_the_excluded_cases_from_two_counts(self) -> None:
        plan = plan_with(cohorts=self.without_kras, analyses=[{"kind": "cohort_size", "cohort": "kras_wt"}])
        result = compiled(compile_plan(plan))
        self.assertIn("EXCLUDE_UNTESTED", [a.code for a in result.assumptions])
        tested = next(s for s in result.steps if s.tool == "cases_query")
        self.assertEqual(tested.args["filter"], {"and": [{"in": {"available_variation_data": ["ssm"]}}]})
        # TOOLS.md choice 5: no extra step; de.untested cites the scope count and the tested count.
        self.assertEqual([s.tool for s in result.steps],
                         ["fixture_fingerprint", "gene_cases", "cases_query", "check_subset", "set_operation", "set_operation"])
        subtract = next(s for s in result.steps if s.args.get("op") == "difference")
        self.assertEqual((subtract.args["a"], subtract.args["b"]), (tested.id, "s2"))
        self.assertEqual(result.cohorts[0].count_fact_id, "f.s1.cases")
        self.assertEqual(result.cohort("base_tested").count_fact_id, f"f.{tested.id}.count")

    def test_difference_without_a_gene_cohort_is_left_alone(self) -> None:
        cohorts = plan_with(cohorts=[
            {"ref": "f", "def": {"kind": "filter", "clauses": [{"field": "demographic.race", "op": "in", "values": ["asian"]}]}},
            {"ref": "d", "def": {"kind": "difference", "a": "base", "b": "f"}},
        ]).cohorts
        self.assertEqual(apply_untested_rule(cohorts), (cohorts, []))


class SignatureTests(unittest.TestCase):
    def test_both_phrasings_share_a_signature_and_a_new_scope_changes_it(self) -> None:
        open_ended = compiled(compile_plan(j1_plan()))
        tool_named = compiled(compile_plan(j1_plan(["correlation_plot", "gen3_analysis_api"])))
        self.assertEqual(open_ended.signature, tool_named.signature)
        self.assertRegex(open_ended.signature, r"^[0-9a-f]{64}$")
        rescoped = compiled(compile_plan(j1_plan(), context(RACE_FILTER, name="Black patients")))
        self.assertNotEqual(open_ended.signature, rescoped.signature)
        renamed = compiled(compile_plan(j1_plan(), context(name="Everyone", case_count=999)))
        self.assertEqual(open_ended.signature, renamed.signature, "labels and counts are not part of the signature")


def dig(value: JsonValue, *keys: str) -> JsonValue:
    for key in keys:
        assert isinstance(value, dict), f"expected an object at {key}"
        value = value[key]
    return value


class LiveVocabularyContractTests(unittest.TestCase):
    """Skipped unless the local stack answers at ORION_VLAB_ORIGIN (http://identity:8080 in the test container)."""

    origin = os.environ.get("ORION_VLAB_ORIGIN", "")

    def _graphql(self, query: str, variables: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
        if not self.origin:
            self.skipTest("ORION_VLAB_ORIGIN is not set")
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        request = urllib.request.Request(f"{self.origin}/guppy/graphql", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data: dict[str, JsonValue] = json.loads(response.read())["data"]
                return data
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            self.skipTest(f"stack unreachable: {error}")

    def _count(self, clauses: list[JsonValue]) -> int:
        data = self._graphql(
            "query($filter: JSON) { CaseCentric__aggregation { case_centric(filter: $filter, accessibility: accessible) { _totalCount } } }",
            {"filter": {"and": clauses}},
        )
        total = dig(data, "CaseCentric__aggregation", "case_centric", "_totalCount")
        assert isinstance(total, int)
        return total

    def test_guppy_histogram_returns_exactly_the_supplemented_cgs_values(self) -> None:
        data = self._graphql(
            "query { CaseCentric__aggregation { case_centric(accessibility: accessible) {"
            " cgs_risk_key_criteria { cgs_risk_category { histogram { key count } } } } } }"
        )
        buckets = dig(data, "CaseCentric__aggregation", "case_centric", "cgs_risk_key_criteria", "cgs_risk_category", "histogram")
        spec = vocabulary().field(CGS_RISK)
        assert spec is not None and isinstance(buckets, list)
        self.assertEqual({str(dig(b, "key")): dig(b, "count") for b in buckets}, {"High risk": 339, "Standard risk": 661})
        self.assertEqual(set(spec.values or ()), {"High risk", "Standard risk"})

    def test_unnormalized_value_silently_matches_nothing(self) -> None:
        self.assertEqual(self._count([{"in": {CGS_RISK: ["high risk"]}}]), 0)
        self.assertEqual(self._count([{"in": {CGS_RISK: ["High risk"]}}]), 339)

    def test_every_sandbox_case_is_mutation_tested_so_none_are_excluded(self) -> None:
        self.assertEqual(self._count([{"in": {"available_variation_data": ["ssm"]}}]), self._count([]))


if __name__ == "__main__":
    unittest.main()
