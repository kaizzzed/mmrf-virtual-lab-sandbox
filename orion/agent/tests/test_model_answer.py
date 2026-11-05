"""The answer loop: compose, verify, one re-compose with feedback, the Interpretation-present gate
(C-02) and the Observed-only endings (E11)."""

import unittest
from collections.abc import Sequence
from typing import Any

from orion_agent.compiler import CompiledPlan, SessionLedger
from orion_agent.interfaces import ComposerOutput, ComposerResult, ObservedResult, RuleFinding
from orion_agent.model.answer import WITHHELD_UNVERIFIED, write_answer
from orion_agent.model.errors import ModelRoleError
from orion_agent.model.factory import RefusingComposer
from orion_agent.model.verifier import CodeVerifier
from orion_agent.models import FactRecord
from support import vocabulary
from test_model_fixtures import fixture_model_output, j3_plan, last_view, placeholder_text, view_facts, view_observed
from test_model_mock import run
from test_verifier import output, statement

J3 = "j3-after-confirm.events.jsonl"
GOOD = fixture_model_output(last_view(J3))
BAD = output([statement("The two groups differ.")])


class ScriptedComposer:
    def __init__(self, *replies: ComposerOutput | ModelRoleError | str) -> None:
        self.replies = list(replies)
        self.feedback: list[Sequence[RuleFinding]] = []

    async def compose(
        self,
        question: str,
        facts: Sequence[FactRecord],
        plan: CompiledPlan,
        observed: ObservedResult,
        *,
        allow_list: Sequence[str],
        ledger: SessionLedger | None,
        feedback: Sequence[RuleFinding] = (),
    ) -> ComposerResult:
        self.feedback.append(feedback)
        reply = self.replies.pop(0)
        if isinstance(reply, ModelRoleError):
            raise reply
        if isinstance(reply, str):
            return ComposerResult(output=None, model_calls=[], serving_model=None, declined=True, decline_reason=reply)
        return ComposerResult(output=reply, model_calls=[], serving_model="model", declined=False, decline_reason=None)


def answer(composer: Any, observed: ObservedResult | None = None) -> Any:
    view = last_view(J3)
    return run(
        write_answer(
            composer, CodeVerifier(), question=view.question, facts=view_facts(view), plan=j3_plan(),
            observed=observed or view_observed(view), ledger=None, vocab=vocabulary(),
        )
    )


class WriteAnswerTest(unittest.TestCase):
    def test_a_clean_answer_completes_on_the_first_attempt(self) -> None:
        composer = ScriptedComposer(GOOD)
        outcome = answer(composer)
        self.assertEqual((outcome.status, outcome.code, outcome.withheld_reason), ("Complete", None, None))
        self.assertEqual(len(outcome.verifier_results), 1)
        self.assertEqual(len(composer.feedback), 1)
        self.assertEqual(outcome.follow_ups, last_view(J3).follow_ups)

    def test_a_failure_re_composes_once_with_the_findings(self) -> None:
        composer = ScriptedComposer(BAD, GOOD)
        outcome = answer(composer)
        self.assertEqual(outcome.status, "Complete")
        self.assertEqual(len(outcome.verifier_results), 2)
        [feedback] = composer.feedback[1]
        self.assertEqual((feedback.rule, feedback.part, feedback.passed), ("V-NUM", "interpretation", False))

    def test_a_second_failure_drops_the_sentence_and_withholds_when_nothing_survives(self) -> None:
        outcome = answer(ScriptedComposer(BAD, BAD))
        self.assertEqual((outcome.status, outcome.withheld_reason), ("ObservedOnly", WITHHELD_UNVERIFIED))
        self.assertIsNone(outcome.interpretation)
        self.assertEqual(outcome.follow_ups, [])

    def test_a_partly_failing_second_attempt_keeps_the_marker(self) -> None:
        mixed = output([statement("The two groups differ."), statement("{{f.s10.top.1.gene}} ranks first.")])
        outcome = answer(ScriptedComposer(BAD, mixed))
        self.assertEqual(outcome.status, "Complete")
        self.assertEqual([s.dropped for s in outcome.interpretation], ["V-NUM", None])

    def test_a_declined_re_compose_keeps_the_first_verdict(self) -> None:
        mixed = output([statement("The two groups differ."), statement("{{f.s10.top.1.gene}} ranks first.")])
        outcome = answer(ScriptedComposer(mixed, "both models declined"))
        self.assertEqual(outcome.status, "Complete")
        self.assertEqual([s.dropped for s in outcome.interpretation], ["V-NUM", None])
        self.assertEqual(len(outcome.verifier_results), 1)

    def test_both_models_declining_is_observed_only_with_model_declined(self) -> None:
        outcome = answer(RefusingComposer())
        self.assertEqual((outcome.status, outcome.code), ("ObservedOnly", "MODEL_DECLINED"))
        self.assertTrue(outcome.withheld_reason.startswith("both models declined"))
        [line] = outcome.inserted_limitations
        self.assertEqual(line.code, "MODEL_DECLINED")
        self.assertIsNone(outcome.interpretation)

    def test_a_composer_error_is_observed_only_with_its_reason(self) -> None:
        outcome = answer(ScriptedComposer(ModelRoleError("composer", "timeout", "slow")))
        self.assertEqual((outcome.status, outcome.code), ("ObservedOnly", None))
        self.assertEqual(outcome.withheld_reason, "the composer model did not answer in time")

    def test_code_insertions_apply_when_the_model_text_is_withheld(self) -> None:
        view = last_view(J3)
        observed = view_observed(view).model_copy(
            update={"observed": [s for s in view.observed if not placeholder_text(s).startswith("Method:")], "limitations": []}
        )
        outcome = answer(RefusingComposer(), observed)
        self.assertEqual(len(outcome.inserted_observed), 1)
        self.assertEqual([lim.code for lim in outcome.inserted_limitations], ["PANEL_COVERAGE", "MODEL_DECLINED"])


if __name__ == "__main__":
    unittest.main()
