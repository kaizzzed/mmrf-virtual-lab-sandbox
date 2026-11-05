"""Run controller (RFA rev 25, A2 "Agent topology", A3 "Run lifecycle").

Each run executes as a detached asyncio.Task. The invocation registers the run and creates its
task without awaiting, then only drains a subscriber queue. When Starlette cancels the handler on
a client disconnect, the run keeps going and finishes in its record. One run is active per thread.
"""

import asyncio
import logging
import threading
from collections import defaultdict
from collections.abc import AsyncGenerator, Sequence
from datetime import datetime
from functools import partial
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import cast

from ag_ui.core import BaseEvent, RunAgentInput, RunErrorEvent, RunFinishedEvent, StepFinishedEvent, StepStartedEvent
from pydantic import ValidationError

from . import AGENT_VERSION, events, ir
from .compiler import Compiler, CompileStop
from .events import iso_utc, utc_now
from .ids import is_valid_id

# Re-exported: the run controller's collaborators are defined in interfaces.py.
from .interfaces import ExecutionResult as ExecutionResult
from .interfaces import Executor as Executor
from .interfaces import Planner as Planner
from .interfaces import ResumeState as ResumeState
from .interfaces import StepSession as StepSession
from .lease import is_expired, keep_renewed, owner_id
from .models import (
    END_STATUSES,
    ORION_ACTION,
    RESERVED_ACTIONS,
    WAITING_STATUSES,
    AskAction,
    CallRecord,
    CancelAction,
    Code,
    CohortRef,
    ConfirmationRecord,
    ConfirmPending,
    FactRecord,
    FinishStatus,
    Limitation,
    NearestApp,
    RunStatus,
    RunView,
    StepOrigin,
    StepStatus,
    WarmupAction,
)
from .run_store import Environment, RunRecord, RunStore, RunWriter, ThreadIndex
from .settings import Settings, fault_list

log = logging.getLogger("orion.controller")

TERMINAL_EVENTS = (RunFinishedEvent, RunErrorEvent)
STOPPED_SUMMARY = "Not run: stopped"
FAILED_SUMMARY = "Not run: the run failed"
INTERRUPTED_SUMMARY = "Not run: interrupted"
STOPPED_WITHHELD = "you stopped the run"
INTERRUPTED_WITHHELD = "the run was interrupted"
INTERNAL_MESSAGE = "ORION hit an internal error; the run record keeps every finished step."


class _InputError(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RunHandle:
    thread_id: str
    run_id: str
    writer: RunWriter | None = None
    task: asyncio.Task[None] | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: set[asyncio.Queue[BaseEvent]] = field(default_factory=set)
    last_snapshot: BaseEvent | None = None
    terminal: BaseEvent | None = None
    open_steps: list[str] = field(default_factory=list)

    @property
    def status(self) -> RunStatus:
        assert self.writer is not None
        return self.writer.record.status

    def subscribe(self) -> asyncio.Queue[BaseEvent]:
        queue: asyncio.Queue[BaseEvent] = asyncio.Queue()
        if self.terminal is not None:
            for event in (self.last_snapshot, self.terminal):
                if event is not None:
                    queue.put_nowait(event)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[BaseEvent]) -> None:
        self.subscribers.discard(queue)

    def publish(self, event: BaseEvent) -> None:
        if isinstance(event, TERMINAL_EVENTS):
            self.terminal = event
        for queue in tuple(self.subscribers):
            queue.put_nowait(event)


# Strong references: the event loop keeps only weak references to tasks, and a run must
# outlive the invocation that started it.
RUNS: dict[str, RunHandle] = {}
# Run ids whose task is alive. A run that pauses or ends has no task, so it does not keep
# the microVM busy.
_BUSY_RUNS: set[str] = set()
_BUSY_LOCK = threading.Lock()


def is_busy() -> bool:
    """Read from /ping, which Starlette runs in a worker thread."""
    with _BUSY_LOCK:
        return bool(_BUSY_RUNS)


def _set_busy(run_id: str, busy: bool) -> None:
    with _BUSY_LOCK:
        if busy:
            _BUSY_RUNS.add(run_id)
        else:
            _BUSY_RUNS.discard(run_id)


def reset_registry() -> None:
    """Forget every run handle; for tests that start each case on a fresh event loop."""
    RUNS.clear()
    with _BUSY_LOCK:
        _BUSY_RUNS.clear()


def terminal_for(record: RunRecord) -> BaseEvent:
    """The terminal event for a run that is paused or ended. Interrupted ends RUN_FINISHED, like every end but Failed."""
    if record.status == "Failed":
        return events.run_error(record.code or "INTERNAL_ERROR", "The run failed.")
    return events.run_finished(record.thread_id, record.run_id, cast(FinishStatus, record.status), record.code)


def _set_step(record: RunRecord, step_id: str, **update: object) -> None:
    record.steps = [s.model_copy(update=update) if s.id == step_id else s for s in record.steps]


def _keep_step_outputs(
    record: RunRecord, facts: Sequence[FactRecord], calls: Sequence[CallRecord], cohorts: Sequence[CohortRef]
) -> None:
    record.calls.extend(calls)
    record.facts.extend(facts)
    for cohort in cohorts:
        record.cohorts = [cohort if c.ref == cohort.ref else c for c in record.cohorts]
        if all(c.ref != cohort.ref for c in record.cohorts):
            record.cohorts.append(cohort)


def _carried_facts(record: RunRecord, step_id: str, row: int, origin: StepOrigin) -> list[FactRecord]:
    """The origin step's facts for a Reused step, renamed to its id and row; values and hashes unchanged."""
    sources = [record.facts, *(version.facts for version in record.superseded)]
    found = [f for facts in sources for f in facts if f.plan_version == origin.plan_version and f.step_id == origin.step_id]
    prefix, renamed = f"f.{origin.step_id}.", f"f.{step_id}."
    return [
        f.model_copy(update={"id": renamed + f.id.removeprefix(prefix), "step_id": step_id, "row": row})
        for f in {f.id: f for f in found}.values()
    ]


class _Session:
    def __init__(self, handle: RunHandle, resume: ResumeState | None = None) -> None:
        self._handle = handle
        self._resume = resume

    @property
    def _writer(self) -> RunWriter:
        assert self._handle.writer is not None
        return self._handle.writer

    @property
    def stop_requested(self) -> bool:
        return self._handle.stop.is_set()

    @property
    def resume(self) -> ResumeState | None:
        return self._resume

    async def wait_stop(self) -> None:
        await self._handle.stop.wait()

    async def start_step(self, step_id: str) -> None:
        self._handle.publish(events.step_started(step_id))
        self._handle.open_steps.append(step_id)
        await self._writer.transition(lambda r: _set_step(r, step_id, status="Running"))

    async def retrying(self, step_id: str, *, attempt: int, backoff_ms: int) -> None:
        log.info("step %s retrying: attempt %d after %d ms", step_id, attempt, backoff_ms)
        await self._writer.transition(lambda r: _set_step(r, step_id, status="Retrying"))

    async def finish_step(
        self,
        step_id: str,
        status: StepStatus,
        output_summary: str | None,
        duration_ms: int | None,
        code: Code | None,
        *,
        facts: Sequence[FactRecord] = (),
        calls: Sequence[CallRecord] = (),
        cohorts: Sequence[CohortRef] = (),
        limitations: Sequence[Limitation] = (),
    ) -> None:
        self._handle.open_steps.remove(step_id)
        self._handle.publish(events.step_finished(step_id))

        def finish(r: RunRecord) -> None:
            _set_step(r, step_id, status=status, output_summary=output_summary, duration_ms=duration_ms, code=code)
            _keep_step_outputs(r, facts, calls, cohorts)
            r.answer.limitations.extend(item for item in limitations if item not in r.answer.limitations)

        await self._writer.transition(finish)

    async def skip_step(
        self, step_id: str, code: Code | None, reason: str, *, calls: Sequence[CallRecord] = ()
    ) -> None:
        if step_id in self._handle.open_steps:
            self._handle.open_steps.remove(step_id)
            self._handle.publish(events.step_finished(step_id))

        def skip(r: RunRecord) -> None:
            _set_step(r, step_id, status="Skipped", output_summary=reason, code=code)
            r.calls.extend(calls)

        await self._writer.transition(skip)

    async def reuse_step(self, step_id: str, *, origin: StepOrigin) -> None:
        def reuse(r: RunRecord) -> None:
            row = next(s.row for s in r.steps if s.id == step_id)
            _set_step(r, step_id, status="Reused", reused_from=origin)
            r.facts.extend(_carried_facts(r, step_id, row, origin))

        await self._writer.transition(reuse)


def _cancel_mark(r: RunRecord) -> None:
    """End a paused or expired-Stopping run Cancelled: open steps Skipped, a pending nonce voided."""
    r.status, r.code = "Cancelled", None
    r.steps = [
        s.model_copy(update={"status": "Skipped", "output_summary": STOPPED_SUMMARY})
        if s.status in ("Queued", "AwaitingUser", "Running", "Retrying")
        else s
        for s in r.steps
    ]
    if isinstance(r.pending, ConfirmPending):
        r.confirmations.append(
            ConfirmationRecord(
                nonce=r.pending.nonce, decision=None, saved=[], make_active=None, at=iso_utc(utc_now()), outcome="voided"
            )
        )
    r.pending = None
    if r.answer.observed and r.answer.withheld_reason is None:
        r.answer.withheld_reason = STOPPED_WITHHELD


class Controller:
    def __init__(
        self, settings: Settings, store: RunStore, compiler: Compiler, planner: Planner, executor: Executor
    ) -> None:
        self.settings = settings
        self.store = store
        self.compiler = compiler
        self.planner = planner
        self.executor = executor
        self.owner = owner_id()
        self._thread_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def busy(self) -> bool:
        return is_busy()

    # -- invocation ------------------------------------------------------------

    async def handle(self, run_input: RunAgentInput) -> AsyncGenerator[BaseEvent, None]:
        """One invocation: RUN_STARTED first, then exactly one RUN_FINISHED or RUN_ERROR, never an exception."""
        if not (is_valid_id(run_input.thread_id) and is_valid_id(run_input.run_id)):
            # RUN_STARTED would have to carry the invalid ids, which the contract's frame schema rejects.
            yield events.run_error("INVALID_INPUT", "threadId and runId must be 32 lowercase hex characters.")
            return
        yield events.run_started(run_input.thread_id, run_input.run_id)
        try:
            async with aclosing(self._dispatch(run_input)) as stream:
                async for event in stream:
                    yield event
                    if isinstance(event, TERMINAL_EVENTS):
                        return
        except Exception:
            log.exception("invocation failed: thread %s run %s", run_input.thread_id, run_input.run_id)
        yield events.run_error("INTERNAL_ERROR", INTERNAL_MESSAGE)

    def _parse(self, run_input: RunAgentInput) -> AskAction | WarmupAction | CancelAction:
        props = run_input.forwarded_props
        orion = props.get("orion") if isinstance(props, dict) else None
        if not isinstance(orion, dict):
            raise _InputError("INVALID_INPUT", "forwardedProps.orion must be an object with an action.")
        action = orion.get("action")
        if isinstance(action, str) and action in RESERVED_ACTIONS:
            raise _InputError("NOT_IMPLEMENTED", f"The {action} action is not implemented yet.")
        try:
            request = ORION_ACTION.validate_python(orion)
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors(include_input=False, include_url=False)[:3]
            )
            raise _InputError("INVALID_INPUT", f"Invalid forwardedProps.orion: {details}") from None
        if not isinstance(request, (AskAction, WarmupAction, CancelAction)):
            # Unreachable while RESERVED_ACTIONS names every other action.
            raise _InputError("NOT_IMPLEMENTED", f"The {request.action} action is not implemented yet.")
        return request

    async def _dispatch(self, run_input: RunAgentInput) -> AsyncGenerator[BaseEvent, None]:
        thread_id, run_id = run_input.thread_id, run_input.run_id
        try:
            request = self._parse(run_input)
        except _InputError as error:
            yield events.run_error(error.code, error.message)
            return
        if isinstance(request, WarmupAction):
            yield events.run_finished(thread_id, run_id, "Warmed", None)
            return
        stream = self._ask(thread_id, run_id, request) if isinstance(request, AskAction) else self._cancel(thread_id, run_id)
        async with aclosing(stream) as inner:
            async for event in inner:
                yield event

    async def _drain(self, handle: RunHandle, queue: asyncio.Queue[BaseEvent]) -> AsyncGenerator[BaseEvent, None]:
        loop = asyncio.get_running_loop()
        interval = self.settings.heartbeat_s
        next_beat = loop.time() + interval
        started: set[str] = set()
        try:
            while True:
                try:
                    async with asyncio.timeout_at(next_beat):
                        event = await queue.get()
                except TimeoutError:
                    next_beat += interval
                    yield events.heartbeat(utc_now())
                    continue
                if isinstance(event, StepStartedEvent):
                    started.add(event.step_name)
                elif isinstance(event, StepFinishedEvent) and event.step_name not in started:
                    # A joining invocation (cancel) missed this step's start; its snapshot carries the row.
                    continue
                yield event
                if isinstance(event, TERMINAL_EVENTS):
                    return
        finally:
            handle.unsubscribe(queue)

    # -- ask --------------------------------------------------------------------

    async def _ask(self, thread_id: str, run_id: str, ask: AskAction) -> AsyncGenerator[BaseEvent, None]:
        started: tuple[RunHandle, asyncio.Queue[BaseEvent]] | None = None
        reports: list[BaseEvent] = []
        async with self._thread_locks[thread_id]:
            index = await self.store.load_thread(thread_id)
            if run_id in index.run_ids or run_id in RUNS or await self.store.load_run(run_id) is not None:
                rejection = events.run_error("INVALID_INPUT", "This runId was already used; send a new one.")
            elif await self._thread_busy(index, reports):
                rejection = events.run_error("RUN_BUSY", "Another run is active in this thread; cancel it or wait for it to end.")
            else:
                started = self._launch(thread_id, run_id, ask, reports)
        if started is None:
            yield rejection
            return
        async with aclosing(self._drain(*started)) as stream:
            async for event in stream:
                yield event

    async def _thread_busy(self, index: ThreadIndex, reports: list[BaseEvent]) -> bool:
        """Under the thread lock: is a run in this thread still live?

        An active run whose lease expired ends Interrupted first, and its snapshot goes into
        `reports`, so this turn shows it Interrupted (A2 "Disconnects").
        """
        # A run launched here may not have written the thread index yet.
        if any(h.thread_id == index.thread_id and h.status not in END_STATUSES for h in RUNS.values()):
            return True
        active = index.active_run_id
        if active is None:
            return False
        record = await self.store.load_run(active)
        if record is not None and record.status in WAITING_STATUSES:
            return True
        if record is not None and record.status not in END_STATUSES:
            if not is_expired(record.lease, utc_now()):
                return True
            reports.append(events.state_snapshot(await self._expire(record)))
        index.active_run_id = None
        await self.store.save_thread(index)
        return False

    async def _expire(self, record: RunRecord) -> RunView:
        """Record a run whose lease lapsed: a Stopping run ends Cancelled, any other ends Interrupted.

        updatedAt is the lease's expiry (or the record's updatedAt without a lease), so the record
        equals what the BFF view route showed for it (contract "Expired-lease rule").
        """
        at = datetime.fromisoformat((record.lease.expires_at if record.lease else record.updated_at).replace("Z", "+00:00"))
        writer = RunWriter(self.store, record, self.owner, self.settings.lease_ttl_s)
        if record.status == "Stopping":
            log.info("run %s cancelled: its Stopping lease expired", record.run_id)
            return await writer.transition(_cancel_mark, at=at)

        def mark(r: RunRecord) -> None:
            r.status, r.code = "Interrupted", "RUN_INTERRUPTED"
            r.steps = [
                s.model_copy(update={"status": "Failed", "code": "RUN_INTERRUPTED"})
                if s.status in ("Running", "Retrying")
                else s.model_copy(update={"status": "Skipped", "output_summary": INTERRUPTED_SUMMARY})
                if s.status == "Queued"
                else s
                for s in r.steps
            ]
            r.answer.limitations.append(
                Limitation(
                    code="RUN_INTERRUPTED",
                    text="The run stopped responding before it finished; completed steps are shown as partial.",
                    unblock="Ask again.",
                )
            )
            if r.answer.observed and r.answer.withheld_reason is None:
                r.answer.withheld_reason = INTERRUPTED_WITHHELD
            r.pending = None

        log.info("run %s interrupted: lease expired", record.run_id)
        return await writer.transition(mark, at=at)

    def _launch(
        self, thread_id: str, run_id: str, ask: AskAction, preface: list[BaseEvent]
    ) -> tuple[RunHandle, asyncio.Queue[BaseEvent]]:
        """Register the run and create its task without awaiting.

        A disconnect cancels the invocation at an await, so it can no longer leave a run that
        has a record or a busy flag but no task. The task writes the record and the index.
        `preface` events reach the subscriber before any event of this run.
        """
        now = iso_utc(utc_now())
        record = RunRecord(
            thread_id=thread_id,
            run_id=run_id,
            status="Planning",
            question=ask.question,
            created_at=now,
            updated_at=now,
            environment=Environment(
                mode=self.settings.mode,
                planner=self.settings.planner,
                executor=self.settings.executor,
                agent_version=AGENT_VERSION,
                faults=fault_list(self.settings.faults),
            ),
            request=ask,
        )
        handle = RunHandle(thread_id=thread_id, run_id=run_id)

        async def publish(view: RunView) -> None:
            handle.last_snapshot = events.state_snapshot(view)
            handle.publish(handle.last_snapshot)

        handle.writer = RunWriter(self.store, record, self.owner, self.settings.lease_ttl_s, publish)
        queue = handle.subscribe()
        for event in preface:
            queue.put_nowait(event)
        RUNS[run_id] = handle
        _set_busy(run_id, True)
        handle.task = asyncio.create_task(self._run(handle), name=f"orion-run-{run_id}")
        log.info("run %s started in thread %s", run_id, thread_id)
        return handle, queue

    # -- the detached run ---------------------------------------------------------

    async def _run(self, handle: RunHandle) -> None:
        assert handle.writer is not None
        lease = asyncio.create_task(keep_renewed(handle.writer, self.settings.lease_renew_s))
        try:
            await self._open(handle)
            await self._plan_and_execute(handle)
        except Exception:
            log.exception("run %s failed", handle.run_id)
            await self._fail(handle)
        finally:
            lease.cancel()
            RUNS.pop(handle.run_id, None)
            _set_busy(handle.run_id, False)

    async def _open(self, handle: RunHandle) -> None:
        """Save the Planning record, then point the thread index at it."""
        assert handle.writer is not None
        await handle.writer.transition(lambda r: None)
        async with self._thread_locks[handle.thread_id]:
            index = await self.store.load_thread(handle.thread_id)
            index.run_ids.append(handle.run_id)
            index.active_run_id = handle.run_id
            await self.store.save_thread(index)

    async def _plan_and_execute(self, handle: RunHandle) -> None:
        assert handle.writer is not None
        writer = handle.writer
        ask = writer.record.request
        result = await self.planner.plan(ask.question, ask.cohort_context, None)
        output = result.output

        def keep_plan_call(r: RunRecord) -> None:
            r.planner_output = output
            r.model_calls.extend(result.model_calls)

        await writer.stage(keep_plan_call)
        if handle.stop.is_set():
            return await self._finish(handle, "Cancelled", None)
        if output is None:
            return await self._finish(handle, "Failed", "PLANNER_DECLINED")
        if isinstance(output, ir.Unsupported):
            return await self._finish(handle, "Unsupported", output.reason, [self._unsupported_limitation(output)])
        if isinstance(output, ir.Clarify):
            note = Limitation(code="NOT_IMPLEMENTED", text="Clarifying questions land Day 1 PM.", unblock="Rephrase with the field to use.")
            return await self._finish(handle, "Limited", "NOT_IMPLEMENTED", [note])
        compiled = self.compiler.compile(output, ask.cohort_context, None)
        if isinstance(compiled, CompileStop):
            return await self._finish(handle, "Limited", compiled.code, compiled.limitations)

        def show_plan(r: RunRecord) -> None:
            r.plan_versions.append(compiled)
            r.steps = [step.queued() for step in compiled.steps]
            r.answer.limitations = list(compiled.limitations)
            if r.status == "Planning":
                r.status = "Executing"

        await writer.transition(show_plan)
        executed = await self.executor.execute(compiled, _Session(handle))
        if handle.stop.is_set():
            await self._skip_queued(handle, STOPPED_SUMMARY)
            return await self._finish(handle, "Cancelled", None)
        await self._finish(handle, executed.status, executed.code, executed.limitations)

    def _unsupported_limitation(self, output: ir.Unsupported) -> Limitation:
        texts = {
            "UNSUPPORTED_ANALYSIS": "ORION cannot run this analysis. It compares survival, ranks mutated genes, runs differential expression and counts cohorts.",
            "CLINICAL_ADVICE": "ORION gives no clinical or treatment advice.",
            "OUT_OF_SCOPE": "This question is outside what ORION covers in the Virtual Lab.",
        }
        # The Unsupported card names the first nearest app and offers Open on this cohort for it.
        nearest = [NearestApp.of(app_id) for app_id in output.nearest_app_ids]
        return Limitation(code=output.reason, text=texts[output.reason], unblock=None, nearest_apps=nearest)

    async def _skip_queued(self, handle: RunHandle, summary: str) -> None:
        assert handle.writer is not None

        def skip(r: RunRecord) -> None:
            r.steps = [
                s.model_copy(update={"status": "Skipped", "output_summary": summary}) if s.status == "Queued" else s
                for s in r.steps
            ]

        if any(s.status == "Queued" for s in handle.writer.record.steps):
            await handle.writer.transition(skip)

    async def _finish(
        self, handle: RunHandle, status: RunStatus, code: Code | None, limitations: list[Limitation] | None = None
    ) -> None:
        assert handle.writer is not None

        def end(r: RunRecord) -> None:
            r.status, r.code = status, code
            for item in limitations or []:
                if item not in r.answer.limitations:
                    r.answer.limitations.append(item)

        await handle.writer.transition(end)
        if status in END_STATUSES:
            await self._clear_active(handle.thread_id, handle.run_id)
        handle.publish(terminal_for(handle.writer.record))
        log.info("run %s ended %s %s", handle.run_id, status, code or "")

    async def _fail(self, handle: RunHandle) -> None:
        """Close every open step, skip the queued ones, end the run Failed, and still send exactly one RUN_ERROR."""
        try:
            assert handle.writer is not None
            for step_id in list(handle.open_steps):
                handle.publish(events.step_finished(step_id))
                await handle.writer.transition(partial(_set_step, step_id=step_id, status="Failed", code="INTERNAL_ERROR"))
            handle.open_steps.clear()
            await self._skip_queued(handle, FAILED_SUMMARY)
            await self._finish(handle, "Failed", "INTERNAL_ERROR")
        except Exception:
            log.exception("run %s could not record its failure", handle.run_id)
            if handle.terminal is None:
                handle.publish(events.run_error("INTERNAL_ERROR", INTERNAL_MESSAGE))

    async def _clear_active(self, thread_id: str, run_id: str) -> None:
        async with self._thread_locks[thread_id]:
            index = await self.store.load_thread(thread_id)
            if index.active_run_id == run_id:
                index.active_run_id = None
                await self.store.save_thread(index)

    # -- cancel -------------------------------------------------------------------

    async def _cancel(self, thread_id: str, run_id: str) -> AsyncGenerator[BaseEvent, None]:
        follow: tuple[RunHandle, asyncio.Queue[BaseEvent]] | None = None
        replies: list[BaseEvent] = []
        async with self._thread_locks[thread_id]:
            handle = RUNS.get(run_id)
            record = handle.writer.record if handle and handle.writer else await self.store.load_run(run_id)
            if record is None or record.thread_id != thread_id:
                replies.append(events.run_error("RUN_NOT_FOUND", "No run with this runId exists in this thread."))
            elif handle is not None and handle.status not in END_STATUSES:
                already = handle.status == "Stopping"
                follow = (handle, handle.subscribe())
                if already and handle.last_snapshot is not None:
                    # A joining invocation first sends the run's current snapshot.
                    follow[1].put_nowait(handle.last_snapshot)
            else:
                replies = await self._cancel_at_rest(record)
        if follow is not None:
            await self._request_stop(follow[0])
            async with aclosing(self._drain(*follow)) as stream:
                async for event in stream:
                    yield event
            return
        for event in replies:
            yield event

    async def _request_stop(self, handle: RunHandle) -> None:
        """Stopping shows at once; the run ends Cancelled at its next step boundary."""
        assert handle.writer is not None
        handle.stop.set()

        def stopping(r: RunRecord) -> bool:
            if r.status in END_STATUSES or r.status == "Stopping":
                return False
            r.status = "Stopping"
            return True

        await handle.writer.transition(stopping)

    async def _cancel_at_rest(self, record: RunRecord) -> list[BaseEvent]:
        """Cancel a run that has no live task here: ended, paused, or orphaned by a dead controller."""
        if record.status in END_STATUSES:
            return [events.state_snapshot(record.view()), terminal_for(record)]
        writer = RunWriter(self.store, record, self.owner, self.settings.lease_ttl_s)
        if record.status in WAITING_STATUSES:
            view = await writer.transition(_cancel_mark)
        elif is_expired(record.lease, utc_now()):
            view = await self._expire(record)
        else:
            return [events.run_error("RUN_BUSY", "This run is owned by another controller; try again shortly.")]
        index = await self.store.load_thread(record.thread_id)
        if index.active_run_id == record.run_id:
            index.active_run_id = None
            await self.store.save_thread(index)
        return [events.state_snapshot(view), terminal_for(record)]

