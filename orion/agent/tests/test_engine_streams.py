"""J1, J2 and J3 end to end through the real AGUIApp, compiler and engine, with fake tools and model roles.

Every stream passes support.check_stream and validates against models.py (parse_frame), and the
journeys' streams have the shape of their fixtures (test_engine_fakes.shape): the same events in
the same order, the same statuses, step rows, fact ids, Limitations codes, pauses and cohorts.
"""

import asyncio
from typing import Any

from bedrock_agentcore.runtime import AGUIApp
from pydantic import JsonValue
from support import FIELDS_PATH, ControllerCase, action_body, ask_body, asgi_call, check_stream, context, vocabulary
from test_engine_fakes import (
    J1_QUESTION,
    J2_QUESTION,
    J3_QUESTION,
    FakeComposer,
    FakePlanner,
    FakeTools,
    FakeVerifier,
    compiler,
    fixture_frames,
    j1_ir,
    j2_ir,
    j3_ir,
    shape,
    stream_frames,
)

from orion_agent import ir
from orion_agent.app import create_app
from orion_agent.controller import Controller
from orion_agent.executor import Engine
from orion_agent.ids import new_id
from orion_agent.interfaces import Composer, Planner, Verifier
from orion_agent.ledger import StoreLedger
from orion_agent.models import ConfirmPending, RunView, SaveCohortsArgs, def_sha256
from orion_agent.run_store import RunStore
from orion_agent.settings import Settings


class EngineCase(ControllerCase):
    """The production wiring with fakes at the edges: real compiler, engine, renderer and ledger."""

    def build(
        self,
        outputs: list[ir.PlannerOutput | None] | None = None,
        *,
        tools: FakeTools | None = None,
        planner: Planner | None = None,
        composer: Composer | None = None,
        verifier: Verifier | None = None,
        heartbeat_s: float = 15.0,
        lease_ttl_s: float = 180.0,
        stop_grace_s: float = 5.0,
        de_launcher: str = "deDirect",
    ) -> tuple[Controller, AGUIApp]:
        settings = Settings.model_validate(
            {"run_dir": self.run_dir, "fields_path": FIELDS_PATH, "heartbeat_s": heartbeat_s, "lease_ttl_s": lease_ttl_s,
             "stop_grace_s": stop_grace_s, "de_launcher": de_launcher}
        )
        store = RunStore(self.run_dir)
        self.tools = tools or FakeTools()
        self.planner = planner or FakePlanner(outputs or [j1_ir()])
        self.composer = composer or FakeComposer()
        self.verifier = verifier or FakeVerifier()
        controller = Controller(
            settings=settings, store=store, compiler=compiler(), planner=self.planner, executor=Engine(self.tools, vocab=vocabulary()),
            composer=self.composer, verifier=self.verifier, ledger_store=StoreLedger(store), vocab=vocabulary(),
        )
        self.controller = controller
        return controller, create_app(controller)

    async def turn(self, app: AGUIApp, body: dict[str, JsonValue], run_id: str | None = None) -> list[dict[str, Any]]:
        """One invocation: its stream passes check_stream; returns the frames as JSON."""
        response = await asgi_call(app, "POST", "/invocations", body)
        check_stream(self, response.text, expect_run_id=run_id)
        return stream_frames(response.text)

    def record(self, run_id: str) -> dict[str, Any]:  # Any: the record is walked as JSON
        return self.read_json(self.run_dir / "runs" / f"{run_id}.json")

    def last_view(self, frames: list[dict[str, Any]]) -> RunView:
        return RunView.model_validate([f for f in frames if f["type"] == "STATE_SNAPSHOT"][-1]["snapshot"])

    def assertShape(self, frames: list[dict[str, Any]], fixture: str) -> None:
        self.assertEqual(shape(frames), shape(fixture_frames(fixture)))

    async def j3_pause(self, app: AGUIApp, thread_id: str, run_id: str) -> ConfirmPending:
        frames = await self.turn(app, ask_body(thread_id, run_id, question=J3_QUESTION), run_id)
        view = self.last_view(frames)
        assert isinstance(view.pending, ConfirmPending)
        args = "".join(f["delta"] for f in frames if f["type"] == "TOOL_CALL_ARGS")
        full = SaveCohortsArgs.model_validate_json(args)
        self.assertEqual(full.nonce, view.pending.nonce)
        return ConfirmPending.model_validate(full.model_dump(mode="json"))

    def create(self, pending: ConfirmPending, *, mismatch: str | None = None) -> dict[str, JsonValue]:
        saved: list[JsonValue] = [
            {"ref": c.ref, "cohortId": f"cohort-{c.ref}", "name": c.name,
             "defSha256": def_sha256(c.definition) if c.definition is not None and c.ref != mismatch else "0" * 64, "reused": False}
            for c in pending.cohorts
        ]
        return {"action": "confirm", "nonce": pending.nonce, "decision": "create", "saved": saved, "makeActive": None}


class J1Tests(EngineCase):
    async def test_j1_runs_to_complete_with_the_fixture_shape(self) -> None:
        _, app = self.build()
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id), run_id)
        self.assertShape(frames, "j1-complete")
        final = self.last_view(frames)
        expected = self.last_view(fixture_frames("j1-complete"))
        self.assertEqual(
            ["".join(s.text for s in o.segments) for o in final.observed],
            ["".join(s.text for s in o.segments) for o in expected.observed],
        )
        self.assertEqual(final.limitations, expected.limitations)
        spec, fixture_spec = final.viz_specs[0], expected.viz_specs[0]
        self.assertEqual((spec.title, spec.caption, spec.pinned, spec.pinned_hash), (fixture_spec.title, fixture_spec.caption, fixture_spec.pinned, fixture_spec.pinned_hash))
        assert spec.frozen is not None and fixture_spec.frozen is not None
        self.assertEqual(spec.frozen.columns, fixture_spec.frozen.columns)
        self.assertEqual(spec.request_pinned_hash, spec.pinned_hash)
        self.assertEqual(final.environment.fingerprint_sha256 is not None, True)
        message = "".join(f["delta"] for f in frames if f["type"] == "TEXT_MESSAGE_CONTENT")
        self.assertTrue(message.startswith("**Observed**\n- High risk: 339 cases, 299 events."))
        self.assertIn("**Interpretation**", message)
        self.assertTrue(message.endswith(f"_{final.notice}_"))
        self.assertEqual([f["messageId"] for f in frames if f["type"] == "TEXT_MESSAGE_START"], [f"{run_id}.v1"])

    async def test_j1_partial_then_retry_step_completes(self) -> None:
        """E11: survival_compare times out twice, the run ends Partial; retry_step reruns s4 and s5 (j1-partial, j1-retry)."""
        tools = FakeTools({"survival_compare": "timeout"})
        _, app = self.build(tools=tools)
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id), run_id)
        self.assertShape(frames, "j1-partial")
        partial = self.last_view(frames)
        self.assertEqual(partial.withheld_reason, "a step failed")
        self.assertEqual([s.output_summary for s in partial.steps[3:]], ["Timed out on both attempts", "Not run: s4 failed"])
        expected = self.last_view(fixture_frames("j1-partial"))
        self.assertEqual(partial.limitations, expected.limitations)
        tools.behaviours.clear()
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "retry_step", "stepId": "s4"}), run_id)
        self.assertShape(frames, "j1-retry")
        record = self.record(run_id)
        s4 = [(c["callId"].rsplit(".", 1)[1], c["attempt"], c["status"]) for c in record["calls"] if c["stepId"] == "s4"]
        self.assertEqual(s4, [("c1", 1, "error"), ("c2", 2, "error"), ("c3", 1, "ok")])
        self.assertEqual([a["action"] for a in record["actions"]], ["ask", "retry_step"])
        self.assertEqual(tools.calls[-1][1:4:2], ("s4", 3), "the rerun's first callId continues at c3")

    async def test_stop_during_survival_aborts_the_call_and_ends_cancelled(self) -> None:
        """E24: a cancel joining a live run at s4: Stopping at once, the call aborted (CANCELLED), Cancelled (j1-stop)."""
        tools = FakeTools({"survival_compare": "block"})
        _, app = self.build(tools=tools)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(tools.started.wait(), 5)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "cancel"}), run_id)
        self.assertShape(frames, "j1-stop")
        check_stream(self, (await ask).text, expect_run_id=run_id)
        self.assertEqual(tools.cancelled, ["s4"])
        record = self.record(run_id)
        aborted = [c for c in record["calls"] if c["stepId"] == "s4"]
        self.assertEqual((len(aborted), aborted[0]["status"], aborted[0]["error"][:9], aborted[0]["httpStatus"]), (1, "error", "CANCELLED", None))
        self.assertEqual(record["answer"]["withheldReason"], "you stopped the run")
        self.assertIsNone(record["lease"])


class J3Tests(EngineCase):
    async def test_j3_pauses_for_confirmation_with_the_fixture_shape(self) -> None:
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id, question=J3_QUESTION), run_id)
        self.assertShape(frames, "j3-save-pause")
        view = self.last_view(frames)
        expected = self.last_view(fixture_frames("j3-save-pause"))
        self.assertEqual(view.cohorts, expected.cohorts[:1] + [c.model_copy(update={"def_sha256": view.cohorts[i + 1].def_sha256}) for i, c in enumerate(expected.cohorts[1:])])
        pending = view.pending
        assert isinstance(pending, ConfirmPending) and isinstance(expected.pending, ConfirmPending)
        self.assertEqual(
            [(c.ref, c.name, c.case_count, c.eligible_count, c.case_count_fact_id, c.eligible_count_fact_id, c.definition_text) for c in pending.cohorts],
            [(c.ref, c.name, c.case_count, c.eligible_count, c.case_count_fact_id, c.eligible_count_fact_id, c.definition_text) for c in expected.pending.cohorts],
        )
        self.assertEqual(pending.plan_signature, view.plan.signature if view.plan else None)
        index = self.read_json(self.run_dir / "threads" / f"{thread_id}.json")
        self.assertEqual(index["activeRunId"], run_id, "a paused run stays the thread's active run")
        self.assertIsNone(self.record(run_id)["lease"], "a pause clears the lease")

    async def test_create_verifies_the_read_back_and_runs_de_with_the_saved_names(self) -> None:
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        frames = await self.turn(app, action_body(thread_id, run_id, self.create(pending)), run_id)
        self.assertShape(frames, "j3-after-confirm")
        final = self.last_view(frames)
        expected = self.last_view(fixture_frames("j3-after-confirm"))
        def texts(view: RunView) -> list[str]:
            return ["".join(s.text for s in o.segments) for o in view.observed]

        self.assertEqual(texts(final)[:3], texts(expected)[:3])
        self.assertEqual(final.limitations, expected.limitations)
        self.assertEqual(final.viz_specs[0].caption, expected.viz_specs[0].caption)
        self.assertEqual(final.viz_specs[0].cohort_refs, ["kras_wt", "kras_mut"])
        record = self.record(run_id)
        de_request = next(c["request"] for c in record["calls"] if c["stepId"] == "s9")
        self.assertEqual([g["cohortId"] for g in de_request["groups"]], ["cohort-kras_wt", "cohort-kras_mut"])
        self.assertEqual([c["outcome"] for c in record["confirmations"]], ["accepted"])
        write = next(c for c in record["calls"] if c["stepId"] == "s8")
        self.assertEqual((write["transport"], write["observed"]["decision"]), ("client", "create"))

    async def test_a_read_back_mismatch_ends_failed_with_write_verify_failed(self) -> None:
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        frames = await self.turn(app, action_body(thread_id, run_id, self.create(pending, mismatch="kras_mut")), run_id)
        self.assertEqual(frames[-1], {"type": "RUN_ERROR", "message": "The saved cohorts did not match the plan when read back.", "code": "WRITE_VERIFY_FAILED"})
        final = self.last_view(frames)
        self.assertEqual((final.status, final.code), ("Failed", "WRITE_VERIFY_FAILED"))
        self.assertEqual([(s.id, s.status) for s in final.steps[7:]], [("s8", "Failed"), ("s9", "Skipped"), ("s10", "Skipped")])
        self.assertIn("WRITE_VERIFY_FAILED", [lim.code for lim in final.limitations])
        self.assertEqual([c["outcome"] for c in self.record(run_id)["confirmations"]], ["verify_failed"])
        self.assertNotIn("de_run", [c[0] for c in self.tools.calls])

    async def test_dont_save_skips_the_write_and_runs_de_on_the_proposed_names(self) -> None:
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        body: dict[str, JsonValue] = {"action": "confirm", "nonce": pending.nonce, "decision": "dont_save", "saved": [], "makeActive": None}
        frames = await self.turn(app, action_body(thread_id, run_id, body), run_id)
        self.assertShape(frames, "j3-dont-save")
        de_request = next(c["request"] for c in self.record(run_id)["calls"] if c["stepId"] == "s9")
        self.assertEqual([g["name"] for g in de_request["groups"]], ["KRAS not detected (ORION)", "KRAS mutated (ORION)"])
        self.assertTrue(all("cohortId" not in g for g in de_request["groups"]))

    async def test_rename_at_the_pause_reuses_the_reads_and_pauses_with_a_new_nonce(self) -> None:
        """j3-edit-at-pause: version 2, s1-s7 Reused with their facts, superseded, the old nonce voided."""
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        first = await self.j3_pause(app, thread_id, run_id)
        calls_before = len(self.tools.calls)
        edit: dict[str, JsonValue] = {"action": "edit_plan", "field": "cohortName", "value": "KRAS wild-type (ORION)", "ref": "kras_wt"}
        frames = await self.turn(app, action_body(thread_id, run_id, edit), run_id)
        self.assertShape(frames, "j3-edit-at-pause")
        self.assertEqual(len(self.tools.calls), calls_before, "no read reruns")
        view = self.last_view(frames)
        assert isinstance(view.pending, ConfirmPending)
        self.assertNotEqual(view.pending.nonce, first.nonce)
        self.assertEqual(view.pending.plan_version, 2)
        self.assertEqual([c.name for c in view.pending.cohorts], ["KRAS wild-type (ORION)", "KRAS mutated (ORION)"])
        record = self.record(run_id)
        self.assertEqual([(c["nonce"], c["outcome"]) for c in record["confirmations"]], [(first.nonce, "voided")])
        self.assertEqual([h["cause"] for h in record["planHistory"]], ["ask", "edit_plan"])
        stale = await asgi_call(app, "POST", "/invocations", action_body(thread_id, run_id, self.create(first)))
        self.assertEqual(stale.frames()[-1].code, "STALE_ACTION")  # type: ignore[union-attr]

    async def test_the_de_fallback_card_pins_the_saved_cohorts(self) -> None:
        _, app = self.build([j3_ir()], de_launcher="deInput")
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        frames = await self.turn(app, action_body(thread_id, run_id, self.create(pending)), run_id)
        self.assertShape(frames, "j3-deinput")

    async def test_a_resent_confirm_replays_the_recorded_outcome(self) -> None:
        """E19: the confirm is lost after the browser saved and is sent again: nothing runs twice."""
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        await self.turn(app, action_body(thread_id, run_id, self.create(pending)), run_id)
        before = (len(self.tools.calls), self.record(run_id)["revision"])
        frames = await self.turn(app, action_body(thread_id, run_id, self.create(pending)), run_id)
        self.assertEqual([f["type"] for f in frames], ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"])
        self.assertEqual(frames[-1]["result"], {"status": "Complete", "code": None})
        record = self.record(run_id)
        self.assertEqual((len(self.tools.calls), record["revision"]), before)
        self.assertEqual([a["outcome"] for a in record["actions"]][-1], "repeat")


class J2Tests(EngineCase):
    async def test_j2_saves_its_cohort_after_one_confirmation(self) -> None:
        _, app = self.build([j2_ir()])
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id, question=J2_QUESTION), run_id)
        view = self.last_view(frames)
        assert isinstance(view.pending, ConfirmPending)
        self.assertEqual(len(view.pending.cohorts), 1)
        self.assertIsNone(view.pending.cohorts[0].eligible_count)
        pending = ConfirmPending.model_validate(
            SaveCohortsArgs.model_validate_json("".join(f["delta"] for f in frames if f["type"] == "TOOL_CALL_ARGS")).model_dump(mode="json")
        )
        frames = await self.turn(app, action_body(thread_id, run_id, self.create(pending)), run_id)
        final = self.last_view(frames)
        self.assertEqual(final.status, "Complete")
        self.assertEqual([v.launcher for v in final.viz_specs], ["oncomatrix"])
        texts = ["".join(s.text for s in o.segments) for o in final.observed]
        self.assertEqual(texts[1], "The most frequently mutated genes are KRAS (163 of 334 cases), NRAS (121) and DIS3 (97).")
        self.assertIn("PANEL_COVERAGE", [lim.code for lim in final.limitations])
        saved = [c for c in final.cohorts if c.role == "planned"]
        self.assertEqual([(c.save, c.cohort_id) for c in saved], [("saved", f"cohort-{saved[0].ref}")])
