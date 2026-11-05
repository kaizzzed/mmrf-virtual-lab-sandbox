"""The verifier's model-text rules, one fixture per rule (A2: placeholders for TP53 and DIS3, the
allow-listed log2FC and one causal sentence among them), and the Interpretation-present gate."""

import unittest
from collections.abc import Sequence

from orion_agent.compiler import LedgerCohort, LedgerRun, ResolvedScope, SessionLedger
from orion_agent.interfaces import ComposerFollowUp, ComposerHypothesis, ComposerOutput, ComposerStatement, VerifierResult
from orion_agent.model.verifier import CodeVerifier, model_text_feedback
from orion_agent.models import Sentence
from support import vocabulary
from test_model_fixtures import (
    fixture_model_output,
    genes_plan,
    j1_facts,
    j1_observed,
    j1_plan,
    j3_plan,
    last_view,
    view_facts,
    view_observed,
)

J3 = "j3-after-confirm.events.jsonl"
TP53, DIS3 = "f.s10.top.6.gene", "f.s10.top.8.gene"
LEDGER = SessionLedger(
    runs=[
        LedgerRun(
            run_id="a" * 32,
            scope=ResolvedScope(source="all_cases", name="All cases", case_count=1000, clauses=[]),
            cohorts=[LedgerCohort(ref="nras_mut", name="NRAS mutated (ORION)", clauses=[])],
        )
    ]
)


def statement(text: str, *cites: str) -> ComposerStatement:
    return ComposerStatement(text=text, cites=list(cites) or ["f.s10.top.1.gene"])


def output(
    interpretation: Sequence[ComposerStatement] = (),
    hypotheses: Sequence[ComposerHypothesis] = (),
    follow_ups: Sequence[ComposerFollowUp] = (),
    apps: Sequence[str] = (),
) -> ComposerOutput:
    return ComposerOutput.model_validate(
        {
            "interpretation": [s.model_dump() for s in interpretation] or [statement("{{f.s10.top.1.gene}} ranks first here.").model_dump()],
            "hypotheses": [h.model_dump() for h in hypotheses],
            "follow_ups": [f.model_dump() for f in follow_ups],
            "open_app_ids": list(apps),
        }
    )


def verify_j3(out: ComposerOutput, *, attempt: int = 1, ledger: SessionLedger | None = None) -> VerifierResult:
    verifier, view = CodeVerifier(), last_view(J3)
    plan = j3_plan()
    allow = verifier.allow_list(vocabulary(), ledger, plan)
    return verifier.verify(
        out, view_facts(view), ledger, vocabulary(), plan=plan, observed=view_observed(view), attempt=attempt, allow_list=allow
    )


def dropped(result: VerifierResult, part: str = "interpretation") -> list[str | None]:
    sentences: list[Sentence] = getattr(result, part)
    return [s.dropped for s in sentences]


class VNumTest(unittest.TestCase):
    def test_gene_placeholders_for_tp53_and_dis3_pass(self) -> None:
        result = verify_j3(output([statement("{{%s}} and {{%s}} are both among the top-ranked genes." % (TP53, DIS3), TP53, DIS3)]))
        self.assertEqual(dropped(result), [None])
        texts = [s.text for s in result.interpretation[0].segments]
        self.assertEqual(texts, ["TP53", " and ", "DIS3", " are both among the top-ranked genes."])
        self.assertEqual([s.fact_id for s in result.interpretation[0].segments if s.fact_id], [TP53, DIS3])
        self.assertEqual(result.interpretation[0].cites, [TP53, DIS3])

    def test_a_gene_with_a_digit_outside_a_placeholder_fails(self) -> None:
        result = verify_j3(output([statement("TP53 is among the top-ranked genes.", TP53)]))
        self.assertEqual(dropped(result), ["V-NUM"])

    def test_a_gene_symbol_without_a_digit_outside_a_placeholder_fails(self) -> None:
        result = verify_j3(output([statement("BRAF ranks first between the groups.", "f.s10.top.1.gene")]))
        self.assertEqual(dropped(result), ["V-NUM"])

    def test_the_allow_listed_log2fc_passes(self) -> None:
        result = verify_j3(output([statement("Its log2FC is {{f.s10.top.1.log2fc}} in Gen3 Analysis terms.", "f.s10.top.1.log2fc")]))
        self.assertEqual(dropped(result), [None])

    def test_digits_number_words_and_numerals_fail(self) -> None:
        for text in ("The top 10 genes differ.", "The two groups differ.", "Half of the genes rise.", "Group III differs."):
            with self.subTest(text=text):
                self.assertEqual(dropped(verify_j3(output([statement(text)]))), ["V-NUM"])

    def test_a_formatted_value_is_never_written_by_hand(self) -> None:
        result = verify_j3(output([statement("The adjusted p is 2.1e-9.", "f.s10.top.1.padj")]))
        self.assertEqual(dropped(result), ["V-NUM"])

    def test_cohort_names_with_a_gene_symbol_are_allowed(self) -> None:
        text = "Expression is higher in KRAS mutated (ORION) than in KRAS not detected (ORION)."
        self.assertEqual(dropped(verify_j3(output([statement(text)]))), [None])

    def test_ledger_cohort_names_are_allowed(self) -> None:
        text = "This differs from the earlier NRAS mutated (ORION) cohort."
        self.assertEqual(dropped(verify_j3(output([statement(text)]), ledger=LEDGER)), [None])
        self.assertEqual(dropped(verify_j3(output([statement(text)]))), ["V-ENT"])

    def test_vocabulary_terms_with_numerals_are_allowed(self) -> None:
        self.assertEqual(dropped(verify_j3(output([statement("A split by ISS stage, such as Stage II, may differ.")]))), [None])


class VRefTest(unittest.TestCase):
    def test_an_unknown_placeholder_fails(self) -> None:
        self.assertEqual(dropped(verify_j3(output([statement("{{f.s11.top.1.gene}} leads.", "f.s10.top.1.gene")]))), ["V-REF"])

    def test_an_unknown_cite_fails(self) -> None:
        self.assertEqual(dropped(verify_j3(output([statement("The ranking is stable.", "f.s99.count")]))), ["V-REF"])

    def test_a_placeholder_missing_from_cites_fails(self) -> None:
        self.assertEqual(dropped(verify_j3(output([statement("{{f.s10.top.2.gene}} follows.", "f.s10.top.1.gene")]))), ["V-REF"])

    def test_a_malformed_placeholder_fails(self) -> None:
        self.assertEqual(dropped(verify_j3(output([statement("{{f.s10.top.1.gene} leads.")]))), ["V-REF"])

    def test_a_dropped_sentence_keeps_no_text_or_cites(self) -> None:
        result = verify_j3(output([statement("The two groups differ.")]))
        self.assertEqual(result.interpretation[0].model_dump(mode="json", exclude_none=True), {"segments": [], "dropped": "V-NUM"})


class VEntTest(unittest.TestCase):
    def test_an_unknown_orion_cohort_fails(self) -> None:
        self.assertEqual(dropped(verify_j3(output([statement("Compare with Lytic lesions (ORION).")]))), ["V-ENT"])

    def test_a_quoted_unknown_name_fails_and_a_known_one_passes(self) -> None:
        self.assertEqual(dropped(verify_j3(output([statement('The "Responders" cohort differs.')]))), ["V-ENT"])
        self.assertEqual(dropped(verify_j3(output([statement('The "KRAS mutated (ORION)" cohort differs.')]))), [None])

    def test_a_follow_up_may_name_a_panel_gene_or_a_run_gene(self) -> None:
        follow = [
            ComposerFollowUp(kind="survival_compare", label="Survival by KRAS status", question="Compare survival by KRAS status"),
            ComposerFollowUp(kind="survival_compare", label="Survival by ETV4", question="Compare survival by ETV4 expression"),
            ComposerFollowUp(kind="survival_compare", label="Survival by EGFR", question="Compare survival by EGFR status"),
        ]
        result = verify_j3(output(follow_ups=follow))
        self.assertEqual([f.label for f in result.follow_ups], ["Survival by KRAS status", "Survival by ETV4"])
        failure = [f for f in result.findings if f.part == "follow_ups" and not f.passed]
        self.assertEqual([(f.rule, f.index) for f in failure], [("V-ENT", 2)])


class VSecTest(unittest.TestCase):
    def test_a_causal_sentence_fails(self) -> None:
        for text in ("{{f.s10.top.1.gene}} drives the difference.", "KRAS mutated (ORION) status causes this.",
                     "This proves a pathway effect.", "The result clearly shows a shift."):
            with self.subTest(text=text):
                self.assertEqual(dropped(verify_j3(output([statement(text)]))), ["V-SEC"])

    def test_a_negated_claim_passes(self) -> None:
        for text in ("Frequency alone does not show a driver role.", "Differential expression alone does not establish a mechanism.",
                     "Test whether the ranking holds, to confirm it."):
            with self.subTest(text=text):
                self.assertEqual(dropped(verify_j3(output([statement(text)]))), [None])

    def test_clinical_advice_fails_even_negated(self) -> None:
        for text in ("These patients should receive an inhibitor.", "This is no basis for treatment.", "Dosing may differ."):
            with self.subTest(text=text):
                self.assertEqual(dropped(verify_j3(output([statement(text)]))), ["V-SEC"])

    def test_hypotheses_are_questions_or_hedged_actions(self) -> None:
        questions = [
            ComposerHypothesis(text="Is {{f.s10.top.1.gene}} associated with survival?", cites=["f.s10.top.1.gene"], form="question"),
            ComposerHypothesis(text="{{f.s10.top.1.gene}} is associated with survival.", cites=["f.s10.top.1.gene"], form="question"),
        ]
        actions = [
            ComposerHypothesis(text="Compare survival between the groups.", cites=["f.s10.top.1.gene"], form="action"),
            ComposerHypothesis(text="{{f.s10.top.1.gene}} is higher in both groups.", cites=["f.s10.top.1.gene"], form="action"),
            ComposerHypothesis(text="Survival might differ between the groups.", cites=["f.s10.top.1.gene"], form="action"),
        ]
        self.assertEqual(dropped(verify_j3(output(hypotheses=questions)), "hypotheses"), [None, "V-SEC"])
        self.assertEqual(dropped(verify_j3(output(hypotheses=actions)), "hypotheses"), [None, "V-SEC", None])

    def test_a_follow_up_asking_for_advice_is_dropped(self) -> None:
        follow = [ComposerFollowUp(kind="cohort_size", label="Treatment options", question="Which therapy suits these cases?")]
        self.assertEqual(verify_j3(output(follow_ups=follow)).follow_ups, [])


class FollowUpTest(unittest.TestCase):
    def test_one_top_n_is_allowed_only_for_ranking_kinds(self) -> None:
        kept = [
            ComposerFollowUp(kind="differential_expression", label="Show the top 20", question="Show the top 20 genes instead"),
            ComposerFollowUp(kind="survival_compare", label="Survival over 5 years", question="Compare survival over 5 years"),
        ]
        refused = [
            ComposerFollowUp(kind="top_mutated_genes", label="Top 3 and 5", question="Show the top 3 and 5 genes"),
            ComposerFollowUp(kind="differential_expression", label="Show the top 200", question="Show the top 200 genes"),
            ComposerFollowUp(kind="top_mutated_genes", label="Top three genes", question="Show the top three genes"),
        ]
        self.assertEqual([f.label for f in verify_j3(output(follow_ups=kept)).follow_ups], ["Show the top 20"])
        self.assertEqual(verify_j3(output(follow_ups=refused)).follow_ups, [])

    def test_gene_placeholders_resolve_to_plain_text_and_numbers_are_refused(self) -> None:
        follow = [
            ComposerFollowUp(kind="survival_compare", label="Survival by {{%s}} status" % DIS3, question="Compare survival by {{%s}} status" % DIS3),
            ComposerFollowUp(kind="cohort_size", label="Size near {{f.s9.tested}}", question="Count the {{f.s9.tested}} genes"),
        ]
        result = verify_j3(output(follow_ups=follow))
        self.assertEqual([(f.label, f.question) for f in result.follow_ups], [("Survival by DIS3 status", "Compare survival by DIS3 status")])

    def test_open_links_keep_only_apps_orion_opens(self) -> None:
        self.assertEqual([link.app_id for link in verify_j3(output(apps=["GeneExpression", "DE", "Projects"])).open_links], ["GeneExpression"])
        links = verify_j3(output(apps=["CohortComparison", "CohortComparison", "SetOperations"])).open_links
        self.assertEqual([(link.app_id, link.title) for link in links], [("CohortComparison", "Cohort Comparison"), ("SetOperations", "Set Operations")])


class GateTest(unittest.TestCase):
    def test_a_failure_on_the_first_attempt_asks_for_one_re_compose(self) -> None:
        result = verify_j3(output([statement("The two groups differ."), statement("{{f.s10.top.1.gene}} ranks first.")]))
        self.assertTrue(result.retry)
        self.assertFalse(result.passed)
        self.assertFalse(result.observed_only)
        [feedback] = model_text_feedback(result)
        self.assertEqual((feedback.rule, feedback.part, feedback.index), ("V-NUM", "interpretation", 0))

    def test_the_second_attempt_drops_without_another_retry(self) -> None:
        result = verify_j3(output([statement("The two groups differ."), statement("{{f.s10.top.1.gene}} ranks first.")]), attempt=2)
        self.assertFalse(result.retry)
        self.assertEqual(dropped(result), ["V-NUM", None])

    def test_no_surviving_interpretation_ends_observed_only(self) -> None:
        result = verify_j3(output([statement("The two groups differ."), statement("BRAF leads.")]), attempt=2)
        self.assertTrue(result.observed_only)

    def test_a_clean_answer_passes_with_every_rule_recorded(self) -> None:
        result = verify_j3(output())
        self.assertTrue(result.passed)
        self.assertEqual({f.rule for f in result.findings if f.part == "interpretation"}, {"V-REF", "V-ENT", "V-NUM", "V-SEC"})


class FixtureModelTextTest(unittest.TestCase):
    """The contract fixtures' model text passes the verifier with nothing dropped or inserted."""

    def test_j1_complete(self) -> None:
        verifier, plan = CodeVerifier(), j1_plan()
        result = verifier.verify(
            fixture_model_output(last_view("j1-complete.events.jsonl")), j1_facts(), None, vocabulary(), plan=plan, observed=j1_observed(),
            attempt=1, allow_list=verifier.allow_list(vocabulary(), None, plan),
        )
        self.assertTrue(result.passed, [f for f in result.findings if not f.passed])
        self.assertEqual([s.model_dump(mode="json", exclude_none=True) for s in result.interpretation],
                         [s.model_dump(mode="json", exclude_none=True) for s in last_view("j1-complete.events.jsonl").interpretation or []])

    def test_j3_after_confirm(self) -> None:
        result = verify_j3(fixture_model_output(last_view(J3)))
        self.assertTrue(result.passed, [f for f in result.findings if not f.passed])
        self.assertEqual(result.follow_ups, last_view(J3).follow_ups)

    def test_oncomatrix(self) -> None:
        verifier, view, plan = CodeVerifier(), last_view("oncomatrix.events.jsonl"), genes_plan()
        result = verifier.verify(
            fixture_model_output(view), view_facts(view), None, vocabulary(), plan=plan, observed=view_observed(view), attempt=1,
            allow_list=verifier.allow_list(vocabulary(), None, plan),
        )
        self.assertTrue(result.passed, [f for f in result.findings if not f.passed])


class AllowListTest(unittest.TestCase):
    def test_the_allow_list_holds_terms_names_and_apps_but_no_bare_gene_or_number(self) -> None:
        verifier = CodeVerifier()
        allow = verifier.allow_list(vocabulary(), LEDGER, j3_plan())
        for term in ("log2FC", "Gen3 Analysis", "six-gene panel", "Bulk RNA-seq UMAP", "KRAS mutated (ORION)", "KRAS not detected",
                     "NRAS mutated (ORION)", "Stage II", "CGS"):
            self.assertIn(term, allow)
        for term in ("KRAS", "TP53", "0", "18.2"):
            self.assertNotIn(term, allow)
        self.assertEqual(allow, verifier.allow_list(vocabulary(), LEDGER, j3_plan()))


if __name__ == "__main__":
    unittest.main()
