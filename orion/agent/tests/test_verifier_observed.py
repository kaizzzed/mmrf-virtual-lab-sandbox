"""V-COMP and V-LIM: code inserts the journey's missing Observed sentence and the missing
limitation line (A2 verifier table), checked against the contract fixtures' own wording."""

import unittest
from collections.abc import Callable

from orion_agent.interfaces import ObservedResult
from orion_agent.model.verifier import CodeVerifier, check_observed
from orion_agent.models import FactRecord, Limitation, Sentence
from support import vocabulary
from test_model_fixtures import (
    genes_plan,
    j1_facts,
    j1_observed,
    j1_plan,
    j3_plan,
    last_view,
    placeholder_text,
    view_facts,
    view_observed,
)

J3 = "j3-after-confirm.events.jsonl"
ONCO = "oncomatrix.events.jsonl"


def without(observed: ObservedResult, keep: Callable[[Sentence], bool]) -> ObservedResult:
    return observed.model_copy(update={"observed": [s for s in observed.observed if keep(s)]})


def without_codes(observed: ObservedResult, *codes: str) -> ObservedResult:
    return observed.model_copy(update={"limitations": [lim for lim in observed.limitations if lim.code not in codes]})


def fact(base: FactRecord, fact_id: str, value: int | float | str, label: str = "") -> FactRecord:
    return base.model_copy(update={"id": fact_id, "value": value, "label": label or base.label})


class VCompTest(unittest.TestCase):
    def test_complete_fixtures_need_nothing(self) -> None:
        for name, plan, facts, observed in (
            ("j1", j1_plan(), j1_facts(), j1_observed()),
            ("j3", j3_plan(), view_facts(last_view(J3)), view_observed(last_view(J3))),
            ("oncomatrix", genes_plan(), view_facts(last_view(ONCO)), view_observed(last_view(ONCO))),
        ):
            with self.subTest(name=name):
                findings, sentences, limitations = check_observed(facts, plan, observed)
                self.assertEqual((sentences, limitations), ([], []))
                self.assertTrue(findings)
                self.assertTrue(all(f.passed for f in findings), [f for f in findings if not f.passed])

    def test_j1_missing_log_rank_p_is_inserted(self) -> None:
        observed = without(j1_observed(), lambda s: placeholder_text(s) != "Log-rank p = {{f.s4.p}} (Gen3 Analysis).")
        _, sentences, _ = check_observed(j1_facts(), j1_plan(), observed)
        [inserted] = sentences
        self.assertEqual(placeholder_text(inserted), "Log-rank p = {{f.s4.p}} (Gen3 Analysis).")
        self.assertEqual([s.text for s in inserted.segments if s.fact_id], ["7.1e-60"])

    def test_j1_missing_group_counts_and_events_are_inserted(self) -> None:
        observed = without(j1_observed(), lambda s: not placeholder_text(s).startswith("High risk:"))
        _, sentences, _ = check_observed(j1_facts(), j1_plan(), observed)
        self.assertEqual([placeholder_text(s) for s in sentences], ["High risk: {{f.s2.count}} cases, {{f.s4.events.high}} events."])
        self.assertEqual("".join(s.text for s in sentences[0].segments), "High risk: 339 cases, 299 events.")

    def test_j3_missing_groups_method_and_table_are_inserted_as_the_fixture_writes_them(self) -> None:
        view = last_view(J3)
        wanted = [placeholder_text(s) for s in view.observed if placeholder_text(s).startswith(("Groups:", "Method:", "Top-ranked"))]
        observed = without(view_observed(view), lambda s: placeholder_text(s) not in wanted)
        _, sentences, _ = check_observed(view_facts(view), j3_plan(), observed)
        self.assertEqual([placeholder_text(s) for s in sentences], wanted)
        method = sentences[1]
        self.assertTrue(method.segments[0].text.startswith("Method: Wilcoxon rank-sum; genes ranked by adjusted p-value; fold change"))

    def test_j2_missing_cohort_size_and_ranking_are_inserted(self) -> None:
        view = last_view(ONCO)
        observed = view_observed(view).model_copy(update={"observed": []})
        _, sentences, _ = check_observed(view_facts(view), genes_plan(), observed)
        self.assertEqual([placeholder_text(s) for s in sentences], [placeholder_text(s) for s in view.observed])

    def test_an_insertion_fails_passed_but_asks_for_no_re_compose(self) -> None:
        from test_verifier import output

        verifier, view = CodeVerifier(), last_view(J3)
        observed = without(view_observed(view), lambda s: not placeholder_text(s).startswith("Method:"))
        result = verifier.verify(
            output(), view_facts(view), None, vocabulary(), plan=j3_plan(), observed=observed, attempt=1,
            allow_list=verifier.allow_list(vocabulary(), None, j3_plan()),
        )
        self.assertFalse(result.passed)
        self.assertFalse(result.retry)
        self.assertEqual(len(result.inserted_observed), 1)
        [finding] = [f for f in result.findings if f.rule == "V-COMP" and not f.passed]
        self.assertEqual((finding.part, finding.index), ("observed", None))


class VLimTest(unittest.TestCase):
    def test_survival_rounding_and_hazard_ratio_lines_are_inserted(self) -> None:
        observed = without_codes(j1_observed(), "VIEWER_P_ROUNDED", "STAT_UNAVAILABLE")
        _, _, limitations = check_observed(j1_facts(), j1_plan(), observed)
        self.assertEqual([lim.model_dump(mode="json") for lim in limitations], [lim.model_dump(mode="json") for lim in j1_observed().limitations])

    def test_no_rounding_line_without_a_viewer_card(self) -> None:
        observed = without_codes(j1_observed(), "VIEWER_P_ROUNDED").model_copy(update={"viz_specs": []})
        _, _, limitations = check_observed(j1_facts(), j1_plan(), observed)
        self.assertEqual(limitations, [])

    def test_panel_coverage_is_inserted_for_gene_defined_cohorts_and_rankings(self) -> None:
        for plan, view in ((j3_plan(), last_view(J3)), (genes_plan(), last_view(ONCO))):
            with self.subTest(view=view.question):
                observed = without_codes(view_observed(view), "PANEL_COVERAGE")
                _, _, limitations = check_observed(view_facts(view), plan, observed)
                self.assertEqual([lim.text for lim in limitations], [lim.text for lim in view.limitations if lim.code == "PANEL_COVERAGE"])

    def test_ties_at_the_cutoff_name_the_tied_genes_with_chips(self) -> None:
        facts = view_facts(last_view(J3))
        base = next(f for f in facts if f.id == "f.s10.top.10.gene")
        facts.append(fact(base, "f.s10.top.11.gene", "SPRY4", "Rank 10 gene"))
        _, _, limitations = check_observed(facts, j3_plan(), view_observed(last_view(J3)))
        [ties] = limitations
        self.assertEqual(ties.code, "TIES_AT_CUTOFF")
        self.assertEqual(ties.text, "SPRY4 tied with the last gene in the top 10, so the list is longer than 10.")
        self.assertEqual([s.fact_id for s in ties.segments or [] if s.fact_id], ["f.s10.top.11.gene"])

    def test_fewer_than_n_cites_the_ranking_count(self) -> None:
        facts = view_facts(last_view(J3))
        base = next(f for f in facts if f.id == "f.s9.tested")
        facts.append(fact(base, "f.s10.count", 7, "Ranked genes"))
        _, _, limitations = check_observed(facts, j3_plan(), view_observed(last_view(J3)))
        [fewer] = limitations
        self.assertEqual((fewer.code, fewer.text), ("FEWER_THAN_N", "Only 7 genes ranked, fewer than the 10 asked for."))
        self.assertEqual([s.fact_id for s in fewer.segments or [] if s.fact_id], ["f.s10.count"])

    def test_an_empty_ranking_says_so(self) -> None:
        facts = [f for f in view_facts(last_view(J3)) if not f.id.startswith("f.s10.top.")]
        base = next(f for f in facts if f.id == "f.s9.tested")
        facts.append(fact(base, "f.s10.count", 0, "Ranked genes"))
        _, _, limitations = check_observed(facts, j3_plan(), view_observed(last_view(J3)))
        self.assertEqual([lim.text for lim in limitations], ["No gene passed the thresholds, so there is no ranking."])

    def test_cases_outside_the_tested_cases_are_reported(self) -> None:
        facts = [fact(f, f.id, 3) if f.id == "f.s4.outside" else f for f in view_facts(last_view(J3))]
        _, _, limitations = check_observed(facts, j3_plan(), view_observed(last_view(J3)))
        [subset] = limitations
        self.assertEqual(subset.code, "NOT_SUBSET")
        self.assertEqual(subset.text, "3 KRAS-mutated cases are outside the mutation-tested cases; only the cases inside were used.")

    def test_a_zero_count_names_its_filter_and_both_causes(self) -> None:
        facts = [fact(f, f.id, 0) if f.id == "f.s2.count" else f for f in j1_facts()]
        _, _, limitations = check_observed(facts, j1_plan(), j1_observed())
        [empty] = limitations
        self.assertEqual(empty.code, "EMPTY_COHORT")
        self.assertTrue(empty.text.startswith("No case matched CGS risk category = High risk: either no case has these values"))
        self.assertEqual(empty.unblock, "Check the values on the plan, or ask about a broader cohort.")

    def test_a_plan_limitation_missing_from_limitations_is_inserted_as_is(self) -> None:
        line = Limitation(code="NAMED_TOOL_MISMATCH", text="OncoMatrix does not plot survival; Correlation Plot does.", unblock=None)
        plan = j1_plan().model_copy(update={"limitations": [line]})
        _, _, limitations = check_observed(j1_facts(), plan, j1_observed())
        self.assertEqual(limitations, [line])
        _, _, again = check_observed(j1_facts(), plan, j1_observed().model_copy(update={"limitations": [*j1_observed().limitations, line]}))
        self.assertEqual(again, [])


if __name__ == "__main__":
    unittest.main()
