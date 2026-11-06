"""The verifier's model-text rules, one fixture per rule (A2: placeholders for TP53 and DIS3, the
allow-listed log2FC and one causal sentence among them), and the Interpretation-present gate."""

import unittest
from collections.abc import Sequence
from typing import Final

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


def verify_named(out: ComposerOutput, scope_name: str) -> VerifierResult:
    """J3 with its scope named `scope_name`: a run that itself names a vocabulary value (a regimen cohort)."""
    verifier, view = CodeVerifier(), last_view(J3)
    plan = j3_plan()
    plan = plan.model_copy(update={"scope": plan.scope.model_copy(update={"name": scope_name})})
    allow = verifier.allow_list(vocabulary(), None, plan)
    return verifier.verify(out, view_facts(view), None, vocabulary(), plan=plan, observed=view_observed(view), attempt=1, allow_list=allow)


def dropped(result: VerifierResult, part: str = "interpretation") -> list[str | None]:
    sentences: list[Sentence] = getattr(result, part)
    return [s.dropped for s in sentences]


REGIMEN = "bortezomib, lenalidomide, dexamethasone"
REGIMEN_COHORT = "Bortezomib, lenalidomide, dexamethasone (ORION)"
CASE_SCOPES: dict[str, str | None] = {
    "R": "Bortezomib, lenalidomide, dexamethasone", "C": REGIMEN_COHORT, "T": "Completed planned therapy", "J": None,
}


class TreatmentDefinedTest(unittest.TestCase):
    """CodeVerifier.treatment_defined: which runs get no model text at all (answer.write_answer withholds it)."""

    def defined(self, scope_name: str | None) -> bool:
        verifier, view, plan = CodeVerifier(), last_view(J3), j3_plan()
        if scope_name is not None:
            plan = plan.model_copy(update={"scope": plan.scope.model_copy(update={"name": scope_name})})
        allow = verifier.allow_list(vocabulary(), None, plan)
        return verifier.treatment_defined(view_facts(view), None, vocabulary(), plan=plan, observed=view_observed(view), allow_list=allow)

    def test_runs_named_by_a_regimen_or_a_therapy_value_are_treatment_defined(self) -> None:
        for case, scope in CASE_SCOPES.items():
            with self.subTest(case=case, scope=scope):
                self.assertEqual(self.defined(scope), case != "J")


def verdict(case: str) -> str | None:
    """The rule that drops one model text, written "<kind>|<scope>|<text>" as the review probes write it. Kind: S an
    Interpretation statement, Q or A a question or action hypothesis, F or L a follow-up chip's question or label.
    Scope: R a run whose scope is the regimen value, C one whose scope is the regimen's cohort "… (ORION)", T one
    named by the field value "Completed planned therapy", J the J3 run, which names no regimen. "<R>" and "<C>" stand
    for the regimen and its cohort."""
    kind, scope, text = case.split("|", 2)
    text = text.replace("<R>", REGIMEN).replace("<C>", REGIMEN_COHORT)
    name = CASE_SCOPES[scope]

    def run(out: ComposerOutput) -> VerifierResult:
        return verify_j3(out) if name is None else verify_named(out, name)

    if kind == "S":
        return dropped(run(output([statement(text)])))[0]
    if kind in ("Q", "A"):
        hypothesis = ComposerHypothesis(text=text, cites=["f.s10.top.1.gene"], form="question" if kind == "Q" else "action")
        return dropped(run(output(hypotheses=[hypothesis])), "hypotheses")[0]
    chip = ComposerFollowUp(kind="survival_compare", label=text if kind == "L" else "Survival by group",
                            question=text if kind == "F" else "Compare survival by ISS stage")
    failed = [f.rule for f in run(output(follow_ups=[chip])).findings if f.part == "follow_ups" and not f.passed]
    return failed[0] if failed else None


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

    def test_a_vocabulary_term_with_a_digit_passes_only_when_the_run_names_it(self) -> None:
        """Seen live on Bedrock: "del(17p) and/or TP53 mutation" in a J3 hypothesis, a whole vocabulary value
        that the run does not name. The contract allows a digit outside a fact chip only in the run's own words."""
        text = "Check whether the t(4;14): Present cases differ."
        self.assertEqual(dropped(verify_j3(output([statement(text)]))), ["V-NUM"])
        verifier = CodeVerifier()
        self.assertNotIn("t(4;14): Present", verifier.allow_list(vocabulary(), None, j3_plan()))
        named = j3_plan().model_copy(update={"scope": j3_plan().scope.model_copy(update={"name": "t(4;14): Present"})})
        self.assertIn("t(4;14): Present", verifier.allow_list(vocabulary(), None, named))

    def test_fold_words_unicode_digits_fractions_and_lower_case_panel_genes_fail(self) -> None:
        for text in ("The cohort shows a twofold higher rate.", "The groups show a tenfold difference.",
                     "About ５０% of the genes are higher.", "Roughly ½ of the genes rise.", "Expression rises by a factor of 2².",
                     "In these cases, kras looks more often mutated.", "Braf mutated cases differ."):
            with self.subTest(text=text):
                self.assertEqual(dropped(verify_j3(output([statement(text)]))), ["V-NUM"])

    def test_words_that_only_end_in_fold_pass(self) -> None:
        self.assertEqual(dropped(verify_j3(output([statement("The ranking may unfold differently in a manifold of scopes.")]))), [None])


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

    def test_compound_therapy_words_benefit_and_drug_names_fail(self) -> None:
        for text in ("Cases with a {{f.s10.top.1.gene}} mutation may benefit from chemotherapy.",
                     "These cases are good candidates for immunotherapy or a stem cell transplant.",
                     "These cases may respond to radiotherapy.", "These cases may benefit from a bortezomib-based regimen.",
                     "Daratumumab could help these cases.", "A cilta-cel approach may suit these cases.",
                     "{{f.s10.top.1.gene}} may be targetable with sotorasib."):
            with self.subTest(text=text):
                cites = ("f.s10.top.1.gene",) if "{{" in text else ()
                self.assertEqual(dropped(verify_j3(output([statement(text, *cites)]))), ["V-SEC"])

    def test_research_wording_passes(self) -> None:
        self.assertEqual(dropped(verify_j3(output([statement("These genes are candidates for further study.")]))), [None])

    def test_a_vocabulary_name_with_a_clinical_word_passes_only_when_the_run_names_it(self) -> None:
        """A regimen value is a name in a run that uses it (a regimen-defined cohort), and clinical wording in
        any other run: J3 does not use the regimen field (security-vsec-regimen-exemption)."""
        cases = (
            ("Survival may differ between the Bortezomib, lenalidomide, dexamethasone cases and the remaining cases.",
             "Bortezomib, lenalidomide, dexamethasone"),
            ("Cases whose reason for discontinuation is Completed planned therapy may differ.", "Completed planned therapy"),
            # A regimen short name is a regimen value too (the vocabulary's regimen fields).
            ("Survival may differ between the VRd cases and the remaining cases.", "VRd"),
        )
        for text, name in cases:
            with self.subTest(text=text):
                self.assertEqual(dropped(verify_j3(output([statement(text)]))), ["V-SEC"])
                self.assertEqual(dropped(verify_named(output([statement(text)]), name)), [None])
        # Fourth review: "VRd" read as a plain word, so "Use VRd for the mutated cases" passed in any run. It was the
        # second group of this test's first sentence; in a run that names only the long regimen it is a regimen
        # value the run does not use.
        text = "Survival may differ between the Bortezomib, lenalidomide, dexamethasone and VRd groups."
        self.assertEqual(dropped(verify_named(output([statement(text)]), "Bortezomib, lenalidomide, dexamethasone")), ["V-SEC"])

    def test_a_treatment_suggestion_naming_a_regimen_fails_in_any_run(self) -> None:
        """'Consider <regimen> for <cohort>' is advice whether or not the run names the regimen (G-5)."""
        regimen = "Daratumumab, lenalidomide, dexamethasone"
        suggestions = [
            ComposerHypothesis(text="Consider daratumumab, lenalidomide, dexamethasone for KRAS mutated (ORION).",
                               cites=["f.s10.top.1.gene"], form="action"),
            ComposerHypothesis(text="Consider daratumumab, lenalidomide, dexamethasone for the mutated group.",
                               cites=["f.s10.top.1.gene"], form="action"),
            ComposerHypothesis(text="Should the mutated group get daratumumab, lenalidomide, dexamethasone?",
                               cites=["f.s10.top.1.gene"], form="question"),
        ]
        statements = [statement("These cases may do well on daratumumab, lenalidomide, dexamethasone."),
                      statement("Daratumumab, lenalidomide, dexamethasone may suit the mutated group.")]
        research = [statement("Survival may differ between the Daratumumab, lenalidomide, dexamethasone cases and the others."),
                    statement("Consider the Daratumumab, lenalidomide, dexamethasone group separately.")]
        for result in (verify_j3(output(statements, suggestions)), verify_named(output(statements, suggestions), regimen)):
            self.assertEqual(dropped(result), ["V-SEC", "V-SEC"])
            self.assertEqual(dropped(result, "hypotheses"), ["V-SEC", "V-SEC", "V-SEC"])
        self.assertEqual(dropped(verify_named(output(research), regimen)), [None, None])
        self.assertEqual(dropped(verify_j3(output(research))), ["V-SEC", "V-SEC"])
        chip = ComposerFollowUp(kind="survival_compare", label="Regimen for the mutated group",
                                question="Should KRAS mutated cases get daratumumab, lenalidomide, dexamethasone?")
        plain = ComposerFollowUp(kind="survival_compare", label="Survival by regimen",
                                 question="Compare survival of Daratumumab, lenalidomide, dexamethasone cases by gender")
        self.assertEqual([f.label for f in verify_named(output(follow_ups=[chip, plain]), regimen).follow_ups], ["Survival by regimen"])

    def test_a_regimen_mentioned_other_than_as_a_name_fails_even_in_its_own_run(self) -> None:
        """Review finding F1 (V-SEC regimen exemption): in a run that names the regimen, advice worded outside the old
        advice-framing list was kept. The regimen is now a name only where the text uses it as one; every other
        mention is checked as in a run that does not use it."""
        regimen = "Bortezomib, lenalidomide, dexamethasone"
        statements = [statement(text) for text in (
            "Bortezomib, lenalidomide, dexamethasone should be used in these cases.",
            "Use bortezomib, lenalidomide, dexamethasone in the mutated group.",
            "These cases need bortezomib, lenalidomide, dexamethasone.",
            "Bortezomib, lenalidomide, dexamethasone works best in the mutated group.",
            "Bortezomib, lenalidomide, dexamethasone would be the better option here.",
            regimen + ".",
        )]
        hypotheses = [
            ComposerHypothesis(text="Explore bortezomib, lenalidomide, dexamethasone as an option for the mutated group.",
                               cites=["f.s10.top.1.gene"], form="action"),
            ComposerHypothesis(text="Would the mutated group do better with bortezomib, lenalidomide, dexamethasone?",
                               cites=["f.s10.top.1.gene"], form="question"),
            ComposerHypothesis(text="Is bortezomib, lenalidomide, dexamethasone the right choice for the mutated group?",
                               cites=["f.s10.top.1.gene"], form="question"),
        ]
        chips = [
            ComposerFollowUp(kind="survival_compare", label="Regimen option",
                             question="Is bortezomib, lenalidomide, dexamethasone an option for the mutated group?"),
            ComposerFollowUp(kind="survival_compare", label="Regimen use",
                             question="Use bortezomib, lenalidomide, dexamethasone in the mutated group?"),
        ]
        for item in statements:
            with self.subTest(text=item.text):
                self.assertEqual(dropped(verify_named(output([item]), regimen)), ["V-SEC"])
                self.assertEqual(dropped(verify_j3(output([item]))), ["V-SEC"])
        for result in (verify_named(output(hypotheses=hypotheses, follow_ups=chips), regimen), verify_j3(output(hypotheses=hypotheses, follow_ups=chips))):
            self.assertEqual(dropped(result, "hypotheses"), ["V-SEC"] * len(hypotheses))
            self.assertEqual(result.follow_ups, [])
            failed = {(f.part, f.rule) for f in result.findings if not f.passed}
            self.assertEqual(failed, {("hypotheses", "V-SEC"), ("follow_ups", "V-SEC")})

    def test_an_orion_suffixed_regimen_cohort_is_one_name(self) -> None:
        """Review finding F2: the cohort's label inside "<regimen> (ORION)" was matched again and read as advice."""
        cohort = "Bortezomib, lenalidomide, dexamethasone (ORION)"
        texts = [statement("Consider the Bortezomib, lenalidomide, dexamethasone (ORION) cohort separately."),
                 statement("Survival in Bortezomib, lenalidomide, dexamethasone (ORION) may differ by ISS stage.")]
        hypothesis = ComposerHypothesis(text="Consider Bortezomib, lenalidomide, dexamethasone (ORION) as a separate group.",
                                        cites=["f.s10.top.1.gene"], form="action")
        result = verify_named(output(texts, [hypothesis]), cohort)
        self.assertEqual(dropped(result), [None, None])
        self.assertEqual(dropped(result, "hypotheses"), [None])
        self.assertEqual(dropped(verify_named(output([statement("Consider Bortezomib, lenalidomide, dexamethasone for these cases.")]),
                                              cohort)), ["V-SEC"])

    def test_a_group_described_by_its_regimen_is_a_name_but_not_a_treatment_judgement(self) -> None:
        """Review finding F3: "cases receiving <regimen>" describes the cohort and was read as advice. Descriptive
        groups pass; imperative, modal, question and benefit framings still fail."""
        regimen = "Bortezomib, lenalidomide, dexamethasone"
        names = [statement(text) for text in (
            "Survival may differ between cases receiving bortezomib, lenalidomide, dexamethasone and the others.",
            "Cases taking bortezomib, lenalidomide, dexamethasone may differ in ISS stage.",
            "Cases starting bortezomib, lenalidomide, dexamethasone may differ in ISS stage.",
            "Cases that received bortezomib, lenalidomide, dexamethasone may differ in ISS stage.",
            "Cases on bortezomib, lenalidomide, dexamethasone may differ in ISS stage.",
            "Survival may differ between the Bortezomib, lenalidomide, dexamethasone cases and the others.",
            "Survival may differ between bortezomib, lenalidomide, dexamethasone and the remaining cases.",
            "Within bortezomib, lenalidomide, dexamethasone, the ranking may differ.",
        )]
        questions = [ComposerHypothesis(text="Do cases receiving bortezomib, lenalidomide, dexamethasone differ in ISS stage?",
                                        cites=["f.s10.top.1.gene"], form="question"),
                     ComposerHypothesis(text="Compare survival of cases receiving bortezomib, lenalidomide, dexamethasone with the others.",
                                        cites=["f.s10.top.1.gene"], form="action")]
        chip = ComposerFollowUp(kind="survival_compare", label="Survival by gender",
                                question="Compare survival of cases receiving bortezomib, lenalidomide, dexamethasone by gender")
        for item in names:
            with self.subTest(text=item.text):
                self.assertEqual(dropped(verify_named(output([item]), regimen)), [None])
        result = verify_named(output(hypotheses=questions, follow_ups=[chip]), regimen)
        self.assertEqual(dropped(result, "hypotheses"), [None, None])
        self.assertEqual([f.label for f in result.follow_ups], ["Survival by gender"])
        advice = [statement(text) for text in (
            "Patients receiving bortezomib, lenalidomide, dexamethasone may fare better.",
            "Patients receiving bortezomib, lenalidomide, dexamethasone should continue.",
            "The Bortezomib, lenalidomide, dexamethasone cases did better than the others.",
            "Keep the mutated cases on bortezomib, lenalidomide, dexamethasone.",
            "These cases may do well on bortezomib, lenalidomide, dexamethasone.",
            "Cases on bortezomib, lenalidomide, dexamethasone may switch to another regimen.",
            "For cases receiving bortezomib, lenalidomide, dexamethasone, it may be the preferred choice.",
            "There may be interest in bortezomib, lenalidomide, dexamethasone for the mutated group.",
        )]
        question = ComposerHypothesis(text="Should the mutated group get bortezomib, lenalidomide, dexamethasone?",
                                      cites=["f.s10.top.1.gene"], form="question")
        for item in advice:
            with self.subTest(text=item.text):
                self.assertEqual(dropped(verify_named(output([item]), regimen)), ["V-SEC"])
        self.assertEqual(dropped(verify_named(output(hypotheses=[question]), regimen), "hypotheses"), ["V-SEC"])
        # The run's field value read through its field label stays a name.
        text = "Cases whose reason for discontinuation is Completed planned therapy may differ."
        self.assertEqual(dropped(verify_named(output([statement(text)]), "Completed planned therapy")), [None])
        self.assertEqual(dropped(verify_named(output([statement("Completed planned therapy may suit these cases.")]),
                                              "Completed planned therapy")), ["V-SEC"])

    def test_advice_before_a_listed_regimen_still_fails(self) -> None:
        """Third review, finding 1: the list exemption took any "and …" clause that held a group word, verb phrases
        included, and the advice-framing check before the name was gone, so "Try <regimen> and see whether …" passed.
        A list member is a noun phrase, and the advice framing right before the regimen fails it as at bb7c6e0."""
        regimen, r = "Bortezomib, lenalidomide, dexamethasone", "bortezomib, lenalidomide, dexamethasone"
        statements = [statement(text.replace("<R>", r)) for text in (
            "Try <R> and see whether the mutated cases respond.",
            "Start <R> and monitor the high-risk patients.",
            "Add <R> and reassess the mutated cases.",
            "Consider <R> and watch the mutated cases closely.",
            "Consider adding <R> and check whether the high-risk patients improve.",
            "Use <R> and monitor the patients.",
            "Prefer <R> and lenalidomide maintenance for these patients.",
            "Use <R> or VRd for the mutated cases.",
            "Consider <R> and female patients separately.",
        )]
        hypotheses = [
            ComposerHypothesis(text=f"Try {r} and see whether the mutated cases respond.", cites=["f.s10.top.1.gene"], form="action"),
            ComposerHypothesis(text=f"Consider adding {r} and check whether the high-risk patients improve.",
                               cites=["f.s10.top.1.gene"], form="action"),
            ComposerHypothesis(text=f"Consider {r} and check whether the mutated cases improve.", cites=["f.s10.top.1.gene"], form="action"),
            ComposerHypothesis(text=f"Should the mutated cases start {r} and see whether the patients respond?",
                               cites=["f.s10.top.1.gene"], form="question"),
            ComposerHypothesis(text=f"Could the mutated cases get {r} and other high-risk patients too?",
                               cites=["f.s10.top.1.gene"], form="question"),
            ComposerHypothesis(text=f"Would starting {r} and monitoring the high-risk patients help?", cites=["f.s10.top.1.gene"], form="question"),
            ComposerHypothesis(text=f"Would {r} or VRd help the mutated cases?", cites=["f.s10.top.1.gene"], form="question"),
        ]
        chips = [
            ComposerFollowUp(kind="survival_compare", label="Survival by group", question=f"Start {r} and compare survival of the high-risk patients"),
            ComposerFollowUp(kind="survival_compare", label="Survival by group", question=f"Try {r} and compare survival of the mutated cases"),
        ]
        for item in statements:
            with self.subTest(text=item.text):
                self.assertEqual(dropped(verify_named(output([item]), regimen)), ["V-SEC"])
                self.assertEqual(dropped(verify_j3(output([item]))), ["V-SEC"])
        for hypothesis in hypotheses:
            with self.subTest(text=hypothesis.text):
                for result in (verify_named(output(hypotheses=[hypothesis]), regimen), verify_j3(output(hypotheses=[hypothesis]))):
                    self.assertEqual(dropped(result, "hypotheses"), ["V-SEC"])
        for result in (verify_named(output(follow_ups=chips), regimen), verify_j3(output(follow_ups=chips))):
            self.assertEqual(result.follow_ups, [])
        for chip in chips:
            with self.subTest(question=chip.question):
                self.assertEqual(verify_named(output(follow_ups=[chip]), regimen).follow_ups, [])

    def test_a_group_described_by_its_regimen_passes_only_in_a_descriptive_sentence(self) -> None:
        """Third review, finding 2: "patients receiving <regimen>" was exempt as a described group, and benefit,
        outcome and management wording was not a treatment judgement, so "Patients receiving <regimen> may live
        longer" passed. Such a group passes only where the sentence describes it (its size, members, or a survival or
        expression comparison stated as data)."""
        regimen, r = "Bortezomib, lenalidomide, dexamethasone", "bortezomib, lenalidomide, dexamethasone"
        judged = [statement(text.replace("<R>", r)) for text in (
            "Patients receiving <R> may live longer.",
            "Patients receiving <R> may survive longer than the others.",
            "Patients receiving <R> may have better outcomes.",
            "Patients receiving <R> may benefit.",
            "Patients receiving <R> appear to benefit.",
            "Patients receiving <R> may outperform the others.",
            "Patients receiving <R> may see a survival advantage.",
            "Patients receiving <R> may have a lower risk of death.",
            "Patients receiving <R> may respond.",
            "Patients receiving <R> should keep receiving it.",
            "Patients receiving <R> should be monitored rather than switched.",
            "Patients starting <R> early may have better outcomes.",
            "Patients taking <R> could see longer survival.",
            "Patients receiving <R> may gain the most.",
            "Patients receiving <R> may progress less often.",
            "Patients given <R> could live longer.",
            "Cases receiving <R> may respond.",
            "Those on <R> should keep going.",
            "Patients on <R> may need to stay on it.",
            "Leave the mutated cases on <R>.",
            "Maintain the mutated cases on <R>.",
            "Start the high-risk cases on <R>.",
            "Enroll the mutated cases in <R>.",
            "Place the high-risk cases in <R>.",
            "Within <R>, the mutated cases may need closer follow-up.",
            "Survival may be longer in <R> than in the others, so it is worth using.",
            "There is interest in <R>, especially for the mutated group.",
            "Patients in <R> may benefit.",
        )]
        question = ComposerHypothesis(text=f"Do patients receiving {r} live longer?", cites=["f.s10.top.1.gene"], form="question")
        for item in judged:
            with self.subTest(text=item.text):
                self.assertEqual(dropped(verify_named(output([item]), regimen)), ["V-SEC"])
        self.assertEqual(dropped(verify_named(output(hypotheses=[question]), regimen), "hypotheses"), ["V-SEC"])
        descriptive = [statement(text.replace("<R>", r)) for text in (
            "Patients receiving <R> may be older than the other cases.",
            "Survival may differ between patients receiving <R> and the other cases.",
            "Survival may be longer in patients receiving <R> than in the others.",
            "Patients who are receiving <R> make up part of this cohort.",
            "The ranking of the top genes may differ in patients receiving <R>.",
            "Compare <R> and the remaining cases.",
            "Is the ranking the same in <R> and the other high-risk patients?",
            "Survival may differ between <R> and KRAS mutated (ORION).",
            "Within <R>, for each ISS stage, the ranking may differ.",
            "The genes of interest in <R> may differ.",
        )]
        hypotheses = [
            ComposerHypothesis(text=f"Do patients taking {r} differ in age at diagnosis from the others?",
                               cites=["f.s10.top.1.gene"], form="question"),
            # Fourth review: "VRd" is a regimen value this run does not use, so the list names a group with
            # "the remaining cases" instead (test_a_vocabulary_name_with_a_clinical_word_passes_only_when_the_run_names_it).
            ComposerHypothesis(text="Compare the Bortezomib, lenalidomide, dexamethasone cases and the remaining cases by ISS stage.",
                               cites=["f.s10.top.1.gene"], form="action"),
        ]
        for item in descriptive:
            with self.subTest(text=item.text):
                self.assertEqual(dropped(verify_named(output([item]), regimen)), [None])
        self.assertEqual(dropped(verify_named(output(hypotheses=hypotheses), regimen), "hypotheses"), [None, None])

    def test_an_orion_suffixed_regimen_cohort_still_takes_the_advice_check_after_it(self) -> None:
        """Third review, finding 3: a name ending in " (ORION)" was a name before the words after it were read, so
        "Consider <regimen> (ORION) for these cases" passed. The framing around a whole cohort name is still read."""
        cohort = "Bortezomib, lenalidomide, dexamethasone (ORION)"
        advice = [statement(text) for text in (
            "Consider Bortezomib, lenalidomide, dexamethasone (ORION) for these cases.",
            "Try Bortezomib, lenalidomide, dexamethasone (ORION) on the mutated cases.",
            "Use Bortezomib, lenalidomide, dexamethasone (ORION) for these cases.",
            "Bortezomib, lenalidomide, dexamethasone (ORION) may suit the mutated cases.",
            "Bortezomib, lenalidomide, dexamethasone (ORION) is the preferred option.",
            "The mutated cases may do well on Bortezomib, lenalidomide, dexamethasone (ORION).",
            "Give Bortezomib, lenalidomide, dexamethasone (ORION) to the mutated cases.",
        )]
        question = ComposerHypothesis(text="Should the mutated cases receive Bortezomib, lenalidomide, dexamethasone (ORION)?",
                                      cites=["f.s10.top.1.gene"], form="question")
        chip = ComposerFollowUp(kind="survival_compare", label="Survival by group",
                                question="Try Bortezomib, lenalidomide, dexamethasone (ORION) on the high-risk patients")
        for item in advice:
            with self.subTest(text=item.text):
                self.assertEqual(dropped(verify_named(output([item]), cohort)), ["V-SEC"])
        result = verify_named(output(hypotheses=[question], follow_ups=[chip]), cohort)
        self.assertEqual(dropped(result, "hypotheses"), ["V-SEC"])
        self.assertEqual(result.follow_ups, [])
        research = [
            ComposerHypothesis(text="Repeat the ranking on the Bortezomib, lenalidomide, dexamethasone (ORION) cohort.",
                               cites=["f.s10.top.1.gene"], form="action"),
            ComposerHypothesis(text="Compare survival of Bortezomib, lenalidomide, dexamethasone (ORION) with the remaining cases.",
                               cites=["f.s10.top.1.gene"], form="action"),
        ]
        plain = ComposerFollowUp(kind="survival_compare", label="Survival by gender",
                                 question="Compare survival of Bortezomib, lenalidomide, dexamethasone (ORION) by gender")
        result = verify_named(output(hypotheses=research, follow_ups=[plain]), cohort)
        self.assertEqual(dropped(result, "hypotheses"), [None, None])
        self.assertEqual([f.label for f in result.follow_ups], ["Survival by gender"])

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

    def test_a_causal_or_certain_follow_up_is_dropped(self) -> None:
        """Follow-up labels and questions take V-SEC's causal and certainty check too (security-followup-no-causal-check)."""
        follow = [
            ComposerFollowUp(kind="top_mutated_genes", label="Genes that drive survival", question="Which genes are mutated most often?"),
            ComposerFollowUp(kind="survival_compare", label="Survival by KRAS status",
                             question="Does a KRAS mutation cause shorter survival?"),
            ComposerFollowUp(kind="survival_compare", label="Survival by KRAS status", question="KRAS clearly shortens survival; compare it"),
        ]
        result = verify_j3(output(follow_ups=follow))
        self.assertEqual(result.follow_ups, [])
        failed = [(f.index, f.rule) for f in result.findings if f.part == "follow_ups" and not f.passed]
        self.assertEqual(failed, [(0, "V-SEC"), (1, "V-SEC"), (2, "V-SEC")])
        self.assertTrue(result.retry)
        kept = [
            ComposerFollowUp(kind="survival_compare", label="Survival by ISS stage", question="Compare survival by ISS stage"),
            ComposerFollowUp(kind="survival_compare", label="Survival by gender",
                             question="Compare survival by gender, to determine whether it differs"),
            ComposerFollowUp(kind="top_mutated_genes", label="No driver role shown", question="Which genes are not shown to drive survival?"),
        ]
        self.assertEqual([f.label for f in verify_j3(output(follow_ups=kept)).follow_ups], [f.label for f in kept])


# Fourth review of the V-SEC regimen rule: the bypasses the skeptics found in the third-round verifier. Each is advice
# or a treatment judgement; in the third round a described group ("patients receiving <R>"), a population ("in <R>"),
# a group word ("the <R> arm") or a whole cohort name made the regimen a name, and only closed lists of judgement and
# advice words were left to fail the sentence.
FOURTH_REVIEW_BYPASSES: Final = (
    # Benefit and outcome claims about a described group.
    "S|R|Patients receiving <R> may thrive.", "S|C|Patients receiving <R> may thrive.",
    "S|R|Patients receiving <R> may have fewer relapses.", "S|R|Patients receiving <R> may relapse later.",
    "S|R|Patients receiving <R> may have lower mortality.", "S|R|Patients receiving <R> may be less likely to die.",
    "S|R|Patients receiving <R> may be less likely to progress.", "S|R|Patients receiving <R> may outlive the others.",
    "S|R|Patients receiving <R> may live for longer.", "S|R|Patients receiving <R> may have deeper remissions.",
    "S|R|Patients receiving <R> may stay in remission longer.", "S|R|Patients receiving <R> may have a survival edge.",
    "S|R|Patients receiving <R> may come out ahead.", "S|R|Patients receiving <R> may have a better life expectancy.",
    "S|R|Patients starting <R> early may gain survival.",
    # Management advice about a described group.
    "S|R|Patients receiving <R> may want to stay with it.", "S|R|Patients receiving <R> may not need to change it.",
    "S|R|Patients receiving <R> may be fine on it.", "S|R|Patients receiving <R> may do just as well without escalation.",
    "S|R|Patients receiving <R> may be candidates to continue.", "S|R|Patients receiving <R> may be good candidates.",
    "S|R|Patients taking <R> may wish to keep at it.", "S|R|Patients receiving <R> may be wise to persist.",
    "S|R|Patients receiving <R> should be spared escalation.", "S|R|Patients receiving <R> should not be changed over.",
    "S|R|For patients receiving <R>, carrying on seems sensible.",
    "S|R|Survival may be longer in patients receiving <R> than in the others, which argues for it.",
    # Cases put into the described group.
    "S|R|The mutated cases should be among the patients receiving <R>.",
    "S|R|The mutated cases ought to join the patients receiving <R>.",
    "S|R|Clinicians may want to add the mutated cases to the patients receiving <R>.",
    "S|R|More patients receiving <R> would be good.", "S|C|More patients receiving <C> would be good.",
    "Q|R|Would the mutated cases be better among those receiving <R>?",
    "Q|R|Would the mutated cases be better served among those receiving <R>?",
    "Q|R|Is it time for the mutated cases to join those receiving <R>?",
    "Q|R|Would the mutated cases profit from being among those receiving <R>?",
    "Q|R|Would the mutated cases be better candidates among those receiving <R>?",
    "Q|R|Should patients receiving <R> persist with it?", "Q|R|Do patients receiving <R> have fewer relapses?",
    "A|R|Check whether the mutated cases should be among those receiving <R>.",
    "F|R|Should the high-risk patients join those receiving <R>?", "F|R|Do patients receiving <R> relapse less often?",
    "F|R|Should patients receiving <R> carry on?",
    # The cohort's whole name offered.
    "S|C|Consider <C> as first-line for the mutated cases.", "S|C|Add <C> to the mutated cases' regimen.",
    "S|C|Consider <C> first for the mutated cases.", "S|C|Consider <C> early in the mutated cases.",
    "S|C|Use <C> in the mutated cases.", "S|C|Apply <C> to the mutated cases.",
    "S|C|<C> may prolong survival in the mutated cases.", "S|C|<C> may extend life in the mutated cases.",
    "S|C|<C> may lower mortality in the high-risk patients.", "S|C|<C> may pay off for the mutated cases.",
    "S|C|<C>, perhaps, could help the mutated cases.", "S|C|<C> alone could help the mutated cases.",
    "S|C|<C> in particular could help the mutated cases.", "S|C|<C> looks like the way to go for these cases.",
    "S|C|<C> may be a good fit for the mutated cases.",
    # "patients on <R>": keeping or starting them on it (bb7c6e0 also kept these).
    "S|R|It may be worth keeping patients on <R>.", "S|R|Keeping patients on <R> may be wise.",
    "S|R|Leaving the mutated patients on <R> seems reasonable.", "S|R|Clinicians might keep high-risk patients on <R>.",
    "S|R|It might be sensible to start the high-risk patients on <R>.",
    "S|R|It may be worthwhile starting the high-risk patients on <R>.", "S|R|Begin the high-risk patients on <R>.",
    "S|R|Initiate the high-risk patients on <R>.", "S|R|Retain the mutated patients on <R>.",
    "S|R|Patients on <R> should not change.", "S|R|Patients on <R> should stick with it.",
    "S|R|Patients on <R> should be left there.", "S|R|Patients on <R> need not change course.",
    "S|R|Patients on <R> need not stop.", "S|R|Patients on <R> are worth keeping there.",
    "S|R|High-risk patients should be among those on <R>.",
    "S|R|The mutated cases are among those on <R>; keep them there.", "S|R|Note: keep the mutated patients on <R>.",
    "A|R|Compare survival, then keep the mutated patients on <R>.", "A|R|Explore starting the mutated patients on <R>.",
    "Q|R|Why not keep the mutated patients on <R>?", "Q|R|How about starting the mutated patients on <R>?",
    "Q|R|Could high-risk patients on <R> be kept there?", "Q|R|Should more patients be among those on <R>?",
    "F|R|Begin the high-risk patients on <R> and compare survival",
    "F|R|Retain the mutated patients on <R> and track survival", "F|R|Should patients on <R> stay the course?",
    # "in <R>": enrolling or keeping cases in it.
    "S|R|Starting the high-risk cases in <R> may be wise.", "S|R|It may be worth enrolling the mutated cases in <R>.",
    "S|R|A reasonable next step is enrolling the mutated cases in <R>.",
    "S|R|Initiating the mutated cases in <R> could be reasonable.", "S|R|The mutated cases should be put in <R>.",
    "S|R|The mutated cases should be enrolled in <R>.", "S|R|The mutated cases might be placed in <R>.",
    "S|R|Clinicians might place the high-risk cases in <R>.", "S|R|Let the mutated cases remain in <R>.",
    "S|R|Leaving the mutated cases in <R> seems sensible.",
    "S|R|Within <R>, the mutated cases may do fine, so stick with it.",
    "S|R|In <R>, survival may be longer, so it seems sensible to stay with it.",
    "S|R|Survival may be longer in <R> than in the others, which supports using it.",
    "Q|R|Should the mutated cases be in <R>?", "Q|R|Why not enroll the mutated cases in <R>?",
    "Q|R|How about keeping the mutated cases in <R>?", "Q|R|Is there a case for keeping the mutated cases in <R>?",
    "Q|R|Wouldn't the mutated cases be okay in <R>?", "F|R|Should the mutated cases be in <R>?",
    "A|R|Consider beginning the mutated cases in <R>.", "A|R|Consider including the mutated cases in <R>.",
    # A group word after the name.
    "S|R|Assign the mutated cases to the <R> arm.", "S|R|Transfer the high-risk patients to the <R> arm.",
    "S|R|Shift the mutated cases into the <R> group.", "S|R|Slot the mutated cases into the <R> arm.",
    "S|R|Use the <R> arm for the mutated cases.", "S|R|Consider the <R> group's regimen for the mutated cases.",
    "S|R|The <R> arm may be the better bet.", "S|R|The <R> arm may be the place to start for the mutated cases.",
    "S|R|The mutated cases should go in the <R> arm.", "S|R|The mutated cases could join the <R> group.",
    "Q|R|Should the mutated cases join the <R> arm?", "Q|R|What if the mutated cases joined the <R> arm?",
    "A|R|Check whether the high-risk patients should join the <R> arm.",
    "L|R|Assign to the <R> arm", "L|R|Join the <R> arm",
    "F|R|Assign the high-risk patients to the <R> arm and compare survival",
    # Lists, "between" and the field label.
    "S|R|Assign the mutated cases <R> and the remaining cases VRd.",
    "S|R|The mutated cases may need <R> and the remaining cases VRd.",
    "Q|R|Do the mutated cases need <R> and the remaining cases VRd?",
    "S|R|Decide between <R> and VRd for the mutated cases.", "S|R|Pick between <R> and VRd for the mutated cases.",
    "S|R|Deciding between <R> and VRd for the mutated cases may matter.",
    "S|R|Consider also <R> and VRd groups for the mutated cases.",
    "S|R|Cases whose regimen name is <R> could stay the course.",
    "Q|R|Should cases whose regimen name is <R> stay the course?",
)
# Third review (the first round of this rule) and other probes the skeptics ran: they stay failing.
OTHER_REGIMEN_ADVICE: Final = (
    "S|R|Try <R> and see whether the mutated cases respond.", "S|R|Use <R> and monitor the patients.",
    "S|C|Consider <C> for these cases.", "S|C|Try <C> on the mutated cases.", "S|R|<R>.",
    # Whole cohort name: advice outside the old lists, double objects, quotes, brackets, a dash.
    "S|C|Use <C> with the mutated cases.", "S|C|Consider <C>, for these cases.", "S|C|Begin <C> in the high-risk patients.",
    "S|C|Manage the mutated cases with <C>.", "S|C|The mutated cases could go on <C>.", "S|C|Opt for <C> in the mutated cases.",
    "S|C|<C> is worth a try in the mutated cases.", "S|C|<C> may be warranted in the mutated cases.",
    "S|C|<C> may induce remission in the mutated cases.", "S|C|<C> may be safer in the mutated cases.",
    "Q|C|Should the mutated cases be given <C>?", "Q|C|Is <C> right for the mutated cases?",
    "S|C|Clinicians could give the mutated cases <C>.", "S|C|Clinicians could try ‘<C>’ on the mutated cases.",
    "S|C|Clinicians could start the mutated cases on [<C>].", "S|C|<C> may be an alternative for the mutated cases.",
    "S|C|Consider <C> in the mutated cases.", "S|C|Consider <C> as the regimen in the mutated cases.",
    "S|C|Consider <C> — for these cases.", "S|C|Consider <C> here, for the mutated cases.",
    "Q|C|Should clinicians consider <C> first in the mutated cases?", "S|C|Use <C>, especially in the mutated cases.",
    "S|C|Bortezomib, Lenalidomide, Dexamethasone (ORION) is worth a try in the mutated cases.",
    # Described groups and populations with outcome, safety or anaphora wording.
    "S|R|Cases receiving <R> may have prolonged survival.", "S|R|Cases receiving <R> may be safer.",
    "S|R|Cases receiving <R> may be at lower risk.", "S|R|Patients receiving <R> may have higher survival.",
    "S|R|Patients receiving <R> may have a good outlook.", "S|R|Patients receiving <R> are well served by it.",
    "S|R|Cases receiving <R> form the larger group; clinicians could offer it to the mutated cases.",
    "S|R|Cases receiving <R> form the larger group; consider it for the mutated cases.",
    "S|R|Cases receiving <R> show longer median survival than the remaining cases, which may point to the regimen's value.",
    "S|R|Survival may be better in <R> than in the remaining cases.", "S|R|Mutated cases may last longer in <R> than in the other groups.",
    "Q|R|Would the mutated cases be safer in <R>?", "S|R|More of the high-risk cases could be among those receiving <R>.",
    "S|R|For the mutated cases, keep them on <R>.", "A|R|Check survival and, if it holds, put the high-risk patients on <R>.",
    # Regimen short names, brand names, steroids, care words, and spellings that hide a drug name.
    "S|R|Use VRd for the mutated cases.", "S|R|KRd may be the better option for the high-risk cases.",
    "Q|R|Should the mutated cases start DRd?", "F|R|Start VCd in the high-risk patients", "L|R|Start DRd early",
    "S|J|Use VRd for the mutated cases.", "S|J|Give Velcade and Revlimid to the mutated cases.",
    "Q|J|Should the mutated cases get steroids?", "S|J|Add maintenance for the mutated cases.",
    "S|J|Consider stem cell rescue for the high-risk cases.", "S|J|Intensify the regimen for the high-risk cases.",
    "S|J|The high-risk cases may need a stronger regimen.", "S|J|Patients may live longer on a triplet regimen.",
    "S|J|The mutated cases may warrant more aggressive management.",
    "S|R|Give bortez\u200bomib to the mutated cases.", "S|R|Give lenalid\u00adomide to the mutated cases.",
    "S|R|Give dexamethas\u043ene to the mutated cases.", "S|R|Give ｌｅｎａｌｉｄｏｍｉｄｅ to the mutated cases.",
    "S|R|Cases receiving <R> minus dexamethasone may do fine.", "S|C|Within <C>, the steroid could be dropped for the older cases.",
    # Frames built from descriptive words only (this round's own probes).
    "S|C|Survival may be longer in <C> than in the remaining cases, which suggests <C>.",
    "S|C|Survival may be longer in <C> than in the remaining cases, which suggests it.",
    "S|C|The <C> cohort is where the mutated cases could be.", "S|C|The next step is <C>.", "S|C|It is <C>.",
    "S|C|<C> it is.", "Q|R|What if the mutated cases were in <R>?", "S|R|Hold the mutated cases on <R>.",
    "S|J|Adjust the regimen for the high-risk cases.", "S|R|Patients receiving <R> may remain there.",
    "Q|R|Would the mutated cases have longer survival in <R>?", "S|C|<C> may increase survival in the mutated cases.",
    "S|R|Cases receiving <R> for a longer time show longer survival than the remaining cases.",
    "A|C|Consider the mutated cases for <C>.", "A|R|Test the high-risk patients on <R>.",
    "A|C|Examine <C> in the high-risk patients.", "S|C|<C> could be tested in the mutated cases.",
    "S|C|The mutated cases may be considered for <C>.", "S|C|<C> may be for the mutated cases.", "L|R|<R> → mutated cases",
    "S|R|For the mutated cases, regimen name: <R>.", "S|R|Patients on <R> could have it longer.",
    "S|R|Patients receiving <R> may improve with time.", "S|R|Patients receiving <R> may be stable.",
)
# The reported false positives that only describe the data, and the descriptive forms next to them.
DESCRIPTIVE_REGIMEN_TEXT: Final = (
    "S|R|Survival may differ between the remaining cases and <R>.", "S|R|Survival may differ for <R> versus the remaining cases.",
    "A|R|Compare survival of <R> with the remaining cases.", "S|R|<R> may differ from the others in ISS stage.",
    "S|R|<R> was the most common regimen among the cases.", "S|R|The mutated cases mostly received <R>.",
    "S|R|Cases assigned to <R> may be older.", "S|R|Cases with <R> may be older.", "S|R|Patients who had <R> may be older.",
    "S|R|Patients exposed to <R> may be older.", "S|R|Survival may be similar with and without <R>.",
    "S|R|Survival may differ between <R> and the others, with an advantage for neither.",
    "A|C|Repeat the ranking on <C> for comparison.", "F|C|Compare survival of <C> for each ISS stage",
    "F|C|Rank genes in <C> on the mutated cases", "L|R|<R> survival", "L|R|<R> vs the rest", "L|R|Survival: <R>",
    "S|R|Patients currently receiving <R> may be older.", "S|R|Cases receiving <R> may have more follow-up time.",
    "S|R|In <R>, survival does not appear to improve with age.",
    "S|R|The survival difference between <R> and the remaining cases is not adjusted for age, so it may not favour either.",
    "S|R|Survival in <R> does not differ from the remaining cases, so no group is favoured.",
    "S|R|Survival may be similar in <R> and the remaining cases, with no advantage for either.",
    "S|R|Survival curves for cases receiving <R> and the remaining cases cross, with no consistent advantage for either group.",
    "S|R|Cases receiving <R> and the remaining cases show no survival difference that favours either group.",
    "S|R|Cases receiving <R> may have longer survival than the remaining cases.",
    "S|R|Patients receiving <R> may have higher survival than the others.",
    "S|R|Cases receiving <R> whose reason for discontinuation is adverse event form a small subgroup.",
    "S|R|Cases receiving <R> had longer follow-up than the remaining cases.",
    "S|R|Within <R>, the mutated cases have shorter survival.", "S|C|Survival in <C> may be shorter for the mutated cases.",
    "S|R|Cases receiving <R> tend to be older, which may confound the survival comparison.",
    "S|R|Cases receiving <R> form a group defined by the plan's filter.",
    "S|R|Cases receiving <R> may differ from the remaining cases in age, which this comparison does not adjust for.",
    "Q|R|Does the survival gap between cases receiving <R> and the remaining cases persist after adjusting for age, or does it disappear?",
    "A|R|Compare progression-free survival between cases receiving <R> and the remaining cases.",
    "S|R|Cases with <R> as their regimen are older than the remaining cases.",
    "S|R|Cases whose first regimen was <R> are older than the remaining cases.",
    "S|R|Cases receiving <R> for a longer time show a similar survival curve.",
    "S|C|In <C>, {{f.s10.top.1.gene}} is mutated in the most cases.", "Q|C|Does the ranking in <C> differ for the mutated cases?",
    "A|C|Consider <C> as a separate group.", "A|C|Restrict <C> to the mutated cases and compare survival.",
    "S|T|Cases whose reason for discontinuation is completed planned therapy are older than the remaining cases.",
    "L|J|Survival by regimen", "S|J|Survival may differ by regimen name.",
)
# Reported as false positives but kept failing: each reads as a judgement of the regimen or as care, and a plainer
# descriptive wording passes (DESCRIPTIVE_REGIMEN_TEXT).
BORDERLINE_REGIMEN_TEXT: Final = (
    # "VRd" and "KRd" are regimen values this run does not use; "the remaining cases" in their place passes.
    "S|R|Survival may differ between VRd and <R>.", "S|R|Survival differs among <R>, KRd and the remaining cases.",
    # "<C> for the mutated cases" offers the regimen to the group; "the ranking in <C> … for the mutated cases" passes.
    "Q|C|Does <C> for the mutated cases show a different ranking?",
    # Monitoring and switching are care; "had longer follow-up" passes.
    "S|R|Patients receiving <R> may have been monitored for longer.",
    "S|R|Cases receiving <R> may differ in how often they switched regimens.",
    # Response, progression and adverse events are what a regimen is judged by, and ORION computes none of them.
    "S|R|Patients receiving <R> may differ in response rates.", "S|R|Response is not recorded for cases receiving <R> in this data.",
    "S|R|Cases receiving <R> have more progression events recorded than the remaining cases.",
    "S|R|Cases receiving <R> more often list adverse event as the reason for discontinuation.",
    # A negated benefit judgement still judges the regimen; only the forms that favour no group pass.
    "S|R|There is no sign of a survival advantage for cases receiving <R>.",
    "S|R|This comparison does not show whether cases receiving <R> do better.",
    # "choice" and "chosen" are the words of choosing a treatment; "defined by the plan's filter" passes.
    "S|R|The choice of comparison group may matter for cases receiving <R>.",
    # No comparison group: "longer" than whom reads as the benefit claim.
    "S|R|Survival may be longer in patients receiving <R>, though the groups are small.",
    # Whether the survival gap reflects the regimen is the efficacy question.
    "S|R|Cases receiving <R> may differ from the remaining cases in age, so the survival gap may not reflect the regimen.",
    # "best-" makes "best-suited" too; "treated" is one of CLINICAL_WORDS; a field value the run does not name keeps
    # its clinical word ("therapy").
    "S|C|<C> is the best-documented cohort in this run.", "S|R|Cases treated with <R> have shorter median survival than the remaining cases.",
    "S|R|Cases receiving <R> whose reason for discontinuation is completed planned therapy form a small subgroup.",
)


class VSecRegimenTextTest(unittest.TestCase):
    """V-SEC on text about a regimen (G-5): a run's regimen name is a name only where the text uses it as the name of
    a group, and a text that names one, any text of a run that names one, and any text with a treatment noun use only
    the plain descriptive words (lexicon.DESCRIPTIVE_WORDS), so a benefit, outcome, management or advice claim fails
    whatever its wording."""

    def test_the_fourth_review_bypasses_fail(self) -> None:
        for case in FOURTH_REVIEW_BYPASSES:
            with self.subTest(case=case):
                self.assertEqual(verdict(case), "V-SEC")

    def test_other_regimen_advice_fails(self) -> None:
        for case in OTHER_REGIMEN_ADVICE:
            with self.subTest(case=case):
                self.assertEqual(verdict(case), "V-SEC")

    def test_descriptive_regimen_text_passes(self) -> None:
        for case in DESCRIPTIVE_REGIMEN_TEXT:
            with self.subTest(case=case):
                self.assertIsNone(verdict(case))

    def test_borderline_regimen_text_stays_failing(self) -> None:
        for case in BORDERLINE_REGIMEN_TEXT:
            with self.subTest(case=case):
                self.assertEqual(verdict(case), "V-SEC")

    def test_every_text_of_a_regimen_run_is_read_as_about_the_regimen(self) -> None:
        """A pronoun carries the regimen to another statement: in the regimen's run "Consider it for the mutated cases"
        fails; in J3, which names no regimen, it is research wording."""
        text = "Consider it for the mutated cases."
        self.assertEqual(verdict(f"S|R|{text}"), "V-SEC")
        self.assertIsNone(verdict(f"S|J|{text}"))
        self.assertIsNone(verdict("S|R|{{f.s10.top.1.gene}} ranks first here."))

    def test_hidden_spellings_of_a_drug_are_read_as_written(self) -> None:
        """A zero-width space, a soft hyphen, a Cyrillic letter or fullwidth letters inside a drug name hid it from
        every rule; V-ENT, V-NUM and V-SEC now read the text as a reader sees it (lexicon.canonical_text)."""
        for text in ("Give bortez\u200bomib to the mutated cases.", "Give dexamethas\u043ene to the mutated cases.",
                     "Give ｌｅｎａｌｉｄｏｍｉｄｅ to the mutated cases.", "Give l\u00e9nalidomide to the mutated cases."):
            with self.subTest(text=text):
                self.assertEqual(verdict(f"S|J|{text}"), "V-SEC")
        self.assertEqual(verdict("S|J|BR\u0410F ranks first between the groups."), "V-NUM")


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

    def test_a_top_n_that_ends_the_sentence_is_still_the_one_top_n(self) -> None:
        """Seen live on Bedrock: "Show the top 6." was refused because the full stop read as a decimal point."""
        kept = [ComposerFollowUp(kind="top_mutated_genes", label="Top mutated genes", question="Which genes are mutated most often? Show the top 6.")]
        refused = [
            ComposerFollowUp(kind="top_mutated_genes", label="Top 6.5 genes", question="Show the top 6.5 genes."),
            ComposerFollowUp(kind="differential_expression", label="Top genes", question="Show the top 10. Then the top 20."),
        ]
        self.assertEqual([f.question for f in verify_j3(output(follow_ups=kept)).follow_ups], [kept[0].question])
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
