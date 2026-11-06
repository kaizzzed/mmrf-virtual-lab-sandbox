"""Cancel races, lease renewal after a store error, planless edits and reports on turns that change no status.

Each case pins a defect the review found (controller-1 to controller-5): a cancel whose Stopping save is
cut short or fails, a cancel that lands while a pause is being saved, a run that reaches its end while a
Stopping save is in flight, one failed renewal, an edit of a run that never had a plan, and ride-along
reports on a cancel of an ended run or a turn that finds an expired lease.
"""

import asyncio
import contextlib
from collections.abc import Collection
from datetime import timedelta
from pathlib import Path
from typing import Any

from ag_ui.core import BaseEvent, RunAgentInput
from pydantic import JsonValue
from support import FIELDS_PATH, ControllerCase, GatedExecutor, action_body, ask_body, vocabulary
from test_engine_fakes import CGS, FakePlanner, j1_ir, orion_of
from test_engine_streams import EngineCase

from orion_agent import controller as controller_module
from orion_agent import ir
from orion_agent.compiler import CompiledPlan, Compiler
from orion_agent.controller import Controller, ExecutionResult, StepSession
from orion_agent.events import iso_utc, utc_now
from orion_agent.ids import new_id
from orion_agent.lease import Lease
from orion_agent.models import Limitation, RunStatus
from orion_agent.run_store import RunRecord, RunStore
from orion_agent.settings import Settings


class HeldStore(RunStore):
    """Holds the next save of a record in one of `statuses` until released (an S3 PUT in flight), or fails it."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.statuses: Collection[RunStatus] = ()
        self.fail = False
        self.in_save = asyncio.Event()
        self.release = asyncio.Event()

    def arm(self, *statuses: RunStatus, fail: bool = False) -> None:
        self.statuses, self.fail = statuses, fail

    async def save_run(self, record: RunRecord) -> None:
        if record.status in self.statuses:
            self.statuses = ()
            self.in_save.set()
            if self.fail:
                raise OSError("simulated store write failure")
            await self.release.wait()
        await super().save_run(record)


class FlakyStore(RunStore):
    fail_next = False

    async def save_run(self, record: RunRecord) -> None:
        if self.fail_next:
            self.fail_next = False
            raise OSError("simulated transient store error")
        await super().save_run(record)


class FinishingExecutor:
    """Returns Limited once released, without a step transition or a look at the stop: a run that reaches its end
    on its own while a cancel's Stopping save holds the record writer."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, plan: CompiledPlan, session: StepSession) -> ExecutionResult:
        self.started.set()
        await self.release.wait()
        note = Limitation(code="NOT_IMPLEMENTED", text="Test double: no data was read.", unblock=None)
        return ExecutionResult(status="Limited", code="NOT_IMPLEMENTED", limitations=[note])


def clarify_ir() -> ir.Clarify:
    return ir.Clarify(kind="clarify", slot="split_term", option_ids=[CGS, "diagnoses.iss_stage"])


async def collect(controller: Controller, body: dict[str, JsonValue], out: list[BaseEvent]) -> None:
    async for event in controller.handle(RunAgentInput.model_validate(body)):
        out.append(event)


class CancelRaceTests(ControllerCase):
    def held(self, executor: Any = None, planner: Any = None) -> tuple[Controller, HeldStore]:  # Any: test doubles
        store = HeldStore(self.run_dir)
        settings = Settings(run_dir=self.run_dir, fields_path=FIELDS_PATH)
        controller = Controller(
            settings=settings, store=store, compiler=Compiler(vocabulary(), "active_cohort"),
            planner=planner or FakePlanner([j1_ir()]), executor=executor or GatedExecutor(),
        )
        return controller, store

    async def test_a_cancel_cut_off_during_its_stopping_save_still_stops_the_run(self) -> None:
        executor = GatedExecutor()
        controller, store = self.held(executor)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(collect(controller, ask_body(thread_id, run_id), []))
        await asyncio.wait_for(executor.started.wait(), 5)
        store.arm("Stopping")
        cancel = asyncio.create_task(collect(controller, action_body(thread_id, run_id, {"action": "cancel"}), []))
        await asyncio.wait_for(store.in_save.wait(), 5)
        cancel.cancel()  # the client went away, or the BFF preempted the turn, while Stopping was being saved
        with contextlib.suppress(asyncio.CancelledError):
            await cancel
        handle = controller_module.RUNS[run_id]
        self.assertTrue(handle.stop.is_set() and handle.abort.is_set(), "the stop is signalled although the cancel is gone")
        store.release.set()
        executor.release.set()
        await asyncio.wait_for(ask, 10)
        record = await store.load_run(run_id)
        assert record is not None
        self.assertEqual(record.status, "Cancelled")
        self.assertEqual([s.status for s in record.steps][1:], ["Skipped"] * (len(record.steps) - 1))
        self.assertIn(("cancel", "accepted"), [(a.action, a.outcome) for a in record.actions])

    async def test_a_failed_stopping_save_still_stops_the_run_and_the_cancel_follows_it(self) -> None:
        executor = GatedExecutor()
        controller, store = self.held(executor)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(collect(controller, ask_body(thread_id, run_id), []))
        await asyncio.wait_for(executor.started.wait(), 5)
        store.arm("Stopping", fail=True)
        frames: list[BaseEvent] = []
        cancel = asyncio.create_task(collect(controller, action_body(thread_id, run_id, {"action": "cancel"}), frames))
        await asyncio.wait_for(store.in_save.wait(), 5)
        executor.release.set()
        await asyncio.wait_for(cancel, 10)
        await asyncio.wait_for(ask, 10)
        last = frames[-1]
        self.assertEqual(type(last).__name__, "RunFinishedEvent", f"not INTERNAL_ERROR: {last}")
        self.assertEqual(getattr(last, "result", {}).get("status"), "Cancelled")
        record = await store.load_run(run_id)
        assert record is not None
        self.assertEqual(record.status, "Cancelled")

    async def test_a_run_that_ends_while_stopping_is_saved_ends_cancelled(self) -> None:
        executor = FinishingExecutor()
        controller, store = self.held(executor)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(collect(controller, ask_body(thread_id, run_id), []))
        await asyncio.wait_for(executor.started.wait(), 5)
        store.arm("Stopping")
        frames: list[BaseEvent] = []
        cancel = asyncio.create_task(collect(controller, action_body(thread_id, run_id, {"action": "cancel"}), frames))
        await asyncio.wait_for(store.in_save.wait(), 5)
        executor.release.set()  # the run finishes its plan and tries to end Limited behind the Stopping save
        await asyncio.sleep(0.05)
        store.release.set()
        await asyncio.wait_for(cancel, 10)
        await asyncio.wait_for(ask, 10)
        record = await store.load_run(run_id)
        assert record is not None
        self.assertEqual((record.status, record.code), ("Cancelled", None))
        views = [e for e in frames if type(e).__name__ == "StateSnapshotEvent"]
        self.assertEqual([getattr(v, "snapshot")["status"] for v in views], ["Stopping", "Cancelled"])

    async def test_a_cancel_while_a_pause_is_being_saved_ends_the_run_cancelled(self) -> None:
        controller, store = self.held(planner=FakePlanner([clarify_ir()]))
        store.arm("Clarifying")
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(collect(controller, ask_body(thread_id, run_id), []))
        await asyncio.wait_for(store.in_save.wait(), 5)
        frames: list[BaseEvent] = []
        cancel = asyncio.create_task(collect(controller, action_body(thread_id, run_id, {"action": "cancel"}), frames))
        await asyncio.sleep(0.05)
        store.release.set()
        await asyncio.wait_for(cancel, 10)
        await asyncio.wait_for(ask, 10)
        kinds = [type(e).__name__ for e in frames]
        self.assertEqual(kinds, ["RunStartedEvent", "StateSnapshotEvent", "RunFinishedEvent"])
        self.assertEqual(getattr(frames[1], "snapshot")["status"], "Cancelled")
        self.assertEqual(getattr(frames[2], "result")["status"], "Cancelled")
        record = await store.load_run(run_id)
        assert record is not None
        self.assertEqual((record.status, record.lease, record.pending), ("Cancelled", None, None))
        self.assertIsNone((await store.load_thread(thread_id)).active_run_id)
        again: list[BaseEvent] = []
        await collect(controller, ask_body(thread_id, new_id()), again)
        self.assertNotEqual(getattr(again[-1], "code", None), "RUN_BUSY", "the thread is free at once")


class LeaseRenewalTests(ControllerCase):
    async def test_one_failed_renewal_does_not_end_renewals(self) -> None:
        executor = GatedExecutor()
        store = FlakyStore(self.run_dir)
        settings = Settings(run_dir=self.run_dir, fields_path=FIELDS_PATH, lease_renew_s=0.05, lease_ttl_s=0.5)
        controller = Controller(
            settings=settings, store=store, compiler=Compiler(vocabulary(), "active_cohort"), planner=FakePlanner([j1_ir()]),
            executor=executor,
        )
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(collect(controller, ask_body(thread_id, run_id), []))
        await asyncio.wait_for(executor.started.wait(), 5)
        store.fail_next = True  # the run is parked in a step, so the next save is a renewal
        await self.wait_for(lambda: not store.fail_next)
        record = await store.load_run(run_id)
        assert record is not None and record.lease is not None
        after_failure = record.lease.renewed_at
        await asyncio.sleep(0.3)
        record = await store.load_run(run_id)
        assert record is not None and record.lease is not None
        self.assertGreater(record.lease.renewed_at, after_failure, "renewals go on after a failed one")
        executor.release.set()
        await asyncio.wait_for(ask, 10)


class PlanlessEditTests(EngineCase):
    async def test_an_edit_of_a_run_that_ended_before_planning_is_invalid_input(self) -> None:
        _, app = self.build([clarify_ir()])
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id), run_id)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "clarify_answer", "optionId": CGS}), run_id)
        self.assertEqual(frames[-1]["result"], {"status": "Limited", "code": "AMBIGUOUS_REQUEST"})
        self.assertEqual(self.record(run_id)["planVersions"], [])
        await self.assert_rejected(app, action_body(thread_id, run_id, {"action": "edit_plan", "field": "topN", "value": 5}), "INVALID_INPUT")


class ReportOnQuietTurnTests(EngineCase):
    def report(self, run_id: str, viz_id: str) -> dict[str, JsonValue]:
        return {"runId": run_id, "vizId": viz_id, "mounted": True, "error": None, "pinnedHashVerified": True}

    async def expire(self, run_id: str) -> None:
        """Leave a finished run Executing under a lease that lapsed (its controller is gone)."""
        raw = self.record(run_id)
        now = utc_now()
        lease = Lease(owner="dead:1", renewed_at=iso_utc(now - timedelta(seconds=200)), expires_at=iso_utc(now - timedelta(seconds=20)))
        raw["status"], raw["lease"] = "Executing", lease.model_dump(mode="json")
        await RunStore(self.run_dir).save_run(RunRecord.model_validate(raw))

    async def test_a_cancel_of_an_ended_run_records_its_reports(self) -> None:
        _, app = self.build()
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id), run_id)
        revision = self.record(run_id)["revision"]
        body = action_body(thread_id, run_id, {"action": "cancel"})
        orion_of(body)["vizReports"] = [self.report(run_id, "v1")]
        frames = await self.turn(app, body, run_id)
        self.assertEqual(frames[-1]["result"]["status"], "Complete")
        record = self.record(run_id)
        self.assertEqual([(v["vizId"], v["mounted"]) for v in record["vizReports"]], [("v1", True)])
        self.assertEqual(record["revision"], revision, "a report changes no view field")

    async def test_a_report_with_a_long_viewer_error_is_clipped_and_never_rejects_the_turn(self) -> None:
        _, app = self.build()
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id), run_id)
        long_error = "Direct DE viewer failed: " + "x" * 400 + "; the DEinput fallback failed: " + "y" * 300
        body = action_body(thread_id, run_id, {"action": "cancel"})
        orion_of(body)["vizReports"] = [{**self.report(run_id, "v1"), "mounted": False, "error": long_error}]
        frames = await self.turn(app, body, run_id)
        self.assertEqual(frames[-1]["type"], "RUN_FINISHED", "a Stop is never refused for a viewer's long error")
        kept = self.record(run_id)["vizReports"][0]["error"]
        self.assertEqual((len(kept), kept[:25], kept[-1]), (500, long_error[:25], "…"))
        standalone = action_body(thread_id, run_id, {"action": "viz_report", "vizId": "v1", "mounted": False, "error": long_error,
                                                     "pinnedHashVerified": None})
        frames = await self.turn(app, standalone, run_id)
        self.assertEqual(frames[-1]["result"], {"status": "Recorded", "code": None})

    async def test_a_turn_that_finds_an_expired_lease_records_its_reports(self) -> None:
        _, app = self.build()
        thread_id, first, orphan = new_id(), new_id(), new_id()
        await self.turn(app, ask_body(thread_id, first), first)
        await self.turn(app, ask_body(thread_id, orphan), orphan)
        await self.expire(orphan)
        body = action_body(thread_id, orphan, {"action": "cancel"})
        orion_of(body)["vizReports"] = [self.report(first, "v1"), self.report(orphan, "v1")]
        frames = await self.turn(app, body, orphan)
        self.assertEqual(frames[-1]["result"]["status"], "Interrupted")
        self.assertEqual([v["vizId"] for v in self.record(first)["vizReports"]], ["v1"], "another run's report is written")
        self.assertEqual([v["vizId"] for v in self.record(orphan)["vizReports"]], ["v1"], "the target's goes into the expiry")

    async def test_a_standalone_report_on_an_expired_run_is_recorded_with_the_expiry(self) -> None:
        _, app = self.build()
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id), run_id)
        await self.expire(run_id)
        body = action_body(thread_id, run_id, {"action": "viz_report", "vizId": "v1", "mounted": False, "error": "no WebGL",
                                               "pinnedHashVerified": None})
        frames = await self.turn(app, body, run_id)
        self.assertEqual(frames[-1]["result"]["status"], "Interrupted")
        record = self.record(run_id)
        self.assertEqual(record["status"], "Interrupted")
        self.assertEqual([(v["vizId"], v["mounted"], v["error"]) for v in record["vizReports"]], [("v1", False, "no WebGL")])

