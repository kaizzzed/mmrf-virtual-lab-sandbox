"""Regression tests for the integration of the tracks (no network): what each fix would have caught.

- The model factory takes the role kinds from Settings, as app.py logs them (not only from the environment).
- The engine records every attempt the ToolClient reports: a retried call's first attempt and a stopped
  call's in-flight request (tools.records.call_records, ToolCallCancelled.outcome).
- The session ledger carries what a follow-up is rebuilt from: definitions, the plan IR, the active cohort.
- The answer's openLinks include the compiler's C-FIT hand-offs (CompiledPlan.open_apps).
- The model package has one Bedrock client path, and the vocabulary lists its fields publicly.
"""

import asyncio
import importlib.util
import os
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from unittest import mock

from pydantic import JsonValue
from support import FIELDS_PATH, action_body, ask_body, asgi_call, check_stream, context, vocabulary
from test_engine_fakes import J2_QUESTION, J3_QUESTION, FakeComposer, FakePlanner, FakeTools, FakeVerifier, compile_plan, compiler, j1_ir, j2_ir, j3_ir
from test_engine_streams import EngineCase

from orion_agent import ir
from orion_agent.app import create_app
from orion_agent.controller import Controller, _open_links
from orion_agent.compiler import CompiledPlan
from orion_agent.executor import Engine, RunContext
from orion_agent.ids import new_id
from orion_agent.ledger import StoreLedger, ledger_run
from orion_agent.model import factory, lexicon
from orion_agent.models import ConfirmPending, NearestApp, Provenance, SaveCohortsArgs, StateSnapshotFrame
from orion_agent.observed import survival_count_steps
from orion_agent.run_store import RunRecord, RunStore
from orion_agent.settings import Settings
from orion_agent.stubs import StubPlanner
from orion_agent.tools.base import Attempt, RetryHook, ServerToolName, ToolCallOutcome, ToolError, ToolResult
from orion_agent.tools.records import RecordedOutcome, ToolCallCancelled

HAS_SDK = importlib.util.find_spec("anthropic") is not None
ROLE_ENV = ("ORION_PLANNER", "ORION_COMPOSER")


def _provenance(path: str, status: int | None) -> Provenance:
    return Provenance(method="POST", path=path, request_sha256="a" * 64, http_status=status, response_sha256=None if status != 200 else "b" * 64,
                      elapsed_ms=7)


class RecordingTools(FakeTools):
    """FakeTools whose survival call reports per-attempt results (a RecordedOutcome) as the real clients do:
    `retried` fails attempt 1 with a 503 and answers on attempt 2; `held` waits until cancelled and raises
    ToolCallCancelled carrying the aborted attempt and its in-flight request."""

    def __init__(self, mode: Literal["retried", "held"]) -> None:
        super().__init__()
        self.mode = mode

    async def call(
        self, name: ServerToolName, input: JsonValue, *, timeout_s: float, run_id: str, plan_version: int, step_id: str,
        first_call: int, on_retry: RetryHook | None = None,
    ) -> ToolCallOutcome:
        if name != "survival_compare":
            return await super().call(name, input, timeout_s=timeout_s, run_id=run_id, plan_version=plan_version, step_id=step_id,
                                      first_call=first_call, on_retry=on_retry)
        outcome = await super().call(name, input, timeout_s=timeout_s, run_id=run_id, plan_version=plan_version, step_id=step_id,
                                     first_call=first_call)
        call_id = f"{run_id}.v{plan_version}.{step_id}.c{first_call}"
        if self.mode == "held":
            self.started.set()
            aborted = Attempt(call_id=call_id, attempt=1, started_at=outcome.attempts[0].started_at, elapsed_ms=40, status="error",
                              http_status=None, error=ToolError(code="CANCELLED", message="stopped", retryable=False), backoff_ms=0)
            in_flight = ToolResult(status="error", observed=None, error=aborted.error,
                                   provenance=[_provenance("/analysis/v0/survival/", None)])
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ToolCallCancelled(RecordedOutcome(**{**outcome.model_dump(), "result": in_flight, "attempts": [aborted],
                                                           "attempt_results": [in_flight]})) from None
        first_error = ToolError(code="HTTP_ERROR", message="HTTP 503", http_status=503, retryable=True)
        failed = ToolResult(status="error", observed=None, error=first_error, provenance=[_provenance("/analysis/v0/survival/", 503)])
        answered = outcome.result.model_copy(update={"provenance": [_provenance("/analysis/v0/survival/", 200)]})
        attempts = [
            Attempt(call_id=call_id, attempt=1, started_at=outcome.attempts[0].started_at, elapsed_ms=5, status="error", http_status=503,
                    error=first_error, backoff_ms=1000),
            outcome.attempts[0].model_copy(update={"call_id": f"{run_id}.v{plan_version}.{step_id}.c{first_call + 1}", "attempt": 2}),
        ]
        return RecordedOutcome(**{**outcome.model_dump(), "result": answered, "attempts": attempts, "attempt_results": [failed, answered]})


class EngineRecordTests(EngineCase):
    async def test_a_retried_call_keeps_the_first_attempts_requests(self) -> None:
        _, app = self.build(tools=RecordingTools("retried"))
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id), run_id)
        self.assertEqual(self.last_view(frames).status, "Complete")
        s4 = [c for c in self.record(run_id)["calls"] if c["stepId"] == "s4"]
        self.assertEqual([(c["attempt"], c["status"], [p["httpStatus"] for p in c["provenance"]]) for c in s4],
                         [(1, "error", [503]), (2, "ok", [200])])
        self.assertEqual(s4[0]["error"], "HTTP_ERROR: HTTP 503")

    async def test_a_stopped_call_records_the_request_it_had_in_flight(self) -> None:
        tools = RecordingTools("held")
        _, app = self.build(tools=tools)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(tools.started.wait(), 5)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "cancel"}), run_id)
        check_stream(self, (await ask).text, expect_run_id=run_id)
        self.assertEqual(self.last_view(frames).status, "Cancelled")
        [aborted] = [c for c in self.record(run_id)["calls"] if c["stepId"] == "s4"]
        self.assertEqual((aborted["status"], aborted["error"]), ("error", "CANCELLED: stopped"))
        self.assertEqual([(p["path"], p["httpStatus"]) for p in aborted["provenance"]], [("/analysis/v0/survival/", None)])


class GatedStore(RunStore):
    """A run store whose save of a record matching `gate` waits for `release` (a slow disk or S3 write)."""

    def __init__(self, root: Path, gate: Callable[[RunRecord], bool]) -> None:
        super().__init__(root)
        self.gate = gate
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    async def save_run(self, record: RunRecord) -> None:
        if not self.release.is_set() and self.gate(record):
            self.held.set()
            await self.release.wait()
        await super().save_run(record)


class CancelRaceTests(EngineCase):
    async def test_a_cancel_waiting_for_the_writer_streams_stopping_first(self) -> None:
        """E24 found it: Stop arriving while the run's own transition is being written (here s4 starting) got that
        Executing snapshot first. The contract's resume stream starts with the snapshot with the cancel applied."""
        store = GatedStore(self.run_dir, lambda r: any(s.id == "s4" and s.status == "Running" for s in r.steps) and r.status == "Executing")
        tools = FakeTools({"survival_compare": "block"})
        settings = Settings(run_dir=self.run_dir, fields_path=FIELDS_PATH)
        controller = Controller(
            settings=settings, store=store, compiler=compiler(), planner=FakePlanner([j1_ir()]), executor=Engine(tools, vocab=vocabulary()),
            composer=FakeComposer(), verifier=FakeVerifier(), ledger_store=StoreLedger(store), vocab=vocabulary(),
        )
        app = create_app(controller)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(store.held.wait(), 5)
        cancel = asyncio.create_task(asgi_call(app, "POST", "/invocations", action_body(thread_id, run_id, {"action": "cancel"})))
        await asyncio.sleep(0.05)  # the cancel turn is accepted and waits for the writer the run's task holds
        store.release.set()
        frames = check_stream(self, (await cancel).text, expect_run_id=run_id)
        check_stream(self, (await ask).text, expect_run_id=run_id)
        snapshots = [f for f in frames if isinstance(f, StateSnapshotFrame)]
        self.assertEqual(snapshots[0].snapshot.status, "Stopping", [s.snapshot.status for s in snapshots])
        self.assertEqual(snapshots[-1].snapshot.status, "Cancelled")


class FilteredScopeTests(EngineCase):
    async def test_j1_in_a_filtered_active_cohort_renders_every_group_and_the_survival_card(self) -> None:
        """E14 found it: with a filtered scope, survival's dependencies include the scope's cases_query (C-23), so the
        renderer saw three counts for two groups and dropped the group lines, the medians and the card."""
        _, app = self.build()
        race = context({"demographic.race": {"operator": "in", "field": "demographic.race", "operands": ["black or african american"]}},
                       name="Black or African American (ORION)", case_count=334)
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id, cohort_context=race), run_id)
        view = self.last_view(frames)
        self.assertEqual(view.status, "Complete")
        assert view.plan is not None
        self.assertEqual([s.tool for s in view.steps][:5], ["fixture_fingerprint", "cases_query", "cohort_count", "cohort_count", "survival_compare"])
        texts = ["".join(seg.text for seg in o.segments) for o in view.observed]
        self.assertTrue(texts[0].startswith("High risk: 339 cases, 299 events"), texts)
        self.assertTrue(texts[1].startswith("Standard risk: 661 cases, 300 events"), texts)
        self.assertTrue(any(t.startswith("Median overall survival") for t in texts), texts)
        self.assertEqual([v.launcher for v in view.viz_specs], ["survival"])
        compiled = RunRecord.model_validate(self.record(run_id)).plan_versions[-1]
        survival = next(s for s in compiled.steps if s.tool == "survival_compare")
        self.assertEqual(survival_count_steps(compiled, survival), ["s3", "s4"], "the scope's cases_query (s2) is not a group")


class EnginePreGuardTests(unittest.TestCase):
    """The executor-checked pre-guards are tools.guards' (TOOLS.md GuardSpec checked_by executor)."""

    def setUp(self) -> None:
        self.engine = Engine(FakeTools(), vocab=vocabulary())
        self.plan = compile_plan(j2_ir())
        ctx = RunContext(run_id=new_id(), cohort_context=context(), cohorts=[], cohort_steps={})
        self.state = self.engine._state(self.plan, ctx, None)
        self.genes = next(s for s in self.plan.steps if s.tool == "top_mutated_genes")

    def guard(self, filter0: JsonValue) -> object:
        return self.engine._pre_guard(self.state, self.genes, {"filter0": filter0, "geneCount": 4})

    def test_a_numeric_field_written_as_text_never_reaches_the_bridge(self) -> None:
        """OQ11's silent zero: censor_os in ["1"] matches no case in the ProteinPaint bridge while Gen3 matches 599."""
        refused = self.guard({"and": [{"in": {"outcomes.censor_os": ["1"]}}]})
        assert isinstance(refused, tuple)
        code, summary, line = refused
        spec = vocabulary().field("outcomes.censor_os")
        assert spec is not None
        self.assertEqual((code, summary), ("UNSUPPORTED_FIELD", f"Not run: {spec.label} cannot filter ProteinPaint"))
        assert line is not None
        self.assertEqual(line.code, "UNSUPPORTED_FIELD")
        self.assertIsNone(self.guard({"and": [{"in": {"outcomes.censor_os": [1]}}]}))

    def test_case_id_lists_pass_and_elasticsearch_only_fields_stop(self) -> None:
        self.assertIsNone(self.guard({"and": [{"in": {"case_id": ["SYNTH-CASE-0001"]}}]}))
        self.assertIsNone(self.guard(None))
        refused = self.guard({"and": [{"in": {"available_variation_data": ["ssm"]}}]})
        assert isinstance(refused, tuple)
        self.assertEqual(refused[0], "UNSUPPORTED_FIELD")


class LedgerTests(EngineCase):
    async def test_the_ledger_keeps_definitions_the_plan_ir_and_the_active_cohort(self) -> None:
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id, question=J3_QUESTION), run_id)
        record = RunRecord.model_validate(self.record(run_id))
        entry = ledger_run(record)
        assert entry is not None
        self.assertEqual(entry.plan_ir, j3_ir())
        self.assertEqual(entry.active, record.request.cohort_context.active)
        by_ref = {c.ref: c for c in entry.cohorts}
        mutated = next(c for c in record.cohorts if c.ref == "kras_mut")
        self.assertEqual((by_ref["kras_mut"].definition, by_ref["kras_mut"].definition_text), (mutated.definition, mutated.definition_text))
        self.assertEqual(entry.scope, record.plan_versions[-1].scope)

    async def test_show_the_top_5_instead_rebuilds_the_thread_cohorts_after_the_active_cohort_changed(self) -> None:
        """E15 with the stub planner: the follow-up reuses the earlier run's analysis and its thread cohorts (it needs
        the ledger's plan IR and definitions), and names the scope the thread left (it needs the run's active cohort)."""
        _, app = self.build(planner=StubPlanner())
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id, question=J3_QUESTION), run_id)
        args = SaveCohortsArgs.model_validate_json("".join(f["delta"] for f in frames if f["type"] == "TOOL_CALL_ARGS"))
        await self.turn(app, action_body(thread_id, run_id, self.create(ConfirmPending.model_validate(args.model_dump(mode="json")))), run_id)
        race = context({"demographic.race": {"operator": "in", "field": "demographic.race", "operands": ["black or african american"]}},
                       name="Black or African American", case_count=334)
        follow, body = new_id(), ask_body(thread_id, new_id(), question="Show the top 5 instead", cohort_context=race)
        body["runId"] = follow
        frames = await self.turn(app, body, follow)
        view = self.last_view(frames)
        self.assertNotEqual(view.status, "Unsupported", "the follow-up reuses the earlier analysis")
        assert view.plan is not None
        self.assertIsNotNone(view.plan.alt_scope, "the plan names both scopes after the switch")
        record = RunRecord.model_validate(self.record(follow))
        assert isinstance(record.planner_output, ir.Plan)
        self.assertEqual(record.planner_output.follows, run_id)
        self.assertTrue(any(isinstance(c.definition, ir.ThreadDef) for c in record.planner_output.cohorts))


class OpenLinkTests(EngineCase):
    async def test_the_compilers_hand_offs_join_the_composers_links(self) -> None:
        """A named Mutation Frequency is a C-FIT hand-off (CompiledPlan.open_apps): the answer opens it, beside the composer's."""
        plan = j2_ir().model_copy(update={"named_tools": [ir.NamedTool(tool="mutation_frequency")]})
        _, app = self.build([plan])
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id, question=J2_QUESTION), run_id)
        args = SaveCohortsArgs.model_validate_json("".join(f["delta"] for f in frames if f["type"] == "TOOL_CALL_ARGS"))
        frames = await self.turn(app, action_body(thread_id, run_id, self.create(ConfirmPending.model_validate(args.model_dump(mode="json")))), run_id)
        view = self.last_view(frames)
        self.assertEqual(view.status, "Complete")
        self.assertEqual([link.app_id for link in view.open_links], ["MutationFrequency", "CohortComparison"])

    def test_each_app_is_linked_once_hand_offs_first(self) -> None:
        compiled: CompiledPlan = compile_plan(j1_ir()).model_copy(update={"open_apps": [NearestApp.of("CohortComparison")]})
        links = _open_links(compiled, [NearestApp.of("GeneExpression"), NearestApp.of("CohortComparison")])
        self.assertEqual([a.app_id for a in links], ["CohortComparison", "GeneExpression"])


class ModelPackageTests(unittest.TestCase):
    def test_the_factory_takes_the_role_kinds_from_settings(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ROLE_ENV:
                os.environ.pop(name, None)
            self.assertIsInstance(factory.make_planner(Settings(fields_path=FIELDS_PATH)), StubPlanner)
            if HAS_SDK:
                from orion_agent.model.composer import BedrockComposer
                from orion_agent.model.planner import BedrockPlanner

                bedrock = Settings(fields_path=FIELDS_PATH, planner="bedrock", composer="bedrock")
                self.assertIsInstance(factory.make_planner(bedrock), BedrockPlanner)
                self.assertIsInstance(factory.make_composer(bedrock), BedrockComposer)

    def test_one_bedrock_client_path_with_inference_profile_defaults(self) -> None:
        import orion_agent.model as package
        from orion_agent.model.config import ModelConfig

        modules = sorted(p.stem for p in Path(package.__file__).parent.glob("*.py"))
        self.assertNotIn("bedrock", modules)
        self.assertNotIn("transport", modules)
        config = ModelConfig()
        self.assertEqual((config.bedrock_client, config.region, config.planner_model, config.composer_model, config.fallback_model),
                         ("runtime", "us-east-1", "us.anthropic.claude-opus-5-5", "us.anthropic.claude-opus-5-5", "us.anthropic.claude-opus-5"))

    def test_the_vocabulary_lists_its_fields_publicly(self) -> None:
        vocab = vocabulary()
        fields = vocab.fields()
        self.assertEqual([f.id for f in fields], sorted(f.id for f in fields))
        self.assertIn("demographic.race", [f.id for f in fields])
        self.assertEqual(lexicon.vocabulary_fields(vocab), fields)


if __name__ == "__main__":
    unittest.main()
