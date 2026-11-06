"""Run controller behaviour through the real AGUIApp with the Day 1 stubs: detached runs, lease, busy, cancel, ping.

The engine, the resume actions and the journeys are in test_engine_*.py."""

import asyncio
import contextlib
import json
import time
from datetime import timedelta

from bedrock_agentcore.runtime import AGUIApp
from pydantic import JsonValue
from support import (
    ControllerCase,
    FailingExecutor,
    GatedExecutor,
    action_body,
    ask_body,
    asgi_call,
    check_stream,
    context,
    snapshots,
)

from orion_agent import controller as controller_module
from orion_agent import events
from orion_agent.compiler import CompiledPlan
from orion_agent.controller import ExecutionResult, StepSession
from orion_agent.events import iso_utc, utc_now
from orion_agent.ids import is_valid_id, new_id
from orion_agent.lease import Lease
from orion_agent.models import (
    SNAPSHOT_LIMIT_BYTES,
    RUN_VIEW_SCHEMA,
    AskAction,
    HeartbeatFrame,
    NearestApp,
    RunErrorFrame,
    RunFinishedFrame,
    RunStartedFrame,
    RunView,
    StateSnapshotFrame,
    StepStatus,
    StepView,
)
from orion_agent.run_store import Environment, RunRecord, RunStore, ThreadIndex


class SlowIndexStore(RunStore):
    """A thread-index write that blocks its worker thread for a while, like an S3 PUT in mode A."""

    async def save_thread(self, index: ThreadIndex) -> None:
        await asyncio.to_thread(time.sleep, 0.2)
        await super().save_thread(index)


class BrokenStore(RunStore):
    broken = False

    async def save_run(self, record: RunRecord) -> None:
        if self.broken:
            raise OSError("simulated full disk")
        await super().save_run(record)


class StoreBreakingExecutor:
    """Fails mid-step after breaking the store, so the controller cannot record the failure."""

    def __init__(self, store: BrokenStore) -> None:
        self.store = store

    async def execute(self, plan: CompiledPlan, session: StepSession) -> ExecutionResult:
        await session.start_step(plan.steps[0].id)
        self.store.broken = True
        raise RuntimeError("simulated executor bug")


class IdTests(ControllerCase):
    def test_ids_are_128_bit_lowercase_hex(self) -> None:
        ids = {new_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        self.assertTrue(all(is_valid_id(i) for i in ids))
        for bad in ("A" * 32, "a" * 31, "g" * 32, "", None, "a" * 33):
            self.assertFalse(is_valid_id(bad))


class InvocationTests(ControllerCase):
    async def _terminal(self, body: dict[str, JsonValue]) -> RunFinishedFrame | RunErrorFrame:
        _, app = self.make()
        response = await asgi_call(app, "POST", "/invocations", body)
        frames = check_stream(self, response.text)
        last = frames[-1]
        assert isinstance(last, (RunFinishedFrame, RunErrorFrame))
        return last

    async def test_warmup_starts_and_finishes_warmed_without_a_record(self) -> None:
        run_id = new_id()
        last = await self._terminal(action_body(new_id(), run_id, {"action": "warmup"}))
        assert isinstance(last, RunFinishedFrame)
        self.assertEqual((last.result.status, last.result.code), ("Warmed", None))
        self.assertFalse((self.run_dir / "runs" / f"{run_id}.json").exists())

    async def test_invalid_ids_are_rejected_with_a_lone_run_error(self) -> None:
        _, app = self.make()
        thread_id, run_id = new_id(), new_id()
        await asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id))
        before = self.stored()
        bodies = [
            action_body("A" * 32, new_id(), {"action": "warmup"}),
            ask_body(thread_id, "1234"),
            ask_body(thread_id.upper(), new_id()),
            action_body(thread_id, run_id.upper(), {"action": "cancel"}),
        ]
        for body in bodies:
            frames = (await asgi_call(app, "POST", "/invocations", body)).frames()
            self.assertEqual(len(frames), 1)
            only = frames[0]
            assert isinstance(only, RunErrorFrame)
            self.assertEqual(only.code, "INVALID_INPUT")
        self.assertEqual(self.stored(), before)

    async def test_malformed_actions_are_invalid_input(self) -> None:
        _, app = self.make()
        bad: list[dict[str, JsonValue] | None] = [None, {"action": "explode"}, {"action": "ask"}, {"action": "ask", "question": "", "cohortContext": {}}, {"action": "warmup", "extra": 1}]
        for orion in bad:
            with self.subTest(orion=orion):
                await self.assert_rejected(app, action_body(new_id(), new_id(), orion), "INVALID_INPUT")

    async def test_every_action_is_implemented_and_a_wrong_one_leaves_its_run_alone(self) -> None:
        """No action is NOT_IMPLEMENTED any more; one a Limited run cannot take is rejected by check 6 or 7."""
        _, app = self.make()
        thread_id, run_id = new_id(), new_id()
        await asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id))
        cases: list[tuple[dict[str, JsonValue], str]] = [
            ({"action": "clarify_answer", "optionId": "x"}, "STALE_ACTION"),
            ({"action": "consent", "accept": True}, "STALE_ACTION"),
            ({"action": "confirm", "nonce": new_id(), "decision": "dont_save", "saved": [], "makeActive": None}, "STALE_ACTION"),
            ({"action": "retry_step", "stepId": "s1"}, "STALE_ACTION"),
            ({"action": "edit_plan", "field": "method", "value": "edgeR"}, "INVALID_INPUT"),
            ({"action": "viz_report", "vizId": "v1", "mounted": True, "error": None, "pinnedHashVerified": None}, "INVALID_INPUT"),
        ]
        for orion, code in cases:
            with self.subTest(action=orion["action"]):
                await self.assert_rejected(app, action_body(thread_id, run_id, orion), code)  # type: ignore[arg-type]

    async def test_venn_question_ends_unsupported_naming_set_operations(self) -> None:
        _, app = self.make()
        question = "Show a Venn diagram of the two KRAS cohorts"
        frames = check_stream(self, (await asgi_call(app, "POST", "/invocations", ask_body(new_id(), new_id(), question=question))).text)
        last = frames[-1]
        assert isinstance(last, RunFinishedFrame)
        self.assertEqual((last.result.status, last.result.code), ("Unsupported", "UNSUPPORTED_ANALYSIS"))
        final = snapshots(frames)[-1].snapshot
        self.assertEqual((final.plan, final.steps), (None, []))
        self.assertEqual(
            [(lim.code, lim.unblock, lim.nearest_apps) for lim in final.limitations],
            [("UNSUPPORTED_ANALYSIS", None, [NearestApp(app_id="SetOperations", title="Set Operations")])],
        )

    async def test_other_unsupported_questions_name_no_app(self) -> None:
        _, app = self.make()
        frames = check_stream(self, (await asgi_call(app, "POST", "/invocations", ask_body(new_id(), new_id(), question="Which pathway is most active here?"))).text)
        final = snapshots(frames)[-1].snapshot
        self.assertEqual((final.status, [lim.nearest_apps for lim in final.limitations]), ("Unsupported", [[]]))

    async def test_reused_run_id_is_rejected(self) -> None:
        _, app = self.make()
        thread_id, run_id = new_id(), new_id()
        await asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id))
        await self.assert_rejected(app, ask_body(thread_id, run_id), "INVALID_INPUT")
        await self.assert_rejected(app, ask_body(new_id(), run_id), "INVALID_INPUT")

    async def test_unexpected_error_ends_failed_with_one_run_error(self) -> None:
        _, app = self.make(executor=FailingExecutor())
        run_id = new_id()
        response = await asgi_call(app, "POST", "/invocations", ask_body(new_id(), run_id))
        frames = check_stream(self, response.text)
        last = frames[-1]
        assert isinstance(last, RunErrorFrame)
        self.assertEqual(last.code, "INTERNAL_ERROR")
        record = self.read_json(self.run_dir / "runs" / f"{run_id}.json")
        self.assertEqual((record["status"], record["code"]), ("Failed", "INTERNAL_ERROR"))
        final = snapshots(frames)[-1].snapshot
        self.assertEqual(final.steps[0].status, "Failed")
        for steps in ([(s.status, s.output_summary) for s in final.steps[1:]], [(s["status"], s["outputSummary"]) for s in record["steps"][1:]]):
            self.assertTrue(steps)
            self.assertTrue(all(step == ("Skipped", "Not run: the run failed") for step in steps), steps)

    async def test_a_failure_that_cannot_be_recorded_still_ends_busy(self) -> None:
        store = BrokenStore(self.run_dir)
        controller, app = self.make(executor=StoreBreakingExecutor(store))
        controller.store = store
        frames = check_stream(self, (await asgi_call(app, "POST", "/invocations", ask_body(new_id(), new_id()))).text)
        last = frames[-1]
        assert isinstance(last, RunErrorFrame)
        self.assertEqual(last.code, "INTERNAL_ERROR")
        self.assertFalse(controller.busy())


class DetachedRunTests(ControllerCase):
    async def test_client_disconnect_mid_run_still_finishes_the_record(self) -> None:
        executor = GatedExecutor()
        _, app = self.make(executor=executor)
        thread_id, run_id = new_id(), new_id()
        response = await asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id), disconnect_after=3)
        self.assertFalse(any(isinstance(f, (RunFinishedFrame, RunErrorFrame)) for f in response.frames()))
        handle = controller_module.RUNS[run_id]
        assert handle.task is not None
        self.assertFalse(handle.task.done(), "the run must outlive its invocation")
        executor.release.set()
        await asyncio.wait_for(handle.task, 5)
        record = self.read_json(self.run_dir / "runs" / f"{run_id}.json")
        self.assertEqual(record["status"], "Limited")
        self.assertTrue(all(step["status"] == "Done" for step in record["steps"]))
        self.assertIsNone(self.read_json(self.run_dir / "threads" / f"{thread_id}.json")["activeRunId"])

    async def test_disconnect_right_after_run_started_still_runs_the_run(self) -> None:
        controller, app = self.make()
        controller.store = SlowIndexStore(self.run_dir)
        thread_id, run_id = new_id(), new_id()
        call = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        # The Planning record is written and the thread-index write is in flight.
        await self.wait_for((self.run_dir / "runs" / f"{run_id}.json").exists)
        call.cancel()  # what Starlette does to the handler when the client disconnects
        with contextlib.suppress(asyncio.CancelledError):
            await call
        handle = controller_module.RUNS.get(run_id)
        assert handle is not None and handle.task is not None, "the run must have its task"
        await asyncio.wait_for(handle.task, 5)
        self.assertEqual(self.read_json(self.run_dir / "runs" / f"{run_id}.json")["status"], "Limited")
        index = self.read_json(self.run_dir / "threads" / f"{thread_id}.json")
        self.assertEqual((index["runIds"], index["activeRunId"]), ([run_id], None))
        self.assertFalse(controller.busy())
        self.assertEqual(json.loads((await asgi_call(app, "GET", "/ping")).text), {"status": "Healthy"})
        again = await asgi_call(app, "POST", "/invocations", ask_body(thread_id, new_id()))
        self.assertIsInstance(check_stream(self, again.text)[-1], RunFinishedFrame)

    async def test_ping_reports_busy_only_during_a_run(self) -> None:
        executor = GatedExecutor()
        _, app = self.make(executor=executor)
        ping = await asgi_call(app, "GET", "/ping")
        self.assertEqual(json.loads(ping.text), {"status": "Healthy"})
        stream = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(new_id(), new_id())))
        await asyncio.wait_for(executor.started.wait(), 5)
        self.assertEqual(json.loads((await asgi_call(app, "GET", "/ping")).text), {"status": "HealthyBusy"})
        executor.release.set()
        await stream
        self.assertEqual(json.loads((await asgi_call(app, "GET", "/ping")).text), {"status": "Healthy"})

    async def test_rejections_during_a_live_run_leave_it_running(self) -> None:
        executor = GatedExecutor()
        _, app = self.make(executor=executor)
        thread_id, run_id = new_id(), new_id()
        first = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(executor.started.wait(), 5)
        await self.assert_rejected(app, ask_body(thread_id, new_id()), "RUN_BUSY")
        await self.assert_rejected(app, ask_body(thread_id, run_id), "INVALID_INPUT")
        await self.assert_rejected(app, action_body(thread_id, run_id, {"action": "retry_step", "stepId": "s1"}), "STALE_ACTION")
        self.assertEqual(controller_module.RUNS[run_id].status, "Executing")
        executor.release.set()
        last = check_stream(self, (await first).text, expect_run_id=run_id)[-1]
        assert isinstance(last, RunFinishedFrame)
        self.assertEqual(last.result.status, "Limited")
        again = await asgi_call(app, "POST", "/invocations", ask_body(thread_id, new_id()))
        self.assertIsInstance(check_stream(self, again.text)[-1], RunFinishedFrame)

    async def test_cancel_stops_at_the_next_step_boundary_and_keeps_finished_steps(self) -> None:
        executor = GatedExecutor()
        controller, app = self.make(executor=executor)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(executor.started.wait(), 5)
        cancel = asyncio.create_task(asgi_call(app, "POST", "/invocations", action_body(thread_id, run_id, {"action": "cancel"})))
        handle = controller_module.RUNS[run_id]
        await self.wait_for(lambda: handle.status == "Stopping")
        executor.release.set()
        cancel_frames = check_stream(self, (await cancel).text, expect_run_id=run_id)
        ask_frames = check_stream(self, (await ask).text, expect_run_id=run_id)
        cancel_views = [f.snapshot for f in snapshots(cancel_frames)]
        self.assertEqual(cancel_views[0].status, "Stopping")
        final = cancel_views[-1]
        self.assertEqual(final.status, "Cancelled")
        self.assertEqual(final.steps[0].status, "Done")
        self.assertTrue(all(s.status == "Skipped" and s.output_summary == "Not run: stopped" for s in final.steps[1:]))
        for frames in (cancel_frames, ask_frames):
            last = frames[-1]
            assert isinstance(last, RunFinishedFrame)
            self.assertEqual(last.result.status, "Cancelled")
        self.assertIsNone(self.read_json(self.run_dir / "threads" / f"{thread_id}.json")["activeRunId"])

    async def test_cancel_of_unknown_or_ended_runs(self) -> None:
        _, app = self.make()
        thread_id, run_id = new_id(), new_id()
        await self.assert_rejected(app, action_body(thread_id, new_id(), {"action": "cancel"}), "RUN_NOT_FOUND")
        await asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id))
        before = self.stored()
        ended = await asgi_call(app, "POST", "/invocations", action_body(thread_id, run_id, {"action": "cancel"}))
        last = check_stream(self, ended.text)[-1]
        assert isinstance(last, RunFinishedFrame)
        self.assertEqual(last.result.status, "Limited")
        self.assertEqual(self.stored(), before)
        await self.assert_rejected(app, action_body(new_id(), run_id, {"action": "cancel"}), "RUN_NOT_FOUND")

    async def test_heartbeat_streams_while_waiting(self) -> None:
        executor = GatedExecutor()
        _, app = self.make(executor=executor, heartbeat_s=0.05)
        stream = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(new_id(), new_id())))
        await asyncio.wait_for(executor.started.wait(), 5)
        await asyncio.sleep(0.3)
        executor.release.set()
        frames = check_stream(self, (await stream).text)
        beats = [f for f in frames if isinstance(f, HeartbeatFrame)]
        self.assertGreaterEqual(len(beats), 3)


class LeaseTests(ControllerCase):
    async def _orphan(self, thread_id: str, expires_in_s: float) -> str:
        """A run record left Executing by a controller that is gone."""
        store = RunStore(self.run_dir)
        run_id, now = new_id(), utc_now()
        lease = Lease(owner="dead:1", renewed_at=iso_utc(now - timedelta(seconds=200)), expires_at=iso_utc(now + timedelta(seconds=expires_in_s)))
        statuses: tuple[tuple[int, StepStatus], ...] = ((1, "Done"), (2, "Running"), (3, "Queued"))
        record = RunRecord(
            thread_id=thread_id, run_id=run_id, revision=3, status="Executing", question="old", created_at=iso_utc(now),
            updated_at=iso_utc(now), environment=Environment(mode="L", planner="stub", executor="stub", agent_version="test"),
            lease=lease, request=AskAction(action="ask", question="old", cohort_context=context()),
            steps=[
                StepView(id=f"s{i}", row=1, tool="cohort_count", kind="server", folded=False, input_summary="x",
                         status=status, output_summary=None, duration_ms=None, code=None)
                for i, status in statuses
            ],
        )
        await store.save_run(record)
        await store.save_thread(ThreadIndex(thread_id=thread_id, run_ids=[run_id], active_run_id=run_id))
        return run_id

    async def test_expired_lease_is_marked_interrupted_by_the_next_turn(self) -> None:
        _, app = self.make()
        thread_id = new_id()
        old = await self._orphan(thread_id, expires_in_s=-20)
        new = new_id()
        response = await asgi_call(app, "POST", "/invocations", ask_body(thread_id, new))
        frames = check_stream(self, response.text, expect_run_id=new)
        report = frames[1]
        assert isinstance(report, StateSnapshotFrame), "the Interrupted run comes right after RUN_STARTED"
        self.assertEqual((report.snapshot.run_id, report.snapshot.status, report.snapshot.code), (old, "Interrupted", "RUN_INTERRUPTED"))
        self.assertTrue(all(f.snapshot.run_id == new for f in snapshots(frames)[1:]))
        last = frames[-1]
        assert isinstance(last, RunFinishedFrame)
        self.assertEqual((last.run_id, last.result.status), (new, "Limited"))
        record = self.read_json(self.run_dir / "runs" / f"{old}.json")
        self.assertEqual((record["status"], record["code"], record["revision"]), ("Interrupted", "RUN_INTERRUPTED", 4))
        self.assertIn("RUN_INTERRUPTED", [lim["code"] for lim in record["answer"]["limitations"]])
        self.assertEqual([(s["status"], s["code"]) for s in record["steps"]], [("Done", None), ("Failed", "RUN_INTERRUPTED"), ("Skipped", None)])
        index = self.read_json(self.run_dir / "threads" / f"{thread_id}.json")
        self.assertEqual((index["runIds"], index["activeRunId"]), ([old, new], None))

    async def test_live_lease_owned_elsewhere_keeps_the_thread_busy(self) -> None:
        _, app = self.make()
        thread_id = new_id()
        old = await self._orphan(thread_id, expires_in_s=120)
        await self.assert_rejected(app, ask_body(thread_id, new_id()), "RUN_BUSY")
        await self.assert_rejected(app, action_body(thread_id, old, {"action": "cancel"}), "RUN_BUSY")

    async def _cancel_interrupted(self, app: AGUIApp, thread_id: str, run_id: str, revision: int) -> None:
        """A cancel of an Interrupted run: its snapshot right after RUN_STARTED, then RUN_FINISHED Interrupted."""
        body = action_body(thread_id, run_id, {"action": "cancel"})
        frames = check_stream(self, (await asgi_call(app, "POST", "/invocations", body)).text, expect_run_id=run_id)
        self.assertEqual([type(f) for f in frames], [RunStartedFrame, StateSnapshotFrame, RunFinishedFrame])
        view = snapshots(frames)[0].snapshot
        self.assertEqual((view.run_id, view.status, view.code, view.revision), (run_id, "Interrupted", "RUN_INTERRUPTED", revision))
        last = frames[-1]
        assert isinstance(last, RunFinishedFrame)
        self.assertEqual((last.result.status, last.result.code), ("Interrupted", "RUN_INTERRUPTED"))

    async def test_cancel_of_an_orphaned_run_ends_interrupted(self) -> None:
        _, app = self.make()
        thread_id = new_id()
        old = await self._orphan(thread_id, expires_in_s=-1)
        await self._cancel_interrupted(app, thread_id, old, revision=4)
        record = self.read_json(self.run_dir / "runs" / f"{old}.json")
        self.assertEqual((record["status"], record["code"], record["lease"]), ("Interrupted", "RUN_INTERRUPTED", None))
        self.assertIsNone(self.read_json(self.run_dir / "threads" / f"{thread_id}.json")["activeRunId"])
        before = self.stored()
        await self._cancel_interrupted(app, thread_id, old, revision=4)
        self.assertEqual(self.stored(), before)

    async def test_cancel_after_an_ask_reported_the_interruption_ends_interrupted(self) -> None:
        _, app = self.make()
        thread_id = new_id()
        old = await self._orphan(thread_id, expires_in_s=-1)
        await asgi_call(app, "POST", "/invocations", ask_body(thread_id, new_id()))
        before = self.stored()
        await self._cancel_interrupted(app, thread_id, old, revision=4)
        self.assertEqual(self.stored(), before)

    async def test_lease_renews_while_executing_and_clears_at_the_end(self) -> None:
        executor = GatedExecutor()
        _, app = self.make(executor=executor, lease_renew_s=0.05, lease_ttl_s=1.0)
        run_id = new_id()
        stream = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(new_id(), run_id)))
        await asyncio.wait_for(executor.started.wait(), 5)
        path = self.run_dir / "runs" / f"{run_id}.json"
        first = self.read_json(path)["lease"]
        await asyncio.sleep(0.2)
        later = self.read_json(path)
        assert isinstance(first, dict) and isinstance(later["lease"], dict)
        self.assertGreater(later["lease"]["renewedAt"], first["renewedAt"])
        self.assertEqual(later["status"], "Executing")
        executor.release.set()
        await stream
        final = self.read_json(path)
        self.assertEqual((final["status"], final["lease"]), ("Limited", None))


class SnapshotSizeTests(ControllerCase):
    def _view(self, steps: int, summary: str) -> RunView:
        return RunView(
            schema_=RUN_VIEW_SCHEMA, thread_id=new_id(), run_id=new_id(), revision=1, status="Executing", code=None,
            question="q", updated_at=iso_utc(utc_now()), plan=None, facts=[], observed=[], limitations=[],
            interpretation=None, hypotheses=None, withheld_reason=None, viz_specs=[],
            steps=[
                StepView(id=f"s{i + 1}", row=1, tool="cases_query", kind="server", folded=False, input_summary=summary,
                         status="Done", output_summary=summary, duration_ms=1, code=None)
                for i in range(steps)
            ],
        )

    def test_small_views_are_not_trimmed(self) -> None:
        view = self._view(5, "CGS risk category = High risk")
        self.assertEqual(events.fit_view(view), view.model_dump(mode="json"))

    def test_oversized_summaries_are_trimmed_under_32_kb(self) -> None:
        view = self._view(20, "x" * 5000)
        event = events.state_snapshot(view)
        frame = events.encode(event).encode()
        self.assertLessEqual(len(frame), SNAPSHOT_LIMIT_BYTES)
        snapshot = StateSnapshotFrame.model_validate_json(frame.decode()[len("data: "):]).snapshot
        self.assertTrue(all(len(s.input_summary) < 5000 and s.input_summary.endswith("…") for s in snapshot.steps))

    async def test_controller_snapshots_stay_under_32_kb_while_the_record_keeps_full_text(self) -> None:
        executor = GatedExecutor(output_summary="y" * 8000)
        executor.release.set()
        _, app = self.make(executor=executor)
        run_id = new_id()
        frames = check_stream(self, (await asgi_call(app, "POST", "/invocations", ask_body(new_id(), run_id))).text)
        self.assertTrue(snapshots(frames))
        record = self.read_json(self.run_dir / "runs" / f"{run_id}.json")
        self.assertTrue(all(len(step["outputSummary"]) == 8000 for step in record["steps"]))
