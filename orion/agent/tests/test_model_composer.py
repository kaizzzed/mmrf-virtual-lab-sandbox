"""The Bedrock composer over a scripted Bedrock: structured parsing and clipping, the refusal
fallback, a refusal from both models (E11), feedback on the re-compose, error mapping."""

import json
import unittest
from typing import Any

from orion_agent.interfaces import ComposerOutput, RuleFinding
from test_model_fixtures import fixture_model_output, j1_facts, j1_observed, j1_plan, last_view
from test_model_mock import FALLBACK, HAS_SDK, PRIMARY, RUN_ID, SKIP_REASON, ScriptedBedrock, config, message, refusal, run

QUESTION = "Compare survival of CGS high-risk vs. CGS standard-risk patients"


def j1_output() -> dict[str, Any]:
    return fixture_model_output(last_view("j1-complete.events.jsonl")).model_dump(mode="json")


@unittest.skipUnless(HAS_SDK, SKIP_REASON)
class BedrockComposerTest(unittest.TestCase):
    def composer(self, bedrock: ScriptedBedrock, **values: Any) -> Any:
        from orion_agent.model.composer import BedrockComposer

        return BedrockComposer(bedrock.client(max_retries=values.pop("max_retries", 0)), config(**values))

    def compose(self, composer: Any, feedback: tuple[RuleFinding, ...] = ()) -> Any:
        from orion_agent.model.calls import model_call_scope

        async def call() -> Any:
            with model_call_scope(RUN_ID):
                return await composer.compose(
                    QUESTION, j1_facts(), j1_plan(), j1_observed(), allow_list=["Gen3 Analysis", "log2FC"], ledger=None, feedback=feedback
                )

        return run(call())

    def test_structured_output_parses_with_a_composer_call_id(self) -> None:
        bedrock = ScriptedBedrock({PRIMARY: [message(PRIMARY, json.dumps(j1_output()))]})
        result = self.compose(self.composer(bedrock))
        self.assertIsInstance(result.output, ComposerOutput)
        self.assertFalse(result.declined)
        [record] = result.model_calls
        self.assertEqual(record.call_id, f"{RUN_ID}.v1.composer.c1")
        self.assertEqual((record.role, record.effort, record.outcome, record.cache_read_tokens), ("composer", "medium", "ok", 7120))

    def test_the_request_carries_the_allow_list_facts_and_observed(self) -> None:
        from orion_agent.model.schema import composer_output_schema

        bedrock = ScriptedBedrock({PRIMARY: [message(PRIMARY, json.dumps(j1_output()))]})
        composer = self.composer(bedrock)
        self.compose(composer)
        [body] = bedrock.requests
        self.assertEqual(body["output_config"], {"effort": "medium", "format": {"type": "json_schema", "schema": composer_output_schema()}})
        self.assertEqual(body["system"][0]["cache_control"]["type"], "ephemeral")
        self.assertEqual(body["system"][0]["text"], composer.system_prefix)
        for absent in ("temperature", "top_p", "top_k", "tool_choice", "tools", "thinking"):
            self.assertNotIn(absent, body)
        data = json.loads(body["messages"][0]["content"].split("<request>\n", 1)[1].split("\n</request>", 1)[0])
        self.assertEqual(data["allowedTerms"], ["Gen3 Analysis", "log2FC"])
        self.assertIn({"id": "f.s4.p", "label": "Log-rank p", "value": "7.1e-60", "unit": "p", "step": "s4"}, data["facts"])
        self.assertIn("Log-rank p = {{f.s4.p}} (Gen3 Analysis).", data["observed"])
        self.assertNotIn("feedback", data)

    def test_the_re_compose_carries_the_failed_findings(self) -> None:
        bedrock = ScriptedBedrock({PRIMARY: [message(PRIMARY, json.dumps(j1_output()))]})
        finding = RuleFinding(rule="V-NUM", part="interpretation", index=0, passed=False, detail="two is a number word")
        self.compose(self.composer(bedrock), (finding,))
        content = bedrock.requests[0]["messages"][0]["content"]
        self.assertIn('"rule": "V-NUM"', content)
        self.assertIn("broke the rules listed in feedback", content)

    def test_over_long_items_are_clipped_not_fatal(self) -> None:
        output = j1_output()
        output["interpretation"] = output["interpretation"] * 3 + [{"text": "x" * 301, "cites": ["f.s4.p"]}]
        output["hypotheses"].append({"text": "Is it?", "cites": ["not a fact id"], "form": "question"})
        output["follow_ups"].append({"kind": "cohort_size", "label": "L" * 61, "question": "Too long a label"})
        result = self.compose(self.composer(ScriptedBedrock({PRIMARY: [message(PRIMARY, json.dumps(output))]})))
        self.assertEqual(len(result.output.interpretation), 4)
        self.assertEqual(len(result.output.hypotheses), 1)
        self.assertEqual(len(result.output.follow_ups), 2)

    def test_a_refusal_falls_back_to_opus_5(self) -> None:
        bedrock = ScriptedBedrock({PRIMARY: [refusal(PRIMARY)], FALLBACK: [message(FALLBACK, json.dumps(j1_output()))]})
        result = self.compose(self.composer(bedrock))
        self.assertFalse(result.declined)
        self.assertEqual(result.serving_model, FALLBACK)
        self.assertEqual([(c.model, c.fallback, c.outcome) for c in result.model_calls], [(PRIMARY, False, "refused"), (FALLBACK, True, "ok")])

    def test_a_refusal_from_both_models_declines(self) -> None:
        bedrock = ScriptedBedrock({PRIMARY: [refusal(PRIMARY)], FALLBACK: [refusal(FALLBACK)]})
        result = self.compose(self.composer(bedrock))
        self.assertIsNone(result.output)
        self.assertTrue(result.declined)
        self.assertEqual(result.decline_reason, "Claude Opus 5.5 and its fallback, Claude Opus 5, declined to interpret these results")
        self.assertEqual([c.call_id[-2:] for c in result.model_calls], ["c1", "c2"])

    def test_a_refusal_whose_fallback_call_fails_declines(self) -> None:
        """sdk-1: the refusal is kept when the fallback call fails, so the answer is MODEL_DECLINED, not an error."""
        for status in (403, 500):
            with self.subTest(status=status):
                bedrock = ScriptedBedrock({PRIMARY: [refusal(PRIMARY)], FALLBACK: [status]})
                result = self.compose(self.composer(bedrock))
                self.assertIsNone(result.output)
                self.assertTrue(result.declined)
                self.assertEqual(result.decline_reason,
                                 "Claude Opus 5.5 declined to interpret these results, and the fallback model could not be reached")
                self.assertEqual([(c.model, c.fallback, c.outcome) for c in result.model_calls],
                                 [(PRIMARY, False, "refused"), (FALLBACK, True, "error")])

    def test_an_answer_that_does_not_validate_declines(self) -> None:
        bedrock = ScriptedBedrock({PRIMARY: [message(PRIMARY, json.dumps({"interpretation": []}))]})
        result = self.compose(self.composer(bedrock))
        self.assertTrue(result.declined)
        self.assertEqual([c.outcome for c in result.model_calls], ["invalid"])

    def test_errors_raise_with_their_records(self) -> None:
        import httpx2

        from orion_agent.model.errors import ModelRoleError

        for reply, kind in ((429, "rate_limited"), (httpx2.ReadTimeout("slow"), "timeout")):
            with self.subTest(kind=kind), self.assertRaises(ModelRoleError) as raised:
                self.compose(self.composer(ScriptedBedrock({PRIMARY: [reply]})))
            self.assertEqual((raised.exception.code, raised.exception.kind), ("INTERNAL_ERROR", kind))
            self.assertEqual([c.outcome for c in raised.exception.model_calls], ["error"])

    def test_warm_up_runs_at_the_composer_effort(self) -> None:
        bedrock = ScriptedBedrock({PRIMARY: [message(PRIMARY, json.dumps(j1_output()), cache_write=5000)]})
        result = run(self.composer(bedrock).warm_up())
        self.assertEqual((result.role, result.effort, result.outcome, result.cache_write_tokens), ("composer", "medium", "ok", 5000))
        self.assertEqual(bedrock.requests[0]["output_config"]["effort"], "medium")


if __name__ == "__main__":
    unittest.main()
