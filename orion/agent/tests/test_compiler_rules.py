"""Compiler rules beyond Day 1: C-CASEIDS, C-SPLIT, C-SAVE, C-FIT, C-GUARD and C-DEFAULTS; scope
precedence and the two scopes of a follow-up (C-08, E14, E15); thread cohorts; plan rows and their
reasons (C-15, C-35); clarify and Unsupported (E10, E17, E18); compile-time stops (C-34); the call
budget and the 32 KB estimate; the save pause's pending payload."""

import unittest
from unittest import mock

from pydantic import JsonValue
from support import context, vocabulary
from test_compiler_cases import (
    CGS,
    ISS,
    J1,
    J2,
    J3,
    J3_RUN,
    MUT_IDS,
    RACE,
    RACE_ROOT,
    WT_IDS,
    case_id_filter_set,
    compile_ir,
    compiled,
    compiler,
    j3_ledger,
    plan,
    race_context,
    saved_j3_cohorts,
    stopped,
)

from orion_agent import compiler as compiler_module
from orion_agent import ir
from orion_agent.catalog import TOOL_REASONS
from orion_agent.compiler import (
    CompiledPlan,
    Compiler,
    CompileStop,
    build_confirm_pending,
    fact_context,
    find_reuse,
    largest_snapshot_bytes,
    step_chip,
)
from orion_agent.models import (
    SNAPSHOT_LIMIT_BYTES,
    CaseIdsDefinition,
    ClarifyPending,
    CohortContext,
    FilterSet,
    ScopeChoice,
    def_sha256,
)
from orion_agent.tools.schemas import FactContext, StepValueReader, is_step_ref, resolve_pointer

PROTEINPAINT_TOOLS = frozenset({"top_mutated_genes", "sample_lists", "de_preanalysis", "de_run"})


def _walk(value: JsonValue) -> list[JsonValue]:
    out = [value]
    if isinstance(value, dict):
        for item in value.values():
            out += _walk(item)
    elif isinstance(value, list):
        for item in value:
            out += _walk(item)
    return out


def ids(values: list[str]) -> list[JsonValue]:
    out: list[JsonValue] = list(values)
    return out


def reader(calls: dict[str, JsonValue]) -> StepValueReader:
    def read(step_id: str, pointer: str) -> JsonValue:
        return resolve_pointer(calls[step_id], pointer)

    return read


class CaseIdsTests(unittest.TestCase):
    """C-CASEIDS and C-23: ProteinPaint only ever sees case-ID lists, and a filtered scope is read once."""

    def test_proteinpaint_steps_carry_only_case_id_lists(self) -> None:
        contexts: list[CohortContext] = [context(), race_context()]
        for data in (J2, J3):
            for ctx in contexts:
                result = compiled(compile_ir(data, ctx))
                for step in (s for s in result.steps if s.tool in PROTEINPAINT_TOOLS):
                    with self.subTest(step=step.id, tool=step.tool, scope=result.scope.name):
                        for node in _walk(step.args):
                            if isinstance(node, dict) and "in" in node and isinstance(node["in"], dict):
                                self.assertEqual(set(node["in"]), {"case_id"})

    def test_the_browser_filter_reaches_exactly_one_step(self) -> None:
        for data in (J1, J2, J3):
            result = compiled(compile_ir(data, race_context()))
            with self.subTest(plan=result.paraphrase):
                naming = [s.id for s in result.steps if RACE in str(s.args) and "black or african american" in str(s.args)]
                # J2's own race cohort names the field too; the browser's filter is s2 alone.
                self.assertEqual(naming[0], "s2")
                self.assertEqual(result.steps[1].args["filter"], {"and": [{"in": {RACE: ["black or african american"]}}]})
                self.assertEqual(result.cohort("base").count_fact_id, "f.s2.count")

    def test_a_gene_cohort_within_the_tested_cases_is_intersected_where_used(self) -> None:
        result = compiled(compile_ir(J3))
        groups = result.step("s6").args["groups"]
        assert isinstance(groups, list) and isinstance(groups[1], dict)
        self.assertEqual(groups[1]["caseIds"], {"ref": "s2", "pointer": "/observed/caseIds", "intersect": "s3"})
        mut = result.cohort("kras_mut")
        assert mut.ids is not None
        self.assertEqual((mut.ids.ref, mut.ids.intersect, mut.definition), ("s2", "s3", None))


class SplitTests(unittest.TestCase):
    """C-SPLIT: a split field with a ProteinPaint twin is the chart's overlay; without one, no card."""

    def survival(self, field: str, values: list[JsonValue] | None = None) -> CompiledPlan:
        analysis: dict[str, JsonValue] = {"kind": "survival_compare", "split_term": field}
        if values is not None:
            analysis["split_values"] = values
        return compiled(compile_ir({"kind": "plan", "analyses": [analysis]}))

    def test_twins_become_the_overlay(self) -> None:
        for field, twin in ((CGS, "cgs_risk_category"), (RACE, "race"), ("demographic.gender", "gender")):
            with self.subTest(field=field):
                step = next(s for s in self.survival(field).steps if s.tool == "survival_compare")
                self.assertEqual(step.args["split"], {"field": field, "overlay": twin})

    def test_a_field_without_a_twin_has_no_split_and_offers_cohort_comparison(self) -> None:
        result = self.survival(ISS)
        step = next(s for s in result.steps if s.tool == "survival_compare")
        self.assertIsNone(step.args["split"])
        self.assertEqual([a.app_id for a in result.open_apps], ["CohortComparison"])
        self.assertEqual(result.rows[0].label, "Count the 3 ISS stage groups")

    def test_stated_values_narrow_the_groups_and_too_many_values_stop(self) -> None:
        narrowed = self.survival(ISS, ["stage i", "STAGE III"])
        self.assertEqual([s.input_summary for s in narrowed.steps if s.tool == "cohort_count"], ["ISS stage = Stage I", "ISS stage = Stage III"])
        stop = stopped(compile_ir({"kind": "plan", "analyses": [{"kind": "survival_compare", "split_term": "cgs_risk_key_criteria.other_criteria"}]}))
        self.assertEqual(stop.code, "UNSUPPORTED_FIELD")
        self.assertIn("two to six groups", stop.limitations[0].text)

    def test_a_survival_without_a_split_or_groups_stops(self) -> None:
        stop = stopped(compile_ir({"kind": "plan", "analyses": [{"kind": "survival_compare"}]}))
        self.assertEqual(stop.code, "AMBIGUOUS_REQUEST")


class SaveTests(unittest.TestCase):
    """C-SAVE: the cohorts a full tool needs are proposed for saving, once per plan, or reused."""

    def test_j2_saves_its_race_cohort_last(self) -> None:
        result = compiled(compile_ir(J2))
        self.assertEqual([s.tool for s in result.steps][-1], "save_cohorts")
        self.assertTrue(result.writes)
        save = result.steps[-1]
        self.assertEqual((save.args, save.depends_on, save.kind), ({"cohorts": [{"ref": "black", "name": "Black or African American (ORION)"}]}, ["s2"], "client"))
        self.assertEqual(result.cohort("black").save, "proposed")
        self.assertEqual(result.rows[-1].label, "Save 1 cohort")

    def test_j3_saves_both_groups_after_eligibility_and_before_de(self) -> None:
        result = compiled(compile_ir(J3))
        tools = [s.tool for s in result.steps]
        self.assertLess(tools.index("de_preanalysis"), tools.index("save_cohorts"))
        self.assertLess(tools.index("save_cohorts"), tools.index("de_run"))
        self.assertEqual(result.step("s9").depends_on, ["s6", "s8"])
        self.assertEqual(result.step("s10").depends_on, ["s8", "s9"])
        self.assertIn("s7", result.step("s8").depends_on, "an ineligible group stops the run before the save")
        self.assertEqual([c.save for c in result.cohorts], ["none", "none", "proposed", "proposed"])

    def test_an_identical_saved_cohort_is_reused_and_nothing_is_written(self) -> None:
        # The platform stores the value's own spelling, and the browser hashes stored filters as they are.
        stored: JsonValue = {"mode": "and", "root": {RACE: {"operator": "in", "field": RACE, "operands": ["black or african american"]}}}
        saved: list[dict[str, JsonValue]] = [{"id": "c-black", "name": "Black patients", "caseCount": 334, "filters": stored}]
        result = compiled(compile_ir(J2, context(saved=saved)))
        self.assertNotIn("save_cohorts", [s.tool for s in result.steps])
        self.assertFalse(result.writes)
        black = result.cohort("black")
        self.assertEqual((black.save, black.cohort_id, black.saved_name, black.name), ("reused", "c-black", "Black patients", "Black patients"))
        self.assertNotIn("cohortName", [e.field for e in result.editable])

    def test_the_scope_is_never_saved_by_the_plan(self) -> None:
        result = compiled(compile_ir({"kind": "plan", "analyses": [{"kind": "top_mutated_genes", "cohort": "base", "n": 5}]}))
        self.assertFalse(result.writes)
        self.assertEqual([c.ref for c in result.cohorts], ["base"])

    def test_a_name_the_question_states_is_the_proposed_name(self) -> None:
        named = plan(J2, cohorts=[{"ref": "black", "name": "My race cohort", "def": {"kind": "filter", "clauses": [{"field": RACE, "op": "in", "values": ["black or african american"]}]}}])
        result = compiled(compile_ir(named))
        self.assertEqual(result.steps[-1].args, {"cohorts": [{"ref": "black", "name": "My race cohort"}]})

    def test_the_pending_payload_for_a_filter_cohort(self) -> None:
        result = compiled(compile_ir(J2))
        calls: dict[str, JsonValue] = {"s2": {"observed": {"count": 334, "caseIds": ["SYNTH-CASE-0001"], "pages": 1, "complete": True}}}
        pending = build_confirm_pending(result, nonce="0" * 32, read=reader(calls), context=context())
        cohort = pending.cohorts[0]
        self.assertEqual(cohort.definition, result.cohort("black").definition)
        self.assertEqual((cohort.case_count, cohort.case_count_fact_id, cohort.eligible_count, cohort.reuse), (334, "f.s2.count", None, None))
        self.assertEqual((pending.step_id, pending.plan_signature, pending.plan_version), ("s5", result.signature, 1))

    def test_the_pending_reuses_from_the_cohort_list_or_the_ledger(self) -> None:
        result = compiled(compile_ir(J3))
        calls: dict[str, JsonValue] = {
            "s2": {"observed": {"count": 480, "caseIds": ids(MUT_IDS)}},
            "s3": {"observed": {"count": 1000, "caseIds": ids(sorted(MUT_IDS + WT_IDS))}},
            "s5": {"observed": {"count": 520, "caseIds": ids(WT_IDS)}},
            "s7": {"observed": {"groups": [{"key": "control", "eligible": 500}, {"key": "case", "eligible": 470}]}},
        }
        listed = build_confirm_pending(result, nonce="0" * 32, read=reader(calls), context=context(saved=saved_j3_cohorts()))
        self.assertEqual([(c.ref, c.reuse.cohort_id if c.reuse else None) for c in listed.cohorts], [("kras_wt", "c-wt"), ("kras_mut", "c-mut")])
        self.assertEqual([(c.eligible_count, c.eligible_count_fact_id) for c in listed.cohorts], [(500, "f.s7.eligible.control"), (470, "f.s7.eligible.case")])
        # The cohort list still holds the ledger's saved cohort, but its filters cannot be read: the ledger's hash matches.
        unreadable: dict[str, JsonValue] = {"mode": "or", "root": {"a": {"operator": "in", "field": "a", "operands": ["x"]},
                                                                    "b": {"operator": "in", "field": "b", "operands": ["y"]}}}
        ctx = context(saved=[{"id": "c-mut", "name": "Renamed", "filters": unreadable, "caseCount": 480}])
        via_ledger = build_confirm_pending(result, nonce="0" * 32, read=reader(calls), context=ctx, ledger=j3_ledger())
        reuse = via_ledger.cohorts[1].reuse
        self.assertEqual((reuse.cohort_id, reuse.name) if reuse else None, ("c-mut", "Renamed"))
        self.assertIsNone(via_ledger.cohorts[0].reuse, "c-wt is no longer in the cohort list")

    def test_cases_dropped_by_the_intersection_have_no_count_fact(self) -> None:
        result = compiled(compile_ir(J3))
        outside = "SYNTH-CASE-9999"
        calls: dict[str, JsonValue] = {
            "s2": {"observed": {"count": 481, "caseIds": ids(MUT_IDS + [outside])}},
            "s3": {"observed": {"count": 1000, "caseIds": ids(sorted(MUT_IDS + WT_IDS))}},
            "s5": {"observed": {"count": 520, "caseIds": ids(WT_IDS)}},
            "s7": {"observed": {"groups": [{"key": "control", "eligible": 500}, {"key": "case", "eligible": 470}]}},
        }
        pending = build_confirm_pending(result, nonce="0" * 32, read=reader(calls), context=context())
        mut = pending.cohorts[1]
        assert isinstance(mut.definition, CaseIdsDefinition)
        self.assertEqual((mut.case_count, mut.case_count_fact_id, outside in mut.definition.case_ids), (480, None, False))
        self.assertEqual(mut.def_sha256, def_sha256(CaseIdsDefinition(kind="case_ids", case_ids=MUT_IDS)))

    def test_find_reuse_ignores_unreadable_saved_cohorts(self) -> None:
        sha = def_sha256(CaseIdsDefinition(kind="case_ids", case_ids=MUT_IDS))
        self.assertIsNone(find_reuse(sha, context()))
        hit = find_reuse(sha, context(saved=[{"id": "x", "name": "Mut", "filters": case_id_filter_set(MUT_IDS), "caseCount": 480}]))
        self.assertEqual((hit.cohort_id, hit.name) if hit else None, ("x", "Mut"))


class FitTests(unittest.TestCase):
    """C-FIT: named tools against the capability matrix; named tools never change the signature."""

    def test_a_tool_named_for_a_job_outside_its_row_waits_for_consent(self) -> None:
        result = compiled(compile_ir(plan(J1, named_tools=[{"tool": "oncomatrix"}])))
        consent = result.consent
        assert consent is not None
        self.assertEqual((consent.named.app_id, consent.proposed.app_id), ("OncoMatrix", "Correlation"))
        self.assertEqual([lim.code for lim in result.limitations], ["NAMED_TOOL_MISMATCH"])
        self.assertEqual(result.signature, compiled(compile_ir(J1)).signature)

    def test_other_substitutions_propose_the_analysis_app(self) -> None:
        de_for_survival = compiled(compile_ir(plan(J1, named_tools=[{"tool": "differential_expression"}])))
        assert de_for_survival.consent is not None
        self.assertEqual(de_for_survival.consent.reason,
                         "Differential Gene Expression runs differential expression between two groups; Correlation Plot runs the Kaplan–Meier survival comparison you asked for.")
        browser_for_genes = compiled(compile_ir(plan(J2, named_tools=[{"tool": "genome_browser"}])))
        assert browser_for_genes.consent is not None
        self.assertEqual((browser_for_genes.consent.named.app_id, browser_for_genes.consent.proposed.app_id), ("GB", "OncoMatrix"))
        self.assertTrue(browser_for_genes.paraphrase.endswith(", in OncoMatrix instead of Genome Browser."))
        self.assertTrue(browser_for_genes.rows[1].reason.endswith("; Genome Browser, which you named, shows genomic regions"))

    def test_fitting_tools_need_no_consent(self) -> None:
        cases: list[tuple[dict[str, JsonValue], str]] = [
            (J1, "correlation_plot"), (J1, "cohort_builder"), (J1, "gen3_analysis_api"), (J2, "cohort_builder"), (J2, "oncomatrix"),
            (J3, "gen3_analysis_api"), (J3, "differential_expression"),
            ({"kind": "plan", "analyses": [{"kind": "top_mutated_genes", "cohort": "base"}]}, "gen3_analysis_api"),
        ]
        for data, tool in cases:
            with self.subTest(tool=tool):
                result = compiled(compile_ir(plan(data, named_tools=[{"tool": tool}])))
                self.assertIsNone(result.consent)
                self.assertEqual(result.limitations, [])
                self.assertEqual(result.signature, compiled(compile_ir(data)).signature)

    def test_fits_with_hand_off_names_the_app_and_offers_it(self) -> None:
        genes = compiled(compile_ir(plan(J2, named_tools=[{"tool": "mutation_frequency"}])))
        self.assertIsNone(genes.consent)
        self.assertEqual(genes.rows[1].reason, "OncoMatrix ranks mutated genes; Mutation Frequency, which you named, opens on this cohort from the answer")
        self.assertEqual([a.app_id for a in genes.open_apps], ["MutationFrequency"])
        survival = compiled(compile_ir(plan(J1, named_tools=[{"tool": "cohort_comparison"}])))
        self.assertIsNone(survival.consent)
        self.assertIn("Cohort Comparison, which you named", survival.rows[1].reason)
        self.assertEqual([a.app_id for a in survival.open_apps], ["CohortComparison"])


class GuardTests(unittest.TestCase):
    """C-GUARD at compile time: the subset check before every difference, the counts the survival
    pre-guard reads, the 10,000-id lists, eligibility before the save."""

    def test_every_difference_follows_a_subset_check_on_its_row(self) -> None:
        de: JsonValue = {"kind": "differential_expression", "group1": "kras_wt", "group2": "kras_mut", "top_n": 10}
        result = compiled(compile_ir(plan(J3, analyses=[de, {"kind": "cohort_size", "cohort": "kras_wt"}])))
        for i, step in enumerate(result.steps):
            if step.tool == "set_operation" and step.args.get("op") == "difference":
                check = result.steps[i - 1]
                self.assertEqual((check.tool, check.row), ("check_subset", step.row))
                self.assertEqual((check.args["inner"], check.args["outer"]), (step.args["b"], step.args["a"]))

    def test_survival_depends_on_every_group_count_and_lists_cap_at_ten_thousand(self) -> None:
        result = compiled(compile_ir(J1))
        survival = result.step("s4")
        self.assertEqual(survival.depends_on, [s.id for s in result.steps if s.tool == "cohort_count"])
        for step in compiled(compile_ir(J3, race_context())).steps:
            if step.tool == "cases_query":
                self.assertEqual((step.args["pageSize"], step.args["maxIds"]), (1000, 10000))


class DefaultsTests(unittest.TestCase):
    """C-DEFAULTS: every default is recorded as an assumption; a stated value is not."""

    def test_j3_defaults_are_assumptions_in_the_fixtures_order(self) -> None:
        result = compiled(compile_ir(J3))
        self.assertEqual([a.code for a in result.assumptions],
                         ["DEFAULT_SCOPE_ACTIVE", "EXCLUDE_UNTESTED", "DE_METHOD_WILCOXON", "RANK_ADJ_P", "DE_THRESHOLDS_VIEWER", "CONTROL_FIRST"])
        self.assertEqual(result.step("s9").args["thresholds"], {"pValueCutoff": 0.05, "pValueType": "adjusted", "foldChangeCutoff": 0.3})

    def test_stated_settings_are_not_assumptions(self) -> None:
        analysis: dict[str, JsonValue] = {
            "kind": "differential_expression", "group1": "kras_wt", "group2": "kras_mut", "method": "edgeR", "rank_by": "fold_change",
            "top_n": 25, "thresholds": {"p_value_cutoff": 0.01, "p_value_type": "original", "fold_change_cutoff": 1.0},
        }
        result = compiled(compile_ir(plan(J3, analyses=[analysis])))
        self.assertEqual([a.code for a in result.assumptions], ["DEFAULT_SCOPE_ACTIVE", "EXCLUDE_UNTESTED", "CONTROL_FIRST"])
        de, rank = result.step("s9"), result.step("s10")
        thresholds = {"pValueCutoff": 0.01, "pValueType": "original", "foldChangeCutoff": 1.0}
        self.assertEqual((de.args["method"], de.args["thresholds"]), ("edgeR", thresholds))
        self.assertEqual((rank.args["rankBy"], rank.args["ascending"], rank.args["n"], rank.args["thresholds"]), ("abs(fold_change)", False, 25, thresholds))
        self.assertEqual(result.rows[5].label, "Run DE: edgeR, rank by absolute log2FC")
        self.assertEqual(de.input_summary, "edgeR, KRAS not detected (ORION) vs. KRAS mutated (ORION)")

    def test_top_n_defaults_are_ten_for_de_and_three_for_genes(self) -> None:
        de = compiled(compile_ir(plan(J3, analyses=[{"kind": "differential_expression", "group1": "kras_wt", "group2": "kras_mut"}])))
        self.assertEqual((de.step("s10").args["n"], [a.text for a in de.assumptions if a.code == "TOP_N_DEFAULT"]), (10, ["Top 10 genes: the question names no number"]))
        genes = compiled(compile_ir({"kind": "plan", "analyses": [{"kind": "top_mutated_genes", "cohort": "base"}]}))
        self.assertEqual((genes.steps[1].args["geneCount"], genes.steps[2].args["n"]), (3, 3))
        self.assertIn("TOP_N_DEFAULT", [a.code for a in genes.assumptions])

    def test_group1_is_the_control(self) -> None:
        swapped = plan(J3, analyses=[{"kind": "differential_expression", "group1": "kras_mut", "group2": "kras_wt", "top_n": 10}])
        result = compiled(compile_ir(swapped))
        groups = result.step("s6").args["groups"]
        assert isinstance(groups, list) and isinstance(groups[0], dict)
        self.assertEqual(groups[0]["caseIds"], {"ref": "s2", "pointer": "/observed/caseIds", "intersect": "s3"})
        self.assertEqual(result.assumptions[-1].text, "KRAS mutated (ORION) is the control group: fold change is log2(KRAS not detected (ORION) / KRAS mutated (ORION))")


class RowTests(unittest.TestCase):
    """C-35 and C-15: rows name their tools with the matrix's reason; every step sits on one row."""

    def test_rows_name_their_tool_and_reason_and_hold_every_step_once(self) -> None:
        for data in (J1, J2, J3):
            result = compiled(compile_ir(data, race_context()))
            with self.subTest(plan=result.paraphrase):
                self.assertEqual([sid for row in result.rows for sid in row.step_ids], [s.id for s in result.steps])
                for row in result.rows:
                    self.assertEqual(row.reason, TOOL_REASONS[row.tool])
                    self.assertIn(row.tool, [result.step(sid).tool for sid in row.step_ids])
                self.assertEqual((result.steps[0].tool, result.steps[0].folded, result.steps[0].row), ("fixture_fingerprint", True, 1))

    def test_chips_name_the_row_and_the_step(self) -> None:
        result = compiled(compile_ir(J3))
        self.assertEqual(step_chip(result, "s9"), "Row 6 · s9 de_run")
        self.assertEqual(step_chip(result, "s1"), "Row 1 · s1 fixture_fingerprint")


class ScopeTests(unittest.TestCase):
    """C-08, FR-09: a named cohort, then a follow-up's thread scope, then the default; E14 and E15."""

    def test_j1_on_the_active_race_cohort_counts_within_it(self) -> None:
        result = compiled(compile_ir(J1, race_context()))
        self.assertEqual(result.scope.view().model_dump(), {"source": "active_cohort", "name": "Black or African American", "caseCount": 334})
        self.assertEqual(result.cohort("base").count_fact_id, "f.s2.count")
        for step in result.steps[2:4]:
            self.assertEqual(step.depends_on, ["s2"])

    def test_a_follow_up_after_an_active_cohort_switch_names_both_scopes(self) -> None:
        follow_up = plan(J3, follows=J3_RUN)
        result = compiled(compile_ir(follow_up, race_context(), j3_ledger()))
        self.assertEqual((result.scope.source, result.scope.name), ("thread_cohort", "All cases"))
        assert result.alt_scope is not None
        self.assertEqual((result.alt_scope.source, result.alt_scope.name), ("active_cohort", "Black or African American"))
        self.assertEqual(result.alt_scope_choice, ScopeChoice(source="active_cohort"))
        view = result.view()
        self.assertEqual(view.alt_scope.model_dump() if view.alt_scope else None, {"source": "active_cohort", "name": "Black or African American", "caseCount": 334})
        self.assertEqual(result.ledger.follows if result.ledger else None, J3_RUN)

    def test_no_second_scope_when_the_active_cohort_did_not_change(self) -> None:
        result = compiled(compile_ir(plan(J3, follows=J3_RUN), context(), j3_ledger()))
        self.assertEqual(result.scope.source, "thread_cohort")
        self.assertIsNone(result.alt_scope)
        self.assertIsNone(result.alt_scope_choice)

    def test_a_follow_up_on_the_active_cohort_offers_the_thread_scope_back(self) -> None:
        result = compiled(compile_ir(plan(J3, follows=J3_RUN, base_cohort={"source": "active_cohort"}), race_context(), j3_ledger()))
        self.assertEqual(result.scope.source, "active_cohort")
        assert result.alt_scope is not None
        self.assertEqual((result.alt_scope.source, result.alt_scope_choice), ("thread_cohort", ScopeChoice(source="thread_cohort")))


class ThreadCohortTests(unittest.TestCase):
    """Thread cohorts rebuild from the session ledger: E15's "Show the top 5 instead"."""

    def top5(self) -> ir.Plan:
        return plan(
            {"kind": "plan", "analyses": [{"kind": "differential_expression", "group1": "kras_wt", "group2": "kras_mut", "top_n": 5}]},
            follows=J3_RUN,
            cohorts=[{"ref": "kras_wt", "def": {"kind": "thread", "ledger_ref": "kras_wt"}},
                     {"ref": "kras_mut", "def": {"kind": "thread", "ledger_ref": "kras_mut"}}],
        )

    def test_saved_thread_cohorts_rebuild_from_their_case_ids_and_are_reused(self) -> None:
        result = compiled(compile_ir(self.top5(), race_context(saved_j3_cohorts()), j3_ledger()))
        self.assertEqual([s.tool for s in result.steps],
                         ["fixture_fingerprint", "cases_query", "cases_query", "sample_lists", "de_preanalysis", "de_run", "rank_top_n"])
        self.assertEqual(result.step("s2").args["filter"], {"and": [{"in": {"case_id": WT_IDS}}]})
        self.assertFalse(result.writes)
        self.assertEqual([(c.ref, c.save, c.cohort_id) for c in result.cohorts[1:]], [("kras_wt", "reused", "c-wt"), ("kras_mut", "reused", "c-mut")])
        groups = result.step("s6").args["groups"]
        assert isinstance(groups, list)
        self.assertEqual([g["name"] for g in groups if isinstance(g, dict)], ["KRAS not detected (ORION)", "KRAS mutated (ORION)"])
        self.assertEqual(result.step("s7").args["n"], 5)
        assert result.ledger is not None and result.alt_scope is not None
        self.assertEqual([(r.ref, r.run_id) for r in result.ledger.refs], [("kras_wt", J3_RUN), ("kras_mut", J3_RUN)])
        self.assertEqual(result.cohort("kras_wt").definition_text, "Mutation-tested cases without a KRAS mutation, as a case-ID list")

    def test_unsaved_thread_cohorts_are_proposed_again(self) -> None:
        result = compiled(compile_ir(self.top5(), context(), j3_ledger(saved=False)))
        self.assertTrue(result.writes)
        self.assertEqual([c.save for c in result.cohorts[1:]], ["proposed", "proposed"])

    def test_a_thread_cohort_missing_from_the_ledger_stops(self) -> None:
        stop = stopped(compile_ir(self.top5(), context(), None))
        self.assertEqual((stop.code, stop.limitations[0].code), ("UNKNOWN_VALUE", "UNKNOWN_VALUE"))


class ClarifyTests(unittest.TestCase):
    """E10, FR-04: one question from a code template, each option a field with a value preview."""

    def clarify(self, slot: str, options: list[str], question: str = "q", ctx: CohortContext | None = None) -> ClarifyPending | CompileStop:
        output = ir.Clarify.model_validate({"kind": "clarify", "slot": slot, "option_ids": options})
        return compiler().clarify(output, question, ctx)

    def test_unknown_options_are_dropped_and_one_left_stops(self) -> None:
        pending = self.clarify("split_term", [CGS, "made.up", ISS, "diagnoses.age_at_diagnosis"], "Compare survival by risk")
        assert isinstance(pending, ClarifyPending)
        self.assertEqual([o.option_id for o in pending.options], [CGS, ISS])
        self.assertEqual(pending.question, "Which field should split the groups?")
        stop = self.clarify("split_term", [CGS, "made.up"])
        assert isinstance(stop, CompileStop)
        self.assertEqual(stop.code, "AMBIGUOUS_REQUEST")

    def test_gene_and_scope_slots(self) -> None:
        genes = self.clarify("gene", ["kras", "IDH1", "TP53"])
        assert isinstance(genes, ClarifyPending)
        self.assertEqual([(o.option_id, o.field) for o in genes.options], [("KRAS", None), ("TP53", None)])
        ctx = context(RACE_ROOT, name="Black patients", saved=[{"id": "c1", "name": "Stage III", "caseCount": 3,
                                                                "filters": {"mode": "and", "root": {ISS: {"operator": "in", "field": ISS, "operands": ["Stage III"]}}}}])
        scopes = self.clarify("scope", ["all_cases", "active_cohort", "c1", "nope"], ctx=ctx)
        assert isinstance(scopes, ClarifyPending)
        self.assertEqual([(o.option_id, o.label, o.preview) for o in scopes.options],
                         [("all_cases", "All cases", "No filters"), ("active_cohort", "Active cohort: Black patients", "Race = Black or African American"),
                          ("c1", "Stage III", "ISS stage = Stage III")])
        self.assertEqual(scopes.question, "Which cohort should the analysis use?")


class UnsupportedTests(unittest.TestCase):
    """E17, E18: the Unsupported card names the nearest app; clinical advice names none."""

    def test_venn_names_set_operations(self) -> None:
        stop = compiler().unsupported(ir.Unsupported(kind="unsupported", reason="UNSUPPORTED_ANALYSIS", nearest_app_ids=["SetOperations", "Projects"]))
        self.assertEqual((stop.status, stop.code), ("Unsupported", "UNSUPPORTED_ANALYSIS"))
        self.assertEqual([(lim.unblock, [a.app_id for a in lim.nearest_apps]) for lim in stop.limitations], [(None, ["SetOperations"])])

    def test_clinical_advice_names_no_app(self) -> None:
        stop = compiler().unsupported(ir.Unsupported(kind="unsupported", reason="CLINICAL_ADVICE", nearest_app_ids=["DE"]))
        self.assertEqual([lim.nearest_apps for lim in stop.limitations], [[]])
        self.assertEqual(stop.limitations[0].text, "ORION gives no clinical or treatment advice.")


class CompileStopTests(unittest.TestCase):
    """C-34: every compile-time stop makes no data call: a CompileStop holds no step."""

    def test_stops_carry_codes_limitations_and_no_steps(self) -> None:
        self.assertNotIn("steps", CompileStop.model_fields)
        stops: list[tuple[ir.Plan, CohortContext]] = [
            (plan(J3, cohorts=[{"ref": "m", "def": {"kind": "gene_mutated", "gene": "OTHER", "within": "base"}}],
                  analyses=[{"kind": "cohort_size", "cohort": "m"}]), context()),
            (plan({"kind": "plan", "analyses": [{"kind": "survival_compare", "split_term": "nope.field"}]}), context()),
            (plan(J1), context({"files.data_format": {"operator": "in", "field": "files.data_format", "operands": ["BAM"]}})),
            (plan(J1, base_cohort={"source": "saved_cohort", "cohort_name": "Missing"}), context()),
        ]
        for output, ctx in stops:
            with self.subTest(plan=output.model_dump_json()[:80]):
                stop = stopped(compile_ir(output, ctx))
                self.assertTrue(stop.limitations)
                self.assertEqual(stop.limitations[0].code, stop.code)
                self.assertEqual(stop.status, "Limited")


class BudgetTests(unittest.TestCase):
    """CALL_BUDGET and REQUEST_TOO_LARGE stop a plan at compile time, with no data call."""

    def test_a_plan_over_twenty_calls_stops(self) -> None:
        field = "diagnoses.best_overall_response"  # six values: 1 + 6 counts + survival + median per analysis
        analyses: list[JsonValue] = [{"kind": "survival_compare", "split_term": field}] * 3
        stop = stopped(compile_ir({"kind": "plan", "analyses": analyses}))
        self.assertEqual(stop.code, "CALL_BUDGET")
        self.assertIn("25 tool calls, more than the 20", stop.limitations[0].text)
        self.assertEqual(len(compiled(compile_ir({"kind": "plan", "analyses": analyses[:2]})).steps), 17)

    def test_a_plan_whose_largest_view_overflows_the_snapshot_stops(self) -> None:
        # Between J1's largest view (about 19 KB) and J3's (about 30 KB).
        limited = Compiler(vocabulary(), "active_cohort", snapshot_limit_bytes=25_000)
        stop = stopped(compile_ir(J3, using=limited))
        self.assertEqual(stop.code, "CALL_BUDGET")
        self.assertEqual(stop.limitations[0].text, "This plan would produce more results than one answer can show, so nothing was run.")
        self.assertIsInstance(compile_ir(J1, using=limited), CompiledPlan)

    def test_the_largest_allowed_j3_fits_32_kb(self) -> None:
        widest = plan(J3, analyses=[{"kind": "differential_expression", "group1": "kras_wt", "group2": "kras_mut", "top_n": 100}])
        for ctx in (context(), race_context()):
            size = largest_snapshot_bytes(compiled(compile_ir(widest, ctx)))
            assert size is not None
            self.assertLessEqual(size, SNAPSHOT_LIMIT_BYTES)

    def test_a_request_over_the_gateway_limit_stops(self) -> None:
        with mock.patch.object(compiler_module, "MAX_SAMPLE_LIST_BODY_BYTES", 80):
            stop = stopped(compile_ir(J1))
        self.assertEqual((stop.code, stop.limitations[0].unblock), ("REQUEST_TOO_LARGE", "Narrow the cohorts."))


class FactContextTests(unittest.TestCase):
    """What the extractor labels facts with, from the plan (schemas.FactContext)."""

    def test_j3_and_j1_contexts(self) -> None:
        j3 = compiled(compile_ir(J3))
        self.assertEqual(fact_context(j3, "s3"), FactContext(cohort="Mutation-tested cases"))
        self.assertEqual(fact_context(j3, "s5"), FactContext(cohort="KRAS not detected (ORION)"))
        self.assertEqual(fact_context(j3, "s4"), FactContext(inner="KRAS-mutated cases"))
        self.assertEqual(fact_context(j3, "s6"), FactContext(group_labels=("KRAS not detected", "KRAS mutated")))
        self.assertEqual(fact_context(j3, "s7"), FactContext(group_labels=("KRAS not detected", "KRAS mutated")))
        self.assertEqual(fact_context(j3, "s10"), FactContext(rank_source="de_run", rank_by="adjusted_p_value"))
        j1 = compiled(compile_ir(J1))
        self.assertEqual([fact_context(j1, s) for s in ("s2", "s3")], [FactContext(cohort="High risk"), FactContext(cohort="Standard risk")])
        size = compiled(compile_ir({"kind": "plan", "analyses": [{"kind": "cohort_size", "cohort": "base"}]}))
        self.assertEqual(fact_context(size, "s2"), FactContext(cohort="All cases"))
        groups = j3.step("s7").args["groups"]
        assert isinstance(groups, list) and isinstance(groups[0], dict)
        self.assertTrue(is_step_ref(groups[0]["sampleIds"]))


class FilterSetReadTests(unittest.TestCase):
    def test_case_id_filter_sets_read_back_to_the_definition(self) -> None:
        from orion_agent.models import definition_from_filter_set

        definition = definition_from_filter_set(FilterSet.model_validate(case_id_filter_set(MUT_IDS)))
        self.assertEqual(definition, CaseIdsDefinition(kind="case_ids", case_ids=MUT_IDS))


if __name__ == "__main__":
    unittest.main()
