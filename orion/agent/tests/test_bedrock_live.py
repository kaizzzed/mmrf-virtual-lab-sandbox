"""Stage 2, "Bedrock only", live and opt-in: the real planner and composer, Claude Opus 5.5 on Amazon Bedrock.

Skipped unless ORION_LIVE_BEDROCK=1, so the unit suite stays offline and makes no model call. Every
test here calls Bedrock and costs money. It needs short-term AWS credentials in the environment
(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN: pass them to `docker run` by name with
`-e`, never by value) and runs against the inference profiles of model/config.py in us-east-1.

- SmokeTests: one planner call on J1, whose plan must compile to the stub planner's signature; the
  composer, verifier and one re-compose (`answer.write_answer`) on the recorded J1 Observed of the
  contract fixture, which must end Complete; and each role's warm-up.
- JourneyTests (needs the live stack, ORION_VLAB_ORIGIN): J1 to Complete through POST /invocations.
  With ORION_LIVE_BEDROCK_JOURNEYS=all: J1 in both phrasings, then J2 and J3 in both phrasings,
  each through its save pause and a simulated browser's Create, to Complete. Each run must sign
  the stub planner's plan, end Complete with an Interpretation (not Observed only) whose every
  cited fact is a fact of the run, drop no sentence, report the stack's own numbers (raw HTTP in
  test_e2e_live.Upstream), and record the bedrock planner and composer with their
  ModelCallRecords on the us.* models.

Turns go to an in-process app (`app.build_controller` with ORION_PLANNER and ORION_COMPOSER
bedrock, records in a temporary directory) or, with ORION_LIVE_AGENT_URL set (for example
http://orion-agent:8080, the Compose agent of `orion-local.sh up --bedrock`), to that agent over
HTTP, reading its records from ORION_LIVE_RUN_DIR (the volume mmrf-synthetic_orion-runs mounted
read-only). From the sandbox root, after `orion-local.sh up --bedrock`:

    eval "$(aws configure export-credentials --profile sandbox --format env)"
    docker run --rm --network mmrf-synthetic_default -v $PWD/orion/agent:/app -v $PWD/config:/config:ro \\
      -v $PWD/orion/contracts:/contracts:ro -v mmrf-synthetic_orion-runs:/runs:ro \\
      -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN -e AWS_REGION=us-east-1 \\
      -e ORION_LIVE_BEDROCK=1 -e ORION_VLAB_ORIGIN=http://identity:8080 \\
      -e ORION_LIVE_AGENT_URL=http://orion-agent:8080 -e ORION_LIVE_RUN_DIR=/runs \\
      -e PYTHONPATH=/app/tests:/app -w /app/tests orion-agent:dev python -m unittest test_bedrock_live -v

Leave out the two ORION_LIVE_AGENT_URL / ORION_LIVE_RUN_DIR settings to run the app in-process.
"""

import asyncio
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import JsonValue
from support import FIELDS_PATH, action_body, ask_body, asgi_call, check_stream, context
from test_e2e_live import CGS, J2_NAMED, J2_OPEN, J3_NAMED, J3_OPEN, KRAS_GENE_ID, ORIGIN, RACE, SimulatedBrowser, Upstream, stack_reachable
from test_engine_fakes import stream_frames

from orion_agent import controller as controller_module
from orion_agent import ir
from orion_agent.compiler import CompiledPlan, Compiler
from orion_agent.ids import new_id
from orion_agent.models import CohortContext, ConfirmPending, ModelCallRecord, RunView, SaveCohortsArgs
from orion_agent.run_store import RunRecord
from orion_agent.settings import Settings
from orion_agent.stubs import J1_NAMED, J1_OPEN, plan_question
from orion_agent.vocab import Vocabulary

LIVE = os.environ.get("ORION_LIVE_BEDROCK") == "1"
ALL_JOURNEYS = os.environ.get("ORION_LIVE_BEDROCK_JOURNEYS", "") == "all"
AGENT_URL = os.environ.get("ORION_LIVE_AGENT_URL", "")
RUN_DIR = os.environ.get("ORION_LIVE_RUN_DIR", "")
HAS_SDK = importlib.util.find_spec("anthropic") is not None
SKIP_REASON = "set ORION_LIVE_BEDROCK=1 (with short-term AWS credentials) to call Bedrock"
PRIMARY = "us.anthropic.claude-opus-5-5"
FALLBACK = "us.anthropic.claude-opus-5"
RUN_ID = "0123456789abcdef0123456789abcdef"
TURN_TIMEOUT_S = 600.0


def log(message: str) -> None:
    print(f"[bedrock-live] {message}", file=sys.stderr, flush=True)


async def close_shared_clients() -> None:
    """Close the cached SDK clients in the loop that made them: each test runs its own event loop."""
    from orion_agent.model import client

    for shared in list(client._SHARED.values()):
        await shared.sdk.close()
    client._SHARED.clear()


def stub_signature(question: str, cohort_context: CohortContext) -> str:
    """The plan the stub planner signs for the question (the stage 1 baseline), compiled offline."""
    output = plan_question(question, cohort_context, None)
    assert isinstance(output, ir.Plan), f"the stub planner does not plan {question!r}"
    compiled = Compiler(Vocabulary.load(FIELDS_PATH), Settings().default_scope).compile(output, cohort_context, None)
    assert isinstance(compiled, CompiledPlan), compiled
    return compiled.signature


def check_model_calls(case: unittest.TestCase, calls: Sequence[ModelCallRecord], role: str, effort: str) -> list[ModelCallRecord]:
    """The role's calls ran on the us.* inference profiles, at its effort, with tokens on every answered call."""
    mine = [c for c in calls if c.role == role]
    case.assertTrue(mine, f"no {role} ModelCallRecord")
    for call in mine:
        case.assertIn(call.model, (PRIMARY, FALLBACK), call.call_id)
        case.assertEqual(call.fallback, call.model == FALLBACK, call.call_id)
        case.assertEqual(call.effort, effort, call.call_id)
        if call.outcome in ("ok", "invalid", "refused"):
            case.assertIsNotNone(call.stop_reason, call.call_id)
            case.assertIsNotNone(call.input_tokens, call.call_id)
            case.assertIsNotNone(call.output_tokens, call.call_id)
    case.assertIn(mine[-1].outcome, ("ok",), f"the last {role} call answered")
    return mine


@unittest.skipUnless(LIVE and HAS_SDK, SKIP_REASON)
class SmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from orion_agent.model import client

        client._SHARED.clear()  # one SDK client per event loop: each test runs its own loop
        from orion_agent.model.config import ModelConfig

        self.config = ModelConfig.from_env().model_copy(update={"planner": "bedrock", "composer": "bedrock"})
        self.vocab = Vocabulary.load(FIELDS_PATH)

    async def asyncTearDown(self) -> None:
        await close_shared_clients()

    async def test_the_planner_plans_j1_as_the_stub_does(self) -> None:
        from orion_agent.model.calls import model_call_scope
        from orion_agent.model.client import shared_client
        from orion_agent.model.planner import BedrockPlanner

        planner = BedrockPlanner(shared_client(self.config), self.config, self.vocab)
        started = time.monotonic()
        with model_call_scope(RUN_ID):
            result = await planner.plan(J1_OPEN, context(), None)
        log(f"planner J1: {time.monotonic() - started:.1f}s, calls {[c.model_dump(mode='json', by_alias=True) for c in result.model_calls]}")
        self.assertFalse(result.declined, result.decline_reason)
        assert isinstance(result.output, ir.Plan), result.output
        compiled = Compiler(self.vocab, Settings().default_scope).compile(result.output, context(), None)
        assert isinstance(compiled, CompiledPlan), compiled
        self.assertEqual(compiled.signature, stub_signature(J1_OPEN, context()), "the Bedrock planner signs the stub's J1 plan")
        [call] = check_model_calls(self, result.model_calls, "planner", self.config.planner_effort)
        self.assertEqual((call.call_id, call.model, call.attempt), (f"{RUN_ID}.planner.c1", PRIMARY, 1))

    async def test_the_composer_writes_a_verified_interpretation_of_the_recorded_j1(self) -> None:
        from test_model_fixtures import j1_facts, j1_observed, j1_plan

        from orion_agent.model.answer import write_answer
        from orion_agent.model.calls import model_call_scope
        from orion_agent.model.client import shared_client
        from orion_agent.model.composer import BedrockComposer
        from orion_agent.model.verifier import CodeVerifier

        composer = BedrockComposer(shared_client(self.config), self.config)
        started = time.monotonic()
        with model_call_scope(RUN_ID):
            outcome = await write_answer(
                composer, CodeVerifier(), question=J1_OPEN, facts=j1_facts(), plan=j1_plan(), observed=j1_observed(), ledger=None,
                vocab=self.vocab,
            )
        failed = [f"{f.rule} {f.part}[{f.index}]: {f.detail}" for r in outcome.verifier_results for f in r.findings if not f.passed]
        log(f"composer J1: {time.monotonic() - started:.1f}s, {len(outcome.model_calls)} call(s), findings {failed}")
        self.assertEqual((outcome.status, outcome.withheld_reason), ("Complete", None))
        assert outcome.interpretation is not None
        kept = [s for s in outcome.interpretation if s.dropped is None]
        self.assertTrue(kept, "an Interpretation sentence passed verification")
        facts = {f.id for f in j1_facts()}
        for sentence in [*kept, *(outcome.hypotheses or [])]:
            self.assertLessEqual({s.fact_id for s in sentence.segments if s.fact_id}, facts)
        calls = check_model_calls(self, outcome.model_calls, "composer", self.config.composer_effort)
        self.assertLessEqual(len(calls), 2, "one compose and at most one re-compose")
        self.assertEqual(calls[0].call_id, f"{RUN_ID}.v1.composer.c1")

    async def test_each_role_warms_its_cached_prefix(self) -> None:
        from orion_agent.model.client import shared_client
        from orion_agent.model.composer import BedrockComposer
        from orion_agent.model.planner import BedrockPlanner
        from orion_agent.model.warmup import warm_up_roles

        client = shared_client(self.config)
        results = await warm_up_roles(BedrockPlanner(client, self.config, self.vocab), BedrockComposer(client, self.config))
        log(f"warm-up: {[(r.role, r.outcome, r.elapsed_ms, r.cache_read_tokens, r.cache_write_tokens) for r in results]}")
        self.assertEqual([(r.role, r.outcome, r.model) for r in results], [("planner", "ok", PRIMARY), ("composer", "ok", PRIMARY)])
        for result in results:
            self.assertGreater((result.cache_read_tokens or 0) + (result.cache_write_tokens or 0), 0, f"{result.role} cached its prefix")


@unittest.skipUnless(LIVE and HAS_SDK, SKIP_REASON)
@unittest.skipUnless(stack_reachable(), f"the local stack is not reachable at ORION_VLAB_ORIGIN={ORIGIN or '(unset)'}")
class JourneyTests(unittest.IsolatedAsyncioTestCase):
    """J1 (and with ORION_LIVE_BEDROCK_JOURNEYS=all, J2 and J3) to Complete with the Bedrock roles."""

    def setUp(self) -> None:
        from orion_agent.model import client

        client._SHARED.clear()
        controller_module.reset_registry()
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(RUN_DIR) if AGENT_URL else Path(self._tmp.name)
        self.upstream = Upstream(ORIGIN)
        self.browser = SimulatedBrowser()
        self.contexts: dict[str, CohortContext] = {}  # the cohort context each run was asked with
        self.app: Any = None
        if not AGENT_URL:
            from orion_agent.app import build_controller, create_app

            settings = Settings(run_dir=self.run_dir, fields_path=FIELDS_PATH, vlab_origin=ORIGIN, planner="bedrock", composer="bedrock")
            self.app = create_app(build_controller(settings))
        log(f"turns go to {AGENT_URL or 'an in-process app'}; records in {self.run_dir}")

    async def asyncTearDown(self) -> None:
        for handle in list(controller_module.RUNS.values()):
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()
        controller_module.reset_registry()
        await close_shared_clients()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def turn(self, body: dict[str, JsonValue], run_id: str) -> list[dict[str, Any]]:  # Any: frames are JSON
        started = time.monotonic()
        if AGENT_URL:
            status, text = await asyncio.to_thread(self._post, body)
        else:
            response = await asgi_call(self.app, "POST", "/invocations", body)
            status, text = response.status, response.text
        self.assertEqual(status, 200)
        check_stream(self, text, expect_run_id=run_id)
        frames = stream_frames(text)
        log(f"turn {run_id[:8]}: {time.monotonic() - started:.1f}s, ends {frames[-1]['type']}")
        return frames

    def _post(self, body: dict[str, JsonValue]) -> tuple[int, str]:
        parts = urllib.parse.urlsplit(AGENT_URL)
        conn = http.client.HTTPConnection(parts.hostname or "", parts.port or 80, timeout=TURN_TIMEOUT_S)
        try:
            conn.request("POST", "/invocations", body=json.dumps(body).encode(),
                         headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            conn.close()

    def ask(self, thread_id: str, run_id: str, question: str) -> dict[str, JsonValue]:
        body = ask_body(thread_id, run_id, question=question)
        props = body["forwardedProps"]
        assert isinstance(props, dict) and isinstance(props["orion"], dict)
        props["orion"]["cohortContext"] = self.browser.context()
        self.contexts[run_id] = CohortContext.model_validate(props["orion"]["cohortContext"])
        return body

    def view(self, frames: Sequence[Mapping[str, Any]]) -> RunView:
        return RunView.model_validate([f for f in frames if f["type"] == "STATE_SNAPSHOT"][-1]["snapshot"])

    def pending(self, frames: Sequence[Mapping[str, Any]]) -> ConfirmPending:
        args = SaveCohortsArgs.model_validate_json("".join(f["delta"] for f in frames if f["type"] == "TOOL_CALL_ARGS"))
        return ConfirmPending.model_validate(args.model_dump(mode="json"))

    def facts(self, view: RunView) -> dict[str, JsonValue]:
        return {f.id: f.value for f in view.facts}

    def record(self, run_id: str) -> RunRecord:
        path = self.run_dir / "runs" / f"{run_id}.json"
        self.assertTrue(path.is_file(), f"no run record at {path}")
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def assertBedrockComplete(self, frames: Sequence[Mapping[str, Any]], question: str, launcher: str) -> RunView:
        """Complete with an Interpretation, every cited fact a fact of the run, no dropped sentence, the stub's
        plan signature, and a record naming the bedrock roles and their calls on the us.* models."""
        view = self.view(frames)
        record = self.record(view.run_id)
        for part, lines in (("interpretation", view.interpretation or []), ("hypothesis", view.hypotheses or [])):
            for line in lines:
                log(f"  {part}: " + ("".join(f"{{{{{s.fact_id}}}}}" if s.fact_id else s.text for s in line.segments) or f"(dropped {line.dropped})"))
        log(f"  model calls: {[(c.call_id.split('.', 1)[1], c.model, c.outcome, c.elapsed_ms, c.input_tokens, c.output_tokens, c.cache_read_tokens, c.cache_write_tokens) for c in record.model_calls]}")
        self.assertEqual((view.status, view.code, view.withheld_reason, frames[-1]["type"]), ("Complete", None, None, "RUN_FINISHED"))
        assert view.plan is not None and view.interpretation is not None and view.hypotheses is not None
        self.assertEqual(view.plan.signature, stub_signature(question, self.contexts[view.run_id]), "the Bedrock planner signs the stub's plan")
        self.assertTrue(view.observed and view.limitations, "Observed and Limitations")
        self.assertTrue(view.interpretation, "Interpretation")
        dropped = [s.dropped for s in (*view.interpretation, *view.hypotheses) if s.dropped is not None]
        self.assertEqual(dropped, [], "the verifier dropped no sentence")
        self.assertTrue(view.follow_ups or view.open_links, "next steps")
        self.assertEqual([v.launcher for v in view.viz_specs], [launcher])
        facts = set(self.facts(view))
        for sentence in (*view.observed, *view.interpretation, *view.hypotheses):
            cited = {s.fact_id for s in sentence.segments if s.fact_id}
            self.assertLessEqual(cited, facts, "every number on screen is a fact of the run")
        for sentence in (*view.interpretation, *view.hypotheses):
            # V-REF as the contract states it: every model sentence cites at least one fact of the run, and
            # cites every fact it places.
            self.assertTrue(sentence.cites, "each model sentence cites a fact")
            assert sentence.cites is not None
            self.assertLessEqual(set(sentence.cites), facts)
            self.assertLessEqual({s.fact_id for s in sentence.segments if s.fact_id}, set(sentence.cites))
        message = "".join(f["delta"] for f in frames if f["type"] == "TEXT_MESSAGE_CONTENT")
        self.assertIn("**Interpretation**", message)
        self.assertEqual((record.status, record.revision), (view.status, view.revision))
        self.assertEqual((record.environment.planner, record.environment.composer), ("bedrock", "bedrock"))
        check_model_calls(self, record.model_calls, "planner", "low")
        composer = check_model_calls(self, record.model_calls, "composer", "medium")
        self.assertLessEqual(len(composer), 2, "one compose and at most one re-compose")
        return view

    async def test_j1_reaches_complete_with_the_bedrock_roles(self) -> None:
        """E1, E2 on Bedrock: the stub's signature, the stack's n, events and log-rank p, a verified Interpretation."""
        thread_id = new_id()
        high = await asyncio.to_thread(self.upstream.case_ids, (CGS, ["High risk"]))
        standard = await asyncio.to_thread(self.upstream.case_ids, (CGS, ["Standard risk"]))
        events = (await asyncio.to_thread(self.upstream.events, CGS, "High risk"),
                  await asyncio.to_thread(self.upstream.events, CGS, "Standard risk"))
        p_value = await asyncio.to_thread(self.upstream.survival_p, CGS, ["High risk", "Standard risk"])
        for question in (J1_OPEN, J1_NAMED) if ALL_JOURNEYS else (J1_OPEN,):
            run_id = new_id()
            frames = await self.turn(self.ask(thread_id, run_id, question), run_id)
            view = self.assertBedrockComplete(frames, question, "survival")
            facts = self.facts(view)
            self.assertEqual((facts["f.s2.count"], facts["f.s3.count"]), (len(high), len(standard)))
            self.assertEqual((facts["f.s4.events.high"], facts["f.s4.events.standard"]), events)
            self.assertEqual(facts["f.s4.p"], p_value)

    @unittest.skipUnless(ALL_JOURNEYS, "J2 runs with ORION_LIVE_BEDROCK_JOURNEYS=all")
    async def test_j2_pauses_for_one_cohort_then_completes_after_create(self) -> None:
        members = await asyncio.to_thread(self.upstream.case_ids, (RACE, ["black or african american"]))
        kras = len(await asyncio.to_thread(self.upstream.gene_cases, KRAS_GENE_ID) & members)
        for question in (J2_NAMED, J2_OPEN):
            # A fresh browser per phrasing: a cohort the first Create saved would be reused, and the run would not pause.
            self.browser = SimulatedBrowser()
            thread_id, run_id = new_id(), new_id()
            frames = await self.turn(self.ask(thread_id, run_id, question), run_id)
            view = self.view(frames)
            self.assertEqual(view.status, "AwaitingConfirmation")
            pending = self.pending(frames)
            [cohort] = pending.cohorts
            self.assertEqual((cohort.ref, cohort.case_count), ("black", len(members)))
            frames = await self.turn(action_body(thread_id, run_id, self.browser.create(pending)), run_id)
            view = self.assertBedrockComplete(frames, question, "oncomatrix")
            facts = self.facts(view)
            self.assertEqual((facts["f.s2.count"], facts["f.s4.top.1.gene"], facts["f.s4.top.1.cases"]), (len(members), "KRAS", kras))
            self.assertEqual([c.outcome for c in self.record(run_id).confirmations], ["accepted"])

    @unittest.skipUnless(ALL_JOURNEYS, "J3 runs with ORION_LIVE_BEDROCK_JOURNEYS=all")
    async def test_j3_pauses_then_runs_de_on_the_read_back_names(self) -> None:
        tested = await asyncio.to_thread(self.upstream.case_ids, ("available_variation_data", ["ssm"]))
        mutated = await asyncio.to_thread(self.upstream.gene_cases, KRAS_GENE_ID) & tested
        for question in (J3_NAMED, J3_OPEN):
            # A fresh browser per phrasing: a cohort the first Create saved would be reused, and the run would not pause.
            self.browser = SimulatedBrowser()
            thread_id, run_id = new_id(), new_id()
            frames = await self.turn(self.ask(thread_id, run_id, question), run_id)
            pending = self.pending(frames)
            self.assertEqual(sorted(c.ref for c in pending.cohorts), ["kras_mut", "kras_wt"])
            facts = self.facts(self.view(frames))
            self.assertEqual((facts["f.s3.count"], facts["f.s5.count"]), (len(tested), len(tested - mutated)))
            frames = await self.turn(action_body(thread_id, run_id, self.browser.create(pending)), run_id)
            view = self.assertBedrockComplete(frames, question, "deDirect")
            facts = self.facts(view)
            padj = [v for i in range(1, 11) if isinstance(v := facts[f"f.s10.top.{i}.padj"], float)]
            self.assertEqual(len(padj), 10)
            self.assertEqual(padj, sorted(padj), "the top 10 is ranked by adjusted p")


if __name__ == "__main__":
    unittest.main()
