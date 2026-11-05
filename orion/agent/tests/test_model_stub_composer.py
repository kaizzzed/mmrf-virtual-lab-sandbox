"""The stub composer (mode L without Bedrock): J1, J2 and J3 reach Complete with four parts and
pass the verifier; hypotheses are questions; follow-ups come only from runnable kinds (C-06)."""

import unittest
from typing import Any

from orion_agent.catalog import ANALYSIS_APPS
from orion_agent.compiler import CompiledPlan
from orion_agent.interfaces import ObservedResult
from orion_agent.model.answer import write_answer
from orion_agent.model.calls import model_call_scope
from orion_agent.model.stub_composer import STUB_MODEL, StubComposer, stub_output
from orion_agent.model.verifier import CodeVerifier
from orion_agent.models import FactRecord
from support import vocabulary
from test_model_fixtures import genes_plan, j1_facts, j1_observed, j1_plan, j3_plan, last_view, view_facts, view_observed
from test_model_mock import RUN_ID, run

J3 = "j3-after-confirm.events.jsonl"
ONCO = "oncomatrix.events.jsonl"


def journeys() -> list[tuple[str, CompiledPlan, list[FactRecord], ObservedResult]]:
    return [
        ("J1", j1_plan(), j1_facts(), j1_observed()),
        ("J2", genes_plan(), view_facts(last_view(ONCO)), view_observed(last_view(ONCO))),
        ("J3", j3_plan(), view_facts(last_view(J3)), view_observed(last_view(J3))),
    ]


class StubComposerTest(unittest.TestCase):
    def answer(self, plan: CompiledPlan, facts: list[FactRecord], observed: ObservedResult) -> Any:
        async def call() -> Any:
            with model_call_scope(RUN_ID):
                return await write_answer(
                    StubComposer(), CodeVerifier(), question="stub", facts=facts, plan=plan, observed=observed, ledger=None, vocab=vocabulary()
                )

        return run(call())

    def test_each_journey_completes_with_four_parts_and_nothing_dropped(self) -> None:
        for name, plan, facts, observed in journeys():
            with self.subTest(journey=name):
                outcome = self.answer(plan, facts, observed)
                self.assertEqual((outcome.status, outcome.withheld_reason), ("Complete", None))
                self.assertTrue(observed.observed and observed.limitations)
                self.assertTrue(outcome.interpretation and all(s.dropped is None and s.cites for s in outcome.interpretation))
                self.assertTrue(outcome.hypotheses and all(s.dropped is None for s in outcome.hypotheses))
                self.assertTrue(outcome.follow_ups and outcome.open_links)
                [verdict] = outcome.verifier_results
                self.assertTrue(verdict.passed, [f for f in verdict.findings if not f.passed])
                self.assertEqual((outcome.inserted_observed, outcome.inserted_limitations), ([], []))

    def test_numbers_and_genes_reach_the_screen_only_as_fact_segments(self) -> None:
        for name, plan, facts, observed in journeys():
            with self.subTest(journey=name):
                outcome = self.answer(plan, facts, observed)
                for sentence in [*(outcome.interpretation or []), *(outcome.hypotheses or [])]:
                    for segment in sentence.segments:
                        if segment.fact_id is None:
                            self.assertFalse(any(ch.isdigit() for ch in segment.text.replace("(ORION)", "")), segment.text)

    def test_hypotheses_are_questions_and_follow_ups_runnable_kinds(self) -> None:
        for name, plan, facts, observed in journeys():
            with self.subTest(journey=name):
                output = stub_output(plan, facts, observed)
                assert output is not None
                self.assertTrue(all(h.form == "question" and h.text.endswith("?") for h in output.hypotheses))
                self.assertTrue({f.kind for f in output.follow_ups} <= set(ANALYSIS_APPS))

    def test_the_output_is_deterministic(self) -> None:
        for name, plan, facts, observed in journeys():
            with self.subTest(journey=name):
                self.assertEqual(stub_output(plan, facts, observed), stub_output(plan, facts, observed))

    def test_j3_names_the_direction_and_the_read_back_cohorts(self) -> None:
        _, plan, facts, observed = journeys()[2]
        outcome = self.answer(plan, facts, observed)
        assert outcome.interpretation is not None
        first = "".join(s.text for s in outcome.interpretation[0].segments)
        self.assertEqual(
            first, "In this synthetic dataset, BRAF has the smallest adjusted p-value between the groups, with higher expression in "
            "KRAS mutated (ORION) than in KRAS not detected (ORION).",
        )
        self.assertEqual([f.label for f in outcome.follow_ups], ["Show the top 20", "Survival by KRAS status"])

    def test_the_serving_model_is_recorded_as_stub(self) -> None:
        _, plan, facts, observed = journeys()[0]

        async def call() -> Any:
            with model_call_scope(RUN_ID) as scope:
                result = await StubComposer().compose("stub", facts, plan, observed, allow_list=[], ledger=None)
                return result, scope.records

        result, records = run(call())
        self.assertEqual(result.serving_model, STUB_MODEL)
        [record] = records
        self.assertEqual((record.call_id, record.model, record.outcome, record.input_tokens), (f"{RUN_ID}.v1.composer.c1", "stub", "ok", None))
        self.assertEqual(result.model_calls, records)

    def test_no_facts_declines(self) -> None:
        result = run(StubComposer().compose("stub", [], j1_plan(), j1_observed(), allow_list=[], ledger=None))
        self.assertTrue(result.declined)
        self.assertEqual(result.model_calls, [])

    def test_warm_up_is_a_no_op(self) -> None:
        self.assertEqual(run(StubComposer().warm_up()).outcome, "skipped")


if __name__ == "__main__":
    unittest.main()
