"""Every resume action and the controller's edge cases (contract "Actions", "Checks, in order", E7, E10, E19, E20, E24)."""

import asyncio
from datetime import timedelta
from typing import Any

from bedrock_agentcore.runtime import AGUIApp
from pydantic import JsonValue
from support import action_body, ask_body, asgi_call, check_stream, context
from test_engine_fakes import J3_QUESTION, CGS, FakeComposer, FakePlanner, FakeTools, fixture_frames, j1_ir, j3_ir, orion_of, shape
from test_engine_streams import EngineCase

from orion_agent import controller as controller_module
from orion_agent import ir
from orion_agent.events import iso_utc, utc_now
from orion_agent.ids import new_id
from orion_agent.lease import Lease
from orion_agent.models import ConfirmPending, StateSnapshotFrame
from orion_agent.run_store import RunRecord, RunStore

CONSENT_QUESTION = "Use OncoMatrix to plot survival by CGS risk category"
SAVED_HIGH: dict[str, JsonValue] = {
    "id": "saved-high", "name": "High risk patients",
    "filters": {"mode": "and", "root": {CGS: {"operator": "in", "field": CGS, "operands": ["High risk"]}}}, "caseCount": 339,
}


def consent_ir() -> ir.Plan:
    return j1_ir().model_copy(update={"named_tools": [ir.NamedTool(tool="oncomatrix")]})


def clarify_ir() -> ir.Clarify:
    return ir.Clarify(kind="clarify", slot="split_term", option_ids=[CGS, "diagnoses.iss_stage"])


class ResumeTests(EngineCase):
    async def rejected(self, app: AGUIApp, body: dict[str, JsonValue], code: str) -> None:
        await self.assert_rejected(app, body, code)  # type: ignore[arg-type]

    async def test_consent_accept_runs_the_substitute_and_keeps_the_mismatch_line(self) -> None:
        _, app = self.build([consent_ir()])
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id, question=CONSENT_QUESTION), run_id)
        self.assertEqual(shape(frames), shape(fixture_frames("consent")))
        self.assertEqual(self.tools.calls, [], "no data call before consent")
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "consent", "accept": True}), run_id)
        views = [f["snapshot"] for f in frames if f["type"] == "STATE_SNAPSHOT"]
        self.assertEqual((views[0]["status"], views[0]["code"], views[0]["pending"]), ("Executing", None, None))
        self.assertEqual(views[-1]["status"], "Complete")
        self.assertIn("NAMED_TOOL_MISMATCH", [lim["code"] for lim in views[-1]["limitations"]])

    async def test_consent_decline_ends_cancelled_with_every_step_skipped(self) -> None:
        _, app = self.build([consent_ir()])
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id, question=CONSENT_QUESTION), run_id)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "consent", "accept": False}), run_id)
        self.assertEqual([f["type"] for f in frames], ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"])
        view = self.last_view(frames)
        self.assertEqual(view.status, "Cancelled")
        self.assertTrue(all(s.output_summary == "Not run: you declined the substitution" for s in view.steps))
        self.assertIsNone(self.read_json(self.run_dir / "threads" / f"{thread_id}.json")["activeRunId"])

    async def test_clarify_then_answer_plans_again_with_the_fixed_slot(self) -> None:
        _, app = self.build([clarify_ir(), j1_ir()])
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id, question="Compare survival of high-risk patients"), run_id)
        self.assertEqual(shape(frames), shape(fixture_frames("clarify")))
        await self.rejected(app, action_body(thread_id, run_id, {"action": "clarify_answer", "optionId": "demographic.race"}), "INVALID_INPUT")
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "clarify_answer", "optionId": CGS}), run_id)
        views = [f["snapshot"] for f in frames if f["type"] == "STATE_SNAPSHOT"]
        self.assertEqual([views[0]["status"], views[1]["status"], views[-1]["status"]], ["Planning", "Executing", "Complete"])
        assert isinstance(self.planner, FakePlanner)
        clarification = self.planner.clarifications[-1]
        assert clarification is not None
        self.assertEqual((clarification.slot, clarification.option_id, clarification.field), ("split_term", CGS, CGS))
        self.assertEqual([h["cause"] for h in self.record(run_id)["planHistory"]], ["clarify_answer"])
        self.assertEqual(views[1]["plan"]["version"], 1, "clarify does not bump plan.version")

    async def test_a_second_clarifying_question_ends_limited(self) -> None:
        _, app = self.build([clarify_ir()])
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id), run_id)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "clarify_answer", "optionId": CGS}), run_id)
        self.assertEqual(frames[-1]["result"], {"status": "Limited", "code": "AMBIGUOUS_REQUEST"})

    async def test_ask_with_cancel_waiting_ends_the_paused_run_first(self) -> None:
        _, app = self.build([consent_ir(), clarify_ir()])
        thread_id, old, new = new_id(), new_id(), new_id()
        await self.turn(app, ask_body(thread_id, old, question=CONSENT_QUESTION), old)
        await self.rejected(app, ask_body(thread_id, new_id()), "RUN_BUSY")
        body = ask_body(thread_id, new, question="Compare survival of high-risk patients")
        orion_of(body)["cancelWaiting"] = new_id()
        await self.rejected(app, body, "RUN_BUSY")
        orion_of(body)["cancelWaiting"] = old
        frames = await self.turn(app, body, new)
        self.assertEqual(shape(frames), shape(fixture_frames("cancel-waiting")))
        self.assertEqual(self.record(old)["status"], "Cancelled")

    async def test_cancel_waiting_without_a_paused_run_is_stale(self) -> None:
        _, app = self.build()
        thread_id = new_id()
        await self.turn(app, ask_body(thread_id, new_id()))
        body = ask_body(thread_id, new_id())
        orion_of(body)["cancelWaiting"] = new_id()
        await self.rejected(app, body, "STALE_ACTION")

    async def test_cancel_at_the_save_pause_voids_the_nonce(self) -> None:
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "cancel"}), run_id)
        view = self.last_view(frames)
        self.assertEqual((view.status, view.pending), ("Cancelled", None))
        self.assertTrue(all(s.status in ("Done", "Skipped") for s in view.steps))
        self.assertEqual([(c["nonce"], c["outcome"]) for c in self.record(run_id)["confirmations"]], [(pending.nonce, "voided")])
        await self.rejected(app, action_body(thread_id, run_id, self.create(pending)), "STALE_ACTION")

    async def test_cancel_while_planning_aborts_the_planner_call(self) -> None:
        planner = FakePlanner([j1_ir()], block=True)
        _, app = self.build(planner=planner)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(planner.started.wait(), 5)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "cancel"}), run_id)
        self.assertEqual([v["snapshot"]["status"] for v in frames if v["type"] == "STATE_SNAPSHOT"], ["Stopping", "Cancelled"])
        self.assertIsNone(self.last_view(frames).plan, "no plan is shown")
        check_stream(self, (await ask).text, expect_run_id=run_id)
        calls = self.record(run_id)["modelCalls"]
        self.assertEqual([(c["callId"], c["outcome"]) for c in calls], [(f"{run_id}.planner.c1", "cancelled")])

    async def test_cancel_while_answering_keeps_observed_and_withholds(self) -> None:
        composer = FakeComposer(block=True)
        _, app = self.build(composer=composer)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(composer.started.wait(), 5)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "cancel"}), run_id)
        view = self.last_view(frames)
        self.assertEqual((view.status, view.withheld_reason, view.interpretation), ("Cancelled", "you stopped the run", None))
        self.assertTrue(view.observed)
        check_stream(self, (await ask).text, expect_run_id=run_id)
        calls = self.record(run_id)["modelCalls"]
        self.assertEqual(calls[-1]["outcome"], "cancelled")
        self.assertTrue(calls[-1]["callId"].startswith(f"{run_id}.v1.composer.c"))

    async def test_a_second_cancel_joins_the_stopping_run(self) -> None:
        tools = FakeTools({"survival_compare": "block"})
        _, app = self.build(tools=tools, stop_grace_s=5.0)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(tools.started.wait(), 5)
        handle = controller_module.RUNS[run_id]
        # Keep the run Stopping: the engine is mid-call; both cancels see Stopping then Cancelled.
        first = asyncio.create_task(asgi_call(app, "POST", "/invocations", action_body(thread_id, run_id, {"action": "cancel"})))
        await self.wait_for(lambda: handle.status in ("Stopping", "Cancelled"))
        second = await asgi_call(app, "POST", "/invocations", action_body(thread_id, run_id, {"action": "cancel"}))
        frames = check_stream(self, second.text, expect_run_id=run_id)
        check_stream(self, (await first).text, expect_run_id=run_id)
        check_stream(self, (await ask).text, expect_run_id=run_id)
        self.assertEqual(frames[-1].result.status, "Cancelled")  # type: ignore[union-attr]

    async def test_cancel_of_an_ended_run_changes_nothing(self) -> None:
        _, app = self.build()
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id), run_id)
        before = self.stored()
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "cancel"}), run_id)
        self.assertEqual([f["type"] for f in frames], ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"])
        self.assertEqual(frames[-1]["result"]["status"], "Complete")
        self.assertEqual(self.stored(), before)


class CheckTests(EngineCase):
    async def rejected(self, app: AGUIApp, body: dict[str, JsonValue], code: str) -> None:
        await self.assert_rejected(app, body, code)  # type: ignore[arg-type]

    async def test_checks_reject_without_writing(self) -> None:
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        missing = self.create(pending)
        missing["saved"] = missing["saved"][:1]  # type: ignore[index]
        cases: list[tuple[dict[str, JsonValue], str]] = [
            ({"action": "retry_step", "stepId": "s2"}, "STALE_ACTION"),  # check 6: not Partial
            ({"action": "clarify_answer", "optionId": "x"}, "STALE_ACTION"),
            ({"action": "consent", "accept": True}, "STALE_ACTION"),
            ({**self.create(pending), "nonce": new_id()}, "STALE_ACTION"),  # a nonce never recorded
            (missing, "INVALID_INPUT"),  # check 7: saved misses a pending ref
            ({"action": "edit_plan", "field": "topN", "value": 101}, "INVALID_INPUT"),  # check 2: schema
            ({"action": "edit_plan", "field": "cohortName", "value": "X (ORION)", "ref": "nope"}, "INVALID_INPUT"),
            ({"action": "viz_report", "vizId": "v9", "mounted": True, "error": None, "pinnedHashVerified": None}, "INVALID_INPUT"),
            ({"action": "confirm"}, "INVALID_INPUT"),
        ]
        for orion, code in cases:
            with self.subTest(orion=orion):
                await self.rejected(app, action_body(thread_id, run_id, orion), code)
        await self.rejected(app, action_body(new_id(), run_id, {"action": "cancel"}), "RUN_NOT_FOUND")
        await self.rejected(app, action_body(thread_id, new_id(), {"action": "retry_step", "stepId": "s1"}), "RUN_NOT_FOUND")

    async def test_retry_step_needs_a_failed_read_of_the_latest_run(self) -> None:
        tools = FakeTools({"survival_compare": "timeout"})
        _, app = self.build(tools=tools)
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id), run_id)
        await self.rejected(app, action_body(thread_id, run_id, {"action": "retry_step", "stepId": "s2"}), "STALE_ACTION")  # check 8
        await self.rejected(app, action_body(thread_id, run_id, {"action": "retry_step", "stepId": "s9"}), "INVALID_INPUT")  # check 7
        await self.rejected(app, action_body(thread_id, run_id, {"action": "edit_plan", "field": "method", "value": "edgeR"}), "INVALID_INPUT")
        tools.behaviours.clear()
        await self.turn(app, ask_body(thread_id, new_id()))
        await self.rejected(app, action_body(thread_id, run_id, {"action": "retry_step", "stepId": "s4"}), "STALE_ACTION")  # not the latest

    async def test_a_rename_after_the_save_is_stale(self) -> None:
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        await self.turn(app, action_body(thread_id, run_id, self.create(pending)), run_id)
        edit: dict[str, JsonValue] = {"action": "edit_plan", "field": "cohortName", "value": "KRAS wild-type (ORION)", "ref": "kras_wt"}
        await self.rejected(app, action_body(thread_id, run_id, edit), "STALE_ACTION")

    async def test_a_rejection_on_a_live_run_is_logged_on_it(self) -> None:
        tools = FakeTools({"survival_compare": "block"})
        _, app = self.build(tools=tools)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(tools.started.wait(), 5)
        await self.rejected(app, action_body(thread_id, run_id, {"action": "retry_step", "stepId": "s4"}), "STALE_ACTION")
        tools.release.set()
        check_stream(self, (await ask).text, expect_run_id=run_id)
        actions = [(a["action"], a["outcome"], a["code"]) for a in self.record(run_id)["actions"]]
        self.assertIn(("retry_step", "rejected", "STALE_ACTION"), actions)


class EditTests(EngineCase):
    async def test_an_edit_waits_for_an_unchanged_running_step_and_reuses_it(self) -> None:
        tools = FakeTools({"s2": "block"})
        _, app = self.build(tools=tools, heartbeat_s=0.05)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(tools.started.wait(), 5)
        edit: dict[str, JsonValue] = {"action": "edit_plan", "field": "scope", "value": {"source": "all_cases"}, "cohortContext": context().model_dump(mode="json")}
        call = asyncio.create_task(asgi_call(app, "POST", "/invocations", action_body(thread_id, run_id, edit)))
        await asyncio.sleep(0.2)
        tools.release.set()
        response = await call
        frames = check_stream(self, response.text, expect_run_id=run_id)
        check_stream(self, (await ask).text, expect_run_id=run_id)
        snaps = [f.snapshot for f in frames if isinstance(f, StateSnapshotFrame)]
        self.assertTrue(any(f.type == "CUSTOM" for f in frames), "heartbeats while the step finished")
        self.assertEqual((snaps[0].status, snaps[0].plan.version if snaps[0].plan else None), ("Executing", 2))
        self.assertEqual([s.status for s in snaps[0].steps[:2]], ["Reused", "Reused"])
        self.assertEqual(snaps[0].superseded[0].status, "Executing")
        self.assertEqual(snaps[-1].status, "Complete")
        self.assertNotIn(("STEP_FINISHED", "s2"), [(f.type, getattr(f, "step_name", None)) for f in frames])
        self.assertEqual(sum(1 for c in tools.calls if c[1] == "s2"), 1, "the step that finished during the wait is reused")

    async def test_a_scope_switch_cancels_a_running_step_whose_input_changes(self) -> None:
        """E24: a scope switch while J1's steps run: the running count is aborted and version 2 runs on the new scope."""
        tools = FakeTools({"s2": "block"})
        _, app = self.build(tools=tools)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(tools.started.wait(), 5)
        ctx = context(saved=[SAVED_HIGH])
        edit: dict[str, JsonValue] = {"action": "edit_plan", "field": "scope", "value": {"source": "saved_cohort", "cohortName": "High risk patients"},
                "cohortContext": ctx.model_dump(mode="json")}
        tools.behaviours.clear()
        frames = await self.turn(app, action_body(thread_id, run_id, edit), run_id)
        check_stream(self, (await ask).text, expect_run_id=run_id)
        self.assertEqual(tools.cancelled, ["s2"])
        first = [f["snapshot"] for f in frames if f["type"] == "STATE_SNAPSHOT"][0]
        self.assertEqual((first["plan"]["version"], first["plan"]["scope"]["name"]), (2, "High risk patients"))
        record = self.record(run_id)
        aborted = [c for c in record["calls"] if c["planVersion"] == 1 and c["stepId"] == "s2"]
        self.assertTrue(aborted and aborted[0]["error"].startswith("CANCELLED"))
        self.assertEqual(record["status"], "Complete")
        self.assertEqual(record["planVersions"][-1]["altScopeChoice"], {"source": "active_cohort"})

    async def test_an_edit_of_a_complete_run_supersedes_its_answer(self) -> None:
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        await self.turn(app, action_body(thread_id, run_id, self.create(pending)), run_id)
        before = len(self.tools.calls)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "edit_plan", "field": "method", "value": "edgeR"}), run_id)
        final = self.last_view(frames)
        self.assertEqual((final.status, final.plan.version if final.plan else None), ("Complete", 2))
        self.assertEqual(final.superseded[0].model_dump(include={"plan_version", "status", "had_answer"}, by_alias=False),
                         {"plan_version": 1, "status": "Complete", "had_answer": True})
        self.assertEqual([c[1] for c in self.tools.calls[before:]], ["s9"], "only DE reruns")
        self.assertEqual([(c.ref, c.save) for c in final.cohorts if c.role == "planned" and c.save != "none"],
                         [("kras_mut", "saved"), ("kras_wt", "saved")])
        self.assertEqual([v.viz_id for v in final.viz_specs], ["v2"], "viz ids continue across versions")
        self.assertEqual(self.record(run_id)["superseded"][0]["answer"]["vizSpecs"][0]["vizId"], "v1")

    async def test_a_no_op_edit_is_a_repeat(self) -> None:
        _, app = self.build()
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id), run_id)
        edit: dict[str, JsonValue] = {"action": "edit_plan", "field": "scope", "value": {"source": "active_cohort"}, "cohortContext": context().model_dump(mode="json")}
        revision = self.record(run_id)["revision"]
        frames = await self.turn(app, action_body(thread_id, run_id, edit), run_id)
        self.assertEqual([f["type"] for f in frames], ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"])
        record = self.record(run_id)
        self.assertEqual((record["revision"], record["actions"][-1]["outcome"]), (revision, "repeat"))


class DisconnectAndLeaseTests(EngineCase):
    async def test_tab_closed_during_de_run_then_reopened(self) -> None:
        """E20: the confirm stream drops during de_run; the run finishes in its record; a later turn sees it Complete."""
        tools = FakeTools()
        _, app = self.build([j3_ir()], tools=tools)
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        tools.behaviours["de_run"] = "block"
        response = await asgi_call(app, "POST", "/invocations", action_body(thread_id, run_id, self.create(pending)), disconnect_after=3)
        self.assertFalse(any(f.type in ("RUN_FINISHED", "RUN_ERROR") for f in response.frames()))
        await asyncio.wait_for(tools.started.wait(), 5)
        tools.release.set()
        handle = controller_module.RUNS[run_id]
        assert handle.task is not None
        await asyncio.wait_for(handle.task, 5)
        self.assertEqual(self.record(run_id)["status"], "Complete")
        frames = await self.turn(app, action_body(thread_id, run_id, self.create(pending)), run_id)
        self.assertEqual(frames[-1]["result"]["status"], "Complete")

    async def test_left_at_the_save_pause_then_create_after_a_restart(self) -> None:
        """E20: the pause survives the controller (a Runtime recycle): Create resumes the run from its record."""
        _, app = self.build([j3_ir()])
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        controller_module.reset_registry()
        _, again = self.build([j3_ir()])
        frames = await self.turn(again, action_body(thread_id, run_id, self.create(pending)), run_id)
        self.assertEqual(frames[-1]["result"]["status"], "Complete")

    async def _orphan(self, thread_id: str, status: str) -> tuple[str, Lease]:
        store = RunStore(self.run_dir)
        _, app = self.build()
        run_id = new_id()
        await self.turn(app, ask_body(thread_id, run_id), run_id)
        raw = self.record(run_id)
        now = utc_now()
        lease = Lease(owner="dead:1", renewed_at=iso_utc(now - timedelta(seconds=200)), expires_at=iso_utc(now - timedelta(seconds=20)))
        raw["status"], raw["lease"] = status, lease.model_dump(mode="json")
        raw["steps"][3]["status"], raw["steps"][4]["status"] = "Running", "Queued"
        record = RunRecord.model_validate(raw)
        await store.save_run(record)
        index = await store.load_thread(thread_id)
        index.active_run_id = run_id
        await store.save_thread(index)
        return run_id, lease

    async def test_an_expired_lease_ends_interrupted_before_the_next_ask(self) -> None:
        thread_id = new_id()
        old, lease = await self._orphan(thread_id, "Executing")
        new = new_id()
        venn = ir.Unsupported(kind="unsupported", reason="UNSUPPORTED_ANALYSIS", nearest_app_ids=["SetOperations"])
        _, app = self.build([venn])
        frames = await self.turn(app, ask_body(thread_id, new, question="Show a Venn diagram of the two KRAS cohorts"), new)
        reported = [f["snapshot"] for f in frames if f["type"] == "STATE_SNAPSHOT"][0]
        self.assertEqual((reported["runId"], reported["status"], reported["code"], reported["updatedAt"]),
                         (old, "Interrupted", "RUN_INTERRUPTED", lease.expires_at))
        self.assertEqual([(s["id"], s["status"]) for s in reported["steps"]][3:], [("s4", "Failed"), ("s5", "Skipped")])
        self.assertEqual(frames[-1]["result"], {"status": "Unsupported", "code": "UNSUPPORTED_ANALYSIS"})

    async def test_an_expired_stopping_run_ends_cancelled_for_any_action(self) -> None:
        thread_id = new_id()
        old, lease = await self._orphan(thread_id, "Stopping")
        _, app = self.build()
        frames = await self.turn(app, action_body(thread_id, old, {"action": "retry_step", "stepId": "s4"}), old)
        self.assertEqual([f["type"] for f in frames], ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"])
        self.assertEqual(frames[-1]["result"], {"status": "Cancelled", "code": None})
        self.assertEqual(self.record(old)["updatedAt"], lease.expires_at)


class VizReportTests(EngineCase):
    async def test_standalone_and_ride_along_reports(self) -> None:
        _, app = self.build()
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id), run_id)
        revision = self.record(run_id)["revision"]
        report: dict[str, JsonValue] = {"action": "viz_report", "vizId": "v1", "mounted": True, "error": None, "pinnedHashVerified": True}
        frames = await self.turn(app, action_body(thread_id, run_id, report), run_id)
        self.assertEqual([f["type"] for f in frames], ["RUN_STARTED", "RUN_FINISHED"])
        self.assertEqual(frames[-1]["result"], {"status": "Recorded", "code": None})
        frames = await self.turn(app, action_body(thread_id, run_id, {**report, "mounted": False, "error": "no WebGL"}), run_id)
        record = self.record(run_id)
        self.assertEqual(record["revision"], revision, "a report changes no view field")
        self.assertEqual([(v["vizId"], v["mounted"], v["error"]) for v in record["vizReports"]], [("v1", False, "no WebGL")])
        body = ask_body(thread_id, new_id())
        orion_of(body)["vizReports"] = [
            {"runId": run_id, "vizId": "v1", "mounted": True, "error": None, "pinnedHashVerified": True},
            {"runId": new_id(), "vizId": "v1", "mounted": True, "error": None, "pinnedHashVerified": None},
        ]
        await self.turn(app, body)
        self.assertEqual([(v["mounted"], v["pinnedHashVerified"]) for v in self.record(run_id)["vizReports"]], [(True, True)])


class WarmupTests(EngineCase):
    async def test_warmup_warms_the_model_roles_and_creates_no_run(self) -> None:
        _, app = self.build()
        run_id = new_id()
        frames = await self.turn(app, action_body(new_id(), run_id, {"action": "warmup"}), run_id)
        self.assertEqual(frames[-1]["result"], {"status": "Warmed", "code": None})
        self.assertFalse((self.run_dir / "runs" / f"{run_id}.json").exists())
        assert isinstance(self.planner, FakePlanner)
        self.assertEqual(self.planner.warmed, 1)


class StopDuringDeTests(EngineCase):
    async def test_stop_during_de_run_keeps_the_saved_cohorts_and_the_reads(self) -> None:
        """E24: Stop while de_run runs after Create: the DE request is aborted, s9 and s10 are skipped."""
        tools = FakeTools()
        _, app = self.build([j3_ir()], tools=tools)
        thread_id, run_id = new_id(), new_id()
        pending = await self.j3_pause(app, thread_id, run_id)
        tools.behaviours["de_run"] = "block"
        confirm = asyncio.create_task(asgi_call(app, "POST", "/invocations", action_body(thread_id, run_id, self.create(pending))))
        await asyncio.wait_for(tools.started.wait(), 5)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "cancel"}), run_id)
        check_stream(self, (await confirm).text, expect_run_id=run_id)
        final = self.last_view(frames)
        self.assertEqual(final.status, "Cancelled")
        self.assertEqual([(s.id, s.status, s.output_summary) for s in final.steps[7:]],
                         [("s8", "Done", "2 cohorts saved and read back"), ("s9", "Skipped", "Not run: stopped"), ("s10", "Skipped", "Not run: stopped")])
        self.assertEqual({c.ref: c.save for c in final.cohorts if c.save != "none"}, {"kras_mut": "saved", "kras_wt": "saved"})
        self.assertEqual(final.withheld_reason, "you stopped the run")
        self.assertEqual(tools.cancelled, ["s9"])


class RecomposeTests(EngineCase):
    async def test_one_recompose_after_a_failed_verdict_numbers_the_model_calls(self) -> None:
        """The answer loop (orion_agent.model.answer.write_answer) re-composes once after a model-text failure; the
        controller numbers the composer's calls in the run. (It replaces the test of the controller's own copy of the
        loop, which ran only when orion_agent.model was missing and was removed at integration.)"""
        from test_engine_fakes import FakeVerifier

        verifier = FakeVerifier(retry_first=True)
        _, app = self.build(verifier=verifier)
        run_id = new_id()
        frames = await self.turn(app, ask_body(new_id(), run_id), run_id)
        self.assertEqual(self.last_view(frames).status, "Complete")
        self.assertEqual(verifier.attempts, [1, 2])
        self.assertEqual([c["callId"] for c in self.record(run_id)["modelCalls"]],
                         [f"{run_id}.planner.c1", f"{run_id}.v1.composer.c1", f"{run_id}.v1.composer.c2"])


class RecordTests(EngineCase):
    async def test_the_run_record_keeps_every_part(self) -> None:
        _, app = self.build()
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id), run_id)
        record: dict[str, Any] = self.record(run_id)
        final = self.last_view(frames)
        self.assertEqual(record["runView"], final.model_dump(mode="json"))
        self.assertEqual(record["revision"], final.revision)
        self.assertEqual([h["cause"] for h in record["planHistory"]], ["ask"])
        self.assertEqual([c["callId"] for c in record["modelCalls"]], [f"{run_id}.planner.c1", f"{run_id}.v1.composer.c1"])
        self.assertEqual(record["environment"]["toolTransport"], "in_process")
        self.assertEqual(record["environment"]["fingerprintSha256"], final.environment.fingerprint_sha256)
        calls = {c["callId"]: c for c in record["calls"]}
        self.assertEqual(len(calls), len(record["calls"]))
        for fact in record["facts"]:
            call = calls[fact["callId"]]
            self.assertEqual((fact["requestSha256"], fact["responseSha256"]), (call["requestSha256"], call["responseSha256"]))
        self.assertEqual(record["cohorts"][0]["definition"], {"kind": "filter", "clauses": []})
        self.assertEqual(record["answer"]["vizSpecs"][0]["pinned"]["kind"], "survival")
        self.assertEqual([a["action"] for a in record["actions"]], ["ask"])
        self.assertIsNone(record["lease"])
        self.assertEqual(record["facts"][0]["engine"], "fixture-fingerprint@1")

    async def test_a_composer_decline_ends_observed_only_with_its_reason(self) -> None:
        _, app = self.build(composer=FakeComposer(decline=True))
        run_id = new_id()
        frames = await self.turn(app, ask_body(new_id(), run_id), run_id)
        final = self.last_view(frames)
        self.assertEqual((final.status, final.interpretation), ("ObservedOnly", None))
        self.assertTrue(final.withheld_reason)
        self.assertIn("MODEL_DECLINED", [lim.code for lim in final.limitations])
        self.assertEqual([f["type"] for f in frames][-4:], ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "RUN_FINISHED"])

    async def test_one_recompose_after_a_first_failure(self) -> None:
        from test_engine_fakes import FakeVerifier

        verifier = FakeVerifier(retry_first=True)
        composer = FakeComposer()
        _, app = self.build(composer=composer, verifier=verifier)
        frames = await self.turn(app, ask_body(new_id(), new_id()))
        self.assertEqual(self.last_view(frames).status, "Complete")
        self.assertEqual(verifier.attempts, [1, 2])
        self.assertEqual([len(f) > 0 for f in composer.feedback], [False, True])

    async def test_nothing_surviving_the_verifier_ends_observed_only(self) -> None:
        from test_engine_fakes import FakeVerifier

        _, app = self.build(verifier=FakeVerifier(drop_all=True))
        frames = await self.turn(app, ask_body(new_id(), new_id()))
        final = self.last_view(frames)
        self.assertEqual(final.status, "ObservedOnly")
        self.assertTrue(final.withheld_reason)


__all__ = ["J3_QUESTION"]
