"""Run controller (RFA rev 25, A2 "Agent topology", A3 "Run lifecycle"; contract "Actions" and "Invocation outcomes").

Each run executes as a detached asyncio.Task. The invocation registers the run and creates its
task without awaiting, then only drains a subscriber queue. When Starlette cancels the handler on
a client disconnect, the run keeps going and finishes in its record. One run is active per thread.

Every action goes through the contract's checks in order (1 ids, 2 schema, 3 the run exists, 4 an
expired lease, 5 busy, 6 the run can take it now, 7 it names what the run has, 8 the item is in the
right state). An accepted resume streams RUN_STARTED, the run's snapshot with the action applied,
the run's events to its next pause or end, and one terminal event. A rejection writes no record.

Record ownership ("Who writes a record"): the RunWriter of the task running a run; for a run at
rest, the invocation that accepted an action on it, under the thread lock. Reports and action-log
entries for a run live here go through its writer (staged); for a run leased elsewhere, the log.
"""

import asyncio
import contextlib
import logging
import threading
from collections import defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Final, Literal, Protocol, cast

from ag_ui.core import BaseEvent, RunAgentInput, RunErrorEvent, RunFinishedEvent, StepFinishedEvent, StepStartedEvent
from pydantic import BaseModel, JsonValue, ValidationError

from . import AGENT_VERSION, events, ir
from .catalog import ANALYSIS_APPS, NAMED_TOOL_APPS
from .compiler import CompiledPlan, CompileStop, SessionLedger, StepSpec
from .compiler import signature as plan_signature
from .events import iso_utc, utc_now
from .executor import (
    DECLINED_SUMMARY,
    DONT_SAVE_SUMMARY,
    FAILED_SUMMARY,
    INTERRUPTED_SUMMARY,
    STOPPED_SUMMARY,
    RunContext,
    input_hashes,
    plan_cohorts,
    reuse_map,
    verify_read_back,
)
from .ids import is_valid_id

# Re-exported: the run controller's collaborators are defined in interfaces.py.
from .interfaces import Clarification
from .interfaces import Composer as Composer
from .interfaces import ExecutionResult as ExecutionResult
from .interfaces import Executor as Executor
from .interfaces import ObservedRenderer, ObservedResult
from .interfaces import Planner as Planner
from .interfaces import PLANNER_MODEL, PlannerResult
from .interfaces import ResumeState as ResumeState
from .interfaces import SessionLedgerStore
from .interfaces import StepSession as StepSession
from .interfaces import Verifier as Verifier
from .lease import is_expired, keep_renewed, owner_id
from .model.answer import AnswerOutcome, write_answer
from .model.calls import ModelCallScope, model_call_scope
from .model.warmup import warm_up_roles
from .models import (
    ACTION_STATUSES,
    CODES,
    END_STATUSES,
    LEASED_STATUSES,
    MAX_TOP_N_DE,
    MAX_TOP_N_GENES,
    ORION_ACTION,
    STOP_GRACE_S,
    WAITING_STATUSES,
    ActionLogEntry,
    ActionName,
    AnswerRecord,
    AskAction,
    CallRecord,
    CancelAction,
    ClarifyAnswerAction,
    ClarifyOption,
    ClarifyPending,
    Code,
    CohortContext,
    CohortRef,
    ConfirmAction,
    ConfirmationRecord,
    ConfirmPending,
    ConsentAction,
    ConsentPending,
    DeThresholds,
    EditCohortNameAction,
    EditMethodAction,
    EditPlanAction,
    EditScopeAction,
    EditThresholdsAction,
    EditTopNAction,
    FactRecord,
    FinishStatus,
    Limitation,
    LedgerRefUse,
    LedgerUse,
    ModelCallRecord,
    NearestApp,
    OrionAction,
    PlanHistoryEntry,
    RetryStepAction,
    RunStatus,
    RunView,
    SaveCohortsArgs,
    StepOrigin,
    StepStatus,
    StepView,
    SupersededVersion,
    VizReport,
    VizReportAction,
    VizReportRecord,
    VizSpec,
    WarmupAction,
    model_call_id,
)
from .observed import TemplateRenderer, answer_markdown, save_skipped
from .run_store import Environment, RecordStore, RunRecord, RunWriter, ThreadIndex
from .settings import Settings, fault_list
from .tools.schemas import TOOL_PHRASES
from .vocab import Vocabulary

log = logging.getLogger("orion.controller")

TERMINAL_EVENTS = (RunFinishedEvent, RunErrorEvent)
STOPPED_WITHHELD: Final = "you stopped the run"
INTERRUPTED_WITHHELD: Final = "the run was interrupted"
FAILED_STEP_WITHHELD: Final = "a step failed"
LIMITED_WITHHELD: Final = "a limitation stopped the run"
RUN_FAILED_WITHHELD: Final = "the run failed"
NOT_CONFIGURED_WITHHELD: Final = "the interpretation model is not configured in this deployment"
CHECKS_WITHHELD: Final = "no interpretation sentence passed the checks"
ENDED_EARLY_SUMMARY: Final = "Not run: the run stopped early"
INTERNAL_MESSAGE: Final = "ORION hit an internal error; the run record keeps every finished step."
FAILURE_MESSAGES: Final[dict[str, str]] = {
    "WRITE_VERIFY_FAILED": "The saved cohorts did not match the plan when read back.",
    "PLANNER_DECLINED": "Both models declined to plan this question.",
    "INTERNAL_ERROR": INTERNAL_MESSAGE,
}
CLARIFY_QUESTIONS: Final[dict[str, str]] = {
    "split_term": "Which field should define the groups?",
    "cohort_field": "Which field defines the cohort you mean?",
    "gene": "Which gene do you mean?",
    "scope": "Which cohort should this run on?",
}
SCOPE_LABELS: Final[dict[str, str]] = {
    "all_cases": "All cases", "active_cohort": "The active cohort", "saved_cohort": "A saved cohort", "thread_cohort": "An earlier cohort",
}
READY: Final[frozenset[RunStatus]] = frozenset({"Answering", "Complete"})


class _ModelScope:
    """The run's model call scope (orion_agent.model.calls) around one planner or composer call."""

    def __init__(self, record: RunRecord) -> None:
        self._manager = model_call_scope(record.run_id, existing=list(record.model_calls))
        self.scope: ModelCallScope | None = None

    def __enter__(self) -> "_ModelScope":
        self.scope = self._manager.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._manager.__exit__(None, None, None)

    def records(self) -> list[ModelCallRecord]:
        return list(self.scope.records) if self.scope is not None else []


class _Reject(Exception):
    """A rejected invocation: RUN_ERROR with a rejection code; no record is written."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# The Day 1 name; kept for callers.
_InputError = _Reject


class PlanCompiler(Protocol):
    vocab: Vocabulary

    def compile(
        self, plan: ir.Plan, context: CohortContext, ledger: SessionLedger | None = None, version: int = 1
    ) -> CompiledPlan | CompileStop: ...


@dataclass
class PendingEdit:
    """An accepted edit of a live run, applied by its task at the next step boundary (or after an abort)."""

    action: EditPlanAction
    plan: CompiledPlan
    ir: ir.Plan | None
    context: CohortContext


@dataclass
class RunHandle:
    thread_id: str
    run_id: str
    writer: RunWriter | None = None
    task: asyncio.Task[None] | None = None
    # Stop was asked for (contract "cancel"); `abort` asks the in-flight call to end now (Stop, or an edit
    # that changes the running step, or an edit during Answering).
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: set[asyncio.Queue[BaseEvent]] = field(default_factory=set)
    last_snapshot: BaseEvent | None = None
    terminal: BaseEvent | None = None
    open_steps: list[str] = field(default_factory=list)
    edit: PendingEdit | None = None
    # Edit streams waiting for the applied snapshot; they see none of version n's events.
    joiners: list[asyncio.Queue[BaseEvent]] = field(default_factory=list)
    running_step: str | None = None

    @property
    def status(self) -> RunStatus:
        assert self.writer is not None
        return self.writer.record.status

    def subscribe(self, *, current: bool = False, preface: Sequence[BaseEvent] = ()) -> asyncio.Queue[BaseEvent]:
        """A new subscriber queue: `preface` first (reports of other runs), then the current snapshot when joining."""
        queue: asyncio.Queue[BaseEvent] = asyncio.Queue()
        for first in preface:
            queue.put_nowait(first)
        if self.terminal is not None:
            for event in (self.last_snapshot, self.terminal):
                if event is not None:
                    queue.put_nowait(event)
        elif current and self.last_snapshot is not None:
            queue.put_nowait(self.last_snapshot)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[BaseEvent]) -> None:
        self.subscribers.discard(queue)
        if queue in self.joiners:
            self.joiners.remove(queue)

    def attach_joiners(self) -> None:
        self.subscribers.update(self.joiners)
        self.joiners.clear()

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
        code = record.code or "INTERNAL_ERROR"
        return events.run_error(code, FAILURE_MESSAGES.get(code, "The run failed."))
    return events.run_finished(record.thread_id, record.run_id, cast(FinishStatus, record.status), record.code)


def _set_step(record: RunRecord, step_id: str, **update: object) -> None:
    record.steps = [s.model_copy(update=update) if s.id == step_id else s for s in record.steps]


def _merge_cohorts(record: RunRecord, cohorts: Sequence[CohortRef]) -> None:
    for cohort in cohorts:
        if any(c.ref == cohort.ref for c in record.cohorts):
            record.cohorts = [cohort if c.ref == cohort.ref else c for c in record.cohorts]
        else:
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


def _merge_limitations(*groups: Sequence[Limitation]) -> list[Limitation]:
    out: list[Limitation] = []
    for group in groups:
        for item in group:
            if all((o.code, o.text) != (item.code, item.text) for o in out):
                out.append(item)
    return out


def _open_links(plan: CompiledPlan, verified: Sequence[NearestApp]) -> list[NearestApp]:
    """The answer's Open <app> links: the compiler's C-FIT hand-offs (`CompiledPlan.open_apps`, such as
    Cohort Comparison for a survival split ProteinPaint cannot draw) first, then the verified composer's,
    each app once."""
    out: list[NearestApp] = []
    for link in [*plan.open_apps, *verified]:
        if all(o.app_id != link.app_id for o in out):
            out.append(link)
    return out


def _viz_ids(record: RunRecord) -> set[str]:
    answers = [record.answer, *(s.answer for s in record.superseded)]
    return {spec.viz_id for answer in answers for spec in answer.viz_specs}


def _renumber(specs: Sequence[VizSpec], record: RunRecord) -> list[VizSpec]:
    """vizIds continue across plan versions (contract "Viz specs"): an edit's cards take the next numbers."""
    used = [int(v[1:]) for v in (s.viz_id for a in (sa.answer for sa in record.superseded) for s in a.viz_specs)]
    start = max(used, default=0)
    return [spec.model_copy(update={"viz_id": f"v{start + i}"}) for i, spec in enumerate(specs, start=1)]


def _scope_context(record: RunRecord) -> CohortContext:
    """The cohort context the current plan version resolved against: the last scope edit's, else the ask's."""
    for entry in reversed(record.plan_history):
        if isinstance(entry.edit, EditScopeAction):
            return entry.edit.cohort_context
    return record.request.cohort_context


def _current_ir(record: RunRecord) -> ir.Plan | None:
    for entry in reversed(record.plan_history):
        if entry.ir is not None:
            return entry.ir
    return record.planner_output if isinstance(record.planner_output, ir.Plan) else None


def _cancel_mark(r: RunRecord) -> None:
    """End a paused or expired-Stopping run Cancelled: open steps Skipped, a pending nonce voided."""
    r.status, r.code = "Cancelled", None
    r.steps = [
        s.model_copy(update={"status": "Skipped", "output_summary": STOPPED_SUMMARY})
        if s.status in ("Queued", "AwaitingUser", "Running", "Retrying")
        else s
        for s in r.steps
    ]
    _void_pending(r)
    if r.answer.observed and r.answer.withheld_reason is None:
        r.answer.withheld_reason = STOPPED_WITHHELD


def _void_pending(r: RunRecord) -> None:
    if isinstance(r.pending, ConfirmPending):
        r.confirmations.append(
            ConfirmationRecord(nonce=r.pending.nonce, decision=None, saved=[], make_active=None, at=iso_utc(utc_now()), outcome="voided")
        )
    r.pending = None


def _log_action(r: RunRecord, action: ActionName, outcome: Literal["accepted", "repeat", "rejected"], code: Code | None = None) -> None:
    r.actions.append(ActionLogEntry(at=iso_utc(utc_now()), action=action, outcome=outcome, code=code))


class _Session:
    """The controller's side of execution (interfaces.StepSession, executor.EngineSession)."""

    def __init__(self, handle: RunHandle, resume: ResumeState | None, context: RunContext) -> None:
        self._handle = handle
        self._resume = resume
        self._context = context

    @property
    def _writer(self) -> RunWriter:
        assert self._handle.writer is not None
        return self._handle.writer

    @property
    def stop_requested(self) -> bool:
        return self._handle.stop.is_set() or self._handle.edit is not None

    @property
    def resume(self) -> ResumeState | None:
        return self._resume

    @property
    def context(self) -> RunContext:
        return self._context

    async def wait_stop(self) -> None:
        await self._handle.abort.wait()

    async def start_step(self, step_id: str) -> None:
        self._handle.publish(events.step_started(step_id))
        self._handle.open_steps.append(step_id)
        self._handle.running_step = step_id

        def running(r: RunRecord) -> bool:
            step = next((s for s in r.steps if s.id == step_id), None)
            if step is not None and step.status == "Running":
                return False  # a confirm's applied snapshot already shows the write step Running
            _set_step(r, step_id, status="Running")
            return True

        await self._writer.transition(running)

    async def retrying(self, step_id: str, *, attempt: int, backoff_ms: int) -> None:
        log.info("run %s step %s retrying: attempt %d after %d ms", self._handle.run_id, step_id, attempt, backoff_ms)
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
        """The finished row, its facts and cohorts reach the screen with the next transition (the fixtures' shape)."""
        if step_id in self._handle.open_steps:
            self._handle.open_steps.remove(step_id)
        self._handle.running_step = None
        self._handle.publish(events.step_finished(step_id))

        def finish(r: RunRecord) -> None:
            _set_step(r, step_id, status=status, output_summary=output_summary, duration_ms=duration_ms, code=code)
            r.calls.extend(calls)
            r.facts.extend(facts)
            _merge_cohorts(r, cohorts)
            r.answer.limitations = _merge_limitations(r.answer.limitations, limitations)
            step = next((s for s in r.steps if s.id == step_id), None)
            if status in ("Done", "Empty") and step is not None:
                _clear_failure(r, step)
            if step is not None and step.tool == "fixture_fingerprint" and status == "Done":
                used = [c for c in calls if c.status == "ok"]
                observed = used[-1].observed if used else None
                sha = observed.get("fingerprintSha256") if isinstance(observed, dict) else None
                if isinstance(sha, str):
                    r.environment.fingerprint_sha256 = sha

        await self._writer.stage(finish)

    async def skip_step(
        self, step_id: str, code: Code | None, reason: str, *, calls: Sequence[CallRecord] = (), duration_ms: int | None = None
    ) -> None:
        """An aborted running step is its own transition (Stop shows it at once); a queued one joins the next."""
        started = step_id in self._handle.open_steps

        def skip(r: RunRecord) -> None:
            _set_step(r, step_id, status="Skipped", output_summary=reason, code=code, duration_ms=duration_ms)
            r.calls.extend(calls)

        if started:
            self._handle.open_steps.remove(step_id)
            self._handle.running_step = None
            self._handle.publish(events.step_finished(step_id))
            await self._writer.transition(skip)
        else:
            await self._writer.stage(skip)

    async def reuse_step(self, step_id: str, *, origin: StepOrigin) -> None:
        def reuse(r: RunRecord) -> None:
            step = next((s for s in r.steps if s.id == step_id), None)
            if step is None or (step.status == "Reused" and step.reused_from == origin):
                return
            _set_step(r, step_id, status="Reused", reused_from=origin)
            r.facts.extend(_carried_facts(r, step_id, step.row, origin))

        await self._writer.stage(reuse)


def _clear_failure(r: RunRecord, step: StepView) -> None:
    """retry_step: the TOOL_FAILED line goes when the step succeeds (and no other step of that tool still failed)."""
    phrase = TOOL_PHRASES.get(step.tool)
    if phrase is None or any(s.status == "Failed" and s.tool == step.tool and s.id != step.id for s in r.steps):
        return
    r.answer.limitations = [
        item for item in r.answer.limitations if not (item.code == "TOOL_FAILED" and item.text.startswith(f"The {phrase} "))
    ]


class Controller:
    def __init__(
        self,
        settings: Settings,
        store: RecordStore,
        compiler: PlanCompiler,
        planner: Planner,
        executor: Executor,
        *,
        composer: Composer | None = None,
        verifier: Verifier | None = None,
        renderer: ObservedRenderer | None = None,
        ledger_store: SessionLedgerStore | None = None,
        vocab: Vocabulary | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.compiler = compiler
        self.planner = planner
        self.executor = executor
        self.composer = composer
        self.verifier = verifier
        self.vocab = vocab if vocab is not None else getattr(compiler, "vocab", None)
        self.renderer: ObservedRenderer = renderer or TemplateRenderer(self.vocab, de_launcher=settings.de_launcher)
        self.ledger_store = ledger_store
        self.owner = owner_id()
        self._thread_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def busy(self) -> bool:
        return is_busy()

    def _tool_transport(self) -> Literal["in_process", "gateway"]:
        """How tools are reached, as the engine's tool client says (the record's environment.toolTransport)."""
        transport = getattr(getattr(self.executor, "tools", None), "transport", None)
        return transport if transport in ("in_process", "gateway") else self.settings.tool_transport

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

    def _parse(self, run_input: RunAgentInput) -> OrionAction:
        props = run_input.forwarded_props
        orion = props.get("orion") if isinstance(props, dict) else None
        if not isinstance(orion, dict):
            raise _Reject("INVALID_INPUT", "forwardedProps.orion must be an object with an action.")
        try:
            return ORION_ACTION.validate_python(orion)
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors(include_input=False, include_url=False)[:3]
            )
            raise _Reject("INVALID_INPUT", f"Invalid forwardedProps.orion: {details}") from None

    async def _dispatch(self, run_input: RunAgentInput) -> AsyncGenerator[BaseEvent, None]:
        thread_id, run_id = run_input.thread_id, run_input.run_id
        try:
            request = self._parse(run_input)
        except _Reject as error:
            log.info("rejected thread %s run %s: %s %s", thread_id, run_id, error.code, error.message)
            yield events.run_error(error.code, error.message)
            return
        if isinstance(request, WarmupAction):
            stream: AsyncGenerator[BaseEvent, None] = self._warmup(thread_id, run_id, request)
        elif isinstance(request, AskAction):
            stream = self._ask(thread_id, run_id, request)
        elif isinstance(request, VizReportAction):
            stream = self._viz_report(thread_id, run_id, request)
        else:
            stream = self._resume(thread_id, run_id, request)
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
                    # A joining invocation missed this step's start; its snapshot carries the row.
                    continue
                yield event
                if isinstance(event, TERMINAL_EVENTS):
                    return
        finally:
            handle.unsubscribe(queue)

    async def _beating(self, work: Awaitable[object]) -> AsyncGenerator[BaseEvent, None]:
        """Heartbeats while `work` runs (a warmup)."""
        task = asyncio.ensure_future(work)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self.settings.heartbeat_s)
                if done:
                    return
                yield events.heartbeat(utc_now())
        finally:
            if not task.done():
                task.cancel()

    # -- warmup -----------------------------------------------------------------

    async def _warmup(self, thread_id: str, run_id: str, action: WarmupAction) -> AsyncGenerator[BaseEvent, None]:
        """Starts the session and warms each model role's cached prefix at its own effort (C-41); creates no run."""
        await self._ride_along(thread_id, action.viz_reports, None)

        async def warm_roles() -> None:
            try:
                results = await asyncio.wait_for(warm_up_roles(self.planner, self.composer), self.settings.warmup_timeout_s)
                log.info("warmup: %s", [(r.role, r.outcome) for r in results])
            except Exception:
                log.warning("warmup failed", exc_info=True)

        async with aclosing(self._beating(warm_roles())) as beats:
            async for beat in beats:
                yield beat
        yield events.run_finished(thread_id, run_id, "Warmed", None)

    # -- ask --------------------------------------------------------------------

    async def _ask(self, thread_id: str, run_id: str, ask: AskAction) -> AsyncGenerator[BaseEvent, None]:
        started: tuple[RunHandle, asyncio.Queue[BaseEvent]] | None = None
        rejection: BaseEvent | None = None
        async with self._thread_locks[thread_id]:
            index = await self.store.load_thread(thread_id)
            reports: list[BaseEvent] = []
            try:
                if run_id in index.run_ids or run_id in RUNS or await self.store.load_run(run_id) is not None:
                    raise _Reject("INVALID_INPUT", "This runId was already used; send a new one.")
                await self._ask_admission(index, ask, reports)
            except _Reject as error:
                log.info("ask rejected: thread %s run %s %s", thread_id, run_id, error.code)
                rejection = events.run_error(error.code, error.message)
            else:
                await self._ride_along(thread_id, ask.viz_reports, None)
                started = self._launch(thread_id, run_id, ask, reports)
        if started is None:
            assert rejection is not None
            yield rejection
            return
        async with aclosing(self._drain(*started)) as stream:
            async for event in stream:
                yield event

    async def _ask_admission(self, index: ThreadIndex, ask: AskAction, reports: list[BaseEvent]) -> None:
        """Checks 5 and 6 for an ask: a live run is RUN_BUSY; a paused one is cancelled only by cancelWaiting.

        An active run whose lease expired ends Interrupted (or Cancelled) first, and its snapshot goes
        into `reports`, so this turn shows it right after RUN_STARTED (A2 "Disconnects").
        """
        if any(h.thread_id == index.thread_id and h.writer is not None and h.status not in END_STATUSES for h in RUNS.values()):
            raise _Reject("RUN_BUSY", "Another run is active in this thread; cancel it or wait for it to end.")
        active = index.active_run_id
        record = await self.store.load_run(active) if active is not None else None
        if record is not None and record.status in WAITING_STATUSES:
            if ask.cancel_waiting != record.run_id:
                raise _Reject("RUN_BUSY", "A run in this thread is waiting for you; cancel it or answer it first.")
            writer = RunWriter(self.store, record, self.owner, self.settings.lease_ttl_s)

            def cancel(r: RunRecord) -> None:
                _cancel_mark(r)
                _log_action(r, "ask", "accepted")

            reports.append(events.state_snapshot(await writer.transition(cancel)))
            log.info("run %s cancelled by a new question (cancelWaiting)", record.run_id)
        elif record is not None and record.status not in END_STATUSES:
            if not is_expired(record.lease, utc_now()):
                raise _Reject("RUN_BUSY", "Another run is active in this thread; cancel it or wait for it to end.")
            reports.append(events.state_snapshot(await self._expire(record)))
            if ask.cancel_waiting is not None:
                raise _Reject("STALE_ACTION", "The run named in cancelWaiting is not waiting.")
        elif ask.cancel_waiting is not None:
            raise _Reject("STALE_ACTION", "The run named in cancelWaiting is not this thread's waiting run.")
        if active is not None:
            index.active_run_id = None
            await self.store.save_thread(index)

    async def _expire(self, record: RunRecord) -> RunView:
        """Record a run whose lease lapsed: a Stopping run ends Cancelled, any other ends Interrupted.

        updatedAt is the lease's expiry (or the record's updatedAt without a lease), so the record
        equals what the BFF view route showed for it (contract "Expired leases").
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
                if s.status in ("Queued", "AwaitingUser")
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
            _void_pending(r)

        log.info("run %s interrupted: lease expired", record.run_id)
        return await writer.transition(mark, at=at)

    def _new_handle(self, record: RunRecord) -> RunHandle:
        handle = RunHandle(thread_id=record.thread_id, run_id=record.run_id)

        async def publish(view: RunView) -> None:
            handle.last_snapshot = events.state_snapshot(view)
            handle.publish(handle.last_snapshot)

        handle.writer = RunWriter(self.store, record, self.owner, self.settings.lease_ttl_s, publish)
        return handle

    def _start(self, handle: RunHandle, body: Callable[[], Awaitable[None]]) -> None:
        RUNS[handle.run_id] = handle
        _set_busy(handle.run_id, True)
        handle.task = asyncio.create_task(self._task(handle, body), name=f"orion-run-{handle.run_id}")

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
                composer=self.settings.composer,
                executor=self.settings.executor,
                agent_version=AGENT_VERSION,
                tool_transport=self._tool_transport(),
                faults=fault_list(self.settings.faults),
            ),
            request=ask,
            actions=[ActionLogEntry(at=now, action="ask", outcome="accepted", code=None)],
        )
        handle = self._new_handle(record)
        queue = handle.subscribe(preface=preface)
        self._start(handle, partial(self._ask_body, handle))
        log.info("run %s started in thread %s", run_id, thread_id)
        return handle, queue

    # -- the detached run ---------------------------------------------------------

    async def _task(self, handle: RunHandle, body: Callable[[], Awaitable[None]]) -> None:
        assert handle.writer is not None
        lease = asyncio.create_task(keep_renewed(handle.writer, self.settings.lease_renew_s))
        try:
            await body()
            while handle.edit is not None:
                # An edit accepted just as the run paused or ended: apply it now, or answer its stream.
                if handle.status in ACTION_STATUSES["edit_plan"] and not handle.stop.is_set():
                    applied = await self._apply_live_edit(handle)
                    if applied is None:
                        break  # a cancel of the paused or ended run came first; it ends the run
                    await self._continue(handle, *applied)
                else:
                    self._release_joiners(handle)
        except Exception:
            log.exception("run %s failed", handle.run_id)
            await self._fail(handle)
        finally:
            lease.cancel()
            self._release_joiners(handle)
            RUNS.pop(handle.run_id, None)
            _set_busy(handle.run_id, False)

    def _release_joiners(self, handle: RunHandle) -> None:
        """Edit streams of a run that can no longer take the edit get its current snapshot and terminal."""
        handle.edit = None
        if not handle.joiners:
            return
        joiners, handle.joiners = list(handle.joiners), []
        assert handle.writer is not None
        record = handle.writer.record
        for queue in joiners:
            queue.put_nowait(events.state_snapshot(record.view()))
            queue.put_nowait(handle.terminal if handle.terminal is not None else terminal_for(record))

    async def _open(self, handle: RunHandle) -> None:
        """Save the Planning record, then point the thread index at it."""
        assert handle.writer is not None
        await handle.writer.transition(lambda r: None)
        async with self._thread_locks[handle.thread_id]:
            index = await self.store.load_thread(handle.thread_id)
            index.run_ids.append(handle.run_id)
            index.active_run_id = handle.run_id
            await self.store.save_thread(index)

    async def _ask_body(self, handle: RunHandle) -> None:
        await self._open(handle)
        await self._plan(handle, None)

    async def _ledger(self, handle: RunHandle) -> SessionLedger | None:
        if self.ledger_store is None:
            return None
        try:
            return await self.ledger_store.ledger(handle.thread_id, before_run_id=handle.run_id)
        except Exception:
            log.exception("run %s: the session ledger could not be read", handle.run_id)
            return None

    def _renumber_model_calls(self, record: RunRecord, calls: Sequence[ModelCallRecord], role: Literal["planner", "composer"], version: int | None) -> list[ModelCallRecord]:
        """Model callIds are the run's (`<runId>.planner.c<n>`, `<runId>.v<n>.composer.c<k>`), numbered here."""
        prefix = model_call_id(record.run_id, role, 1, version).rsplit(".c", 1)[0]
        taken = sum(1 for c in record.model_calls if c.call_id.startswith(prefix + ".c"))
        return [
            c.model_copy(update={"call_id": model_call_id(record.run_id, role, taken + i, version)})
            for i, c in enumerate(calls, start=1)
        ]

    def _cancelled_call(
        self, record: RunRecord, role: Literal["planner", "composer"], version: int | None, started_at: str, started: float,
        recorded: Sequence[ModelCallRecord] = (),
    ) -> ModelCallRecord:
        """The aborted call's `cancelled` entry when the role recorded none (a role outside the model call scope).

        Its callId follows the record's calls and the scope's (`recorded`, not yet in the record), so every
        callId stays unique; its model and effort are the role's last recorded call's, when there is one."""
        loop = asyncio.get_running_loop()
        prefix = model_call_id(record.run_id, role, 1, version).rsplit(".c", 1)[0]
        taken = sum(1 for c in [*record.model_calls, *recorded] if c.call_id.startswith(prefix + ".c"))
        last = next((c for c in reversed(recorded) if c.role == role), None)
        return ModelCallRecord(
            call_id=model_call_id(record.run_id, role, taken + 1, version), role=role,
            model=last.model if last else PLANNER_MODEL, fallback=last.fallback if last else False,
            effort=last.effort if last else ("low" if role == "planner" else "medium"),
            # After a failed attempt the abort landed in the retry's backoff: the next attempt of that call.
            attempt=min(last.attempt + 1, 4) if last and last.outcome == "error" else 1, started_at=started_at,
            elapsed_ms=max(int((loop.time() - started) * 1000), 0), outcome="cancelled", stop_reason=None,
            input_tokens=None, output_tokens=None, cache_read_tokens=None, cache_write_tokens=None,
        )

    async def _abortable(self, handle: RunHandle, work: Awaitable[object]) -> tuple[bool, object]:
        """Run a model call until it returns or the run's abort fires (then cancel it). Returns (aborted, result)."""
        task = asyncio.ensure_future(work)
        abort = asyncio.ensure_future(handle.abort.wait())
        try:
            done, _ = await asyncio.wait({task, abort}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            abort.cancel()
        if task in done:
            return False, task.result()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return True, None

    async def _plan(self, handle: RunHandle, clarification: Clarification | None) -> None:
        assert handle.writer is not None
        writer = handle.writer
        record = writer.record
        ask = record.request
        ledger = await self._ledger(handle)
        loop = asyncio.get_running_loop()
        started, started_at = loop.time(), iso_utc(utc_now())
        failure: BaseException | None = None
        with _ModelScope(record) as scope:
            try:
                aborted, value = await self._abortable(
                    handle, self.planner.plan(ask.question, ask.cohort_context, ledger, clarification=clarification)
                )
            except Exception as error:  # a model role error (Bedrock, schema); refusals come back as results
                aborted, value, failure = False, None, error
        recorded = scope.records()
        if aborted:
            calls = recorded if any(c.outcome == "cancelled" for c in recorded) else [
                *recorded, self._cancelled_call(record, "planner", None, started_at, started, recorded)
            ]
            await writer.stage(partial(_keep_model_calls, calls=calls))
            return await self._end_stopped(handle)
        if failure is not None:
            log.warning("run %s: the planner failed: %s", handle.run_id, failure)
            calls = recorded or self._renumber_model_calls(record, getattr(failure, "model_calls", []) or [], "planner", None)
            await writer.stage(partial(_keep_model_calls, calls=calls))
            if handle.stop.is_set():
                return await self._end_stopped(handle)
            reason = str(getattr(failure, "reason", "") or "the planner call failed")
            note = Limitation(code="INTERNAL_ERROR", text=f"ORION could not plan this question: {reason}.", unblock="Ask again.")
            return await self._finish(handle, "Failed", _code(getattr(failure, "code", None)) or "INTERNAL_ERROR", [note])
        result = cast(PlannerResult, value)
        output = result.output
        calls = recorded or self._renumber_model_calls(record, result.model_calls, "planner", None)

        def keep_plan_call(r: RunRecord) -> None:
            r.planner_output = output
            r.model_calls.extend(calls)
            r.ledger = self._ledger_use(output, ledger)

        await writer.stage(keep_plan_call)
        if handle.stop.is_set():
            return await self._end_stopped(handle)
        if output is None:
            # The planner's own reason when it gave one: which models declined, or that the fallback could not be reached.
            why = (result.decline_reason or "").strip()
            text = f"ORION could not plan this question. {why.rstrip('.')}." if why else "ORION could not plan this question: both models declined."
            note = Limitation(code="PLANNER_DECLINED", text=text, unblock="Rephrase the question.")
            return await self._finish(handle, "Failed", "PLANNER_DECLINED", [note])
        if isinstance(output, ir.Unsupported):
            stop = self._compiler_call("unsupported", output)
            lines = stop.limitations if isinstance(stop, CompileStop) else [self._unsupported_limitation(output)]
            return await self._finish(handle, "Unsupported", output.reason, lines)
        if isinstance(output, ir.Clarify):
            if clarification is not None:
                note = Limitation(
                    code="AMBIGUOUS_REQUEST",
                    text="ORION still could not tell what you meant after one clarifying question, so nothing was run.",
                    unblock="Ask again and name the field, gene or cohort to use.",
                )
                return await self._finish(handle, "Limited", "AMBIGUOUS_REQUEST", [note])
            given = self._compiler_call("clarify", output, ask.question, ask.cohort_context)
            if isinstance(given, CompileStop):
                return await self._finish(handle, "Limited", given.code, given.limitations)
            pending = given if isinstance(given, ClarifyPending) else self._clarify_pending(output)

            def clarify(r: RunRecord) -> None:
                r.status, r.code, r.pending = "Clarifying", None, pending

            if not await self._guarded(handle, clarify):
                return await self._end_stopped(handle)
            return self._pause_terminal(handle)
        compiled = self.compiler.compile(output, ask.cohort_context, ledger)
        if isinstance(compiled, CompileStop):
            status: RunStatus = "Unsupported" if getattr(compiled, "status", "Limited") == "Unsupported" else "Limited"
            return await self._finish(handle, status, compiled.code, compiled.limitations)
        cause: Literal["ask", "clarify_answer"] = "ask" if clarification is None else "clarify_answer"
        consent = self._consent(compiled, output)
        cohorts = plan_cohorts(compiled, ask.cohort_context, self.vocab)
        used = getattr(compiled, "ledger", None)

        def show_plan(r: RunRecord) -> None:
            r.plan_versions.append(compiled)
            r.plan_history.append(PlanHistoryEntry(version=compiled.version, cause=cause, at=iso_utc(utc_now()), ir=output))
            r.steps = [step.queued() for step in compiled.steps]
            r.cohorts = list(cohorts.cohorts)
            r.answer.limitations = list(compiled.limitations)
            if isinstance(used, LedgerUse):
                r.ledger = used
            if consent is not None:
                r.status, r.code, r.pending = "AwaitingConsent", "NAMED_TOOL_MISMATCH", consent
                if not any(item.code == "NAMED_TOOL_MISMATCH" for item in r.answer.limitations):
                    r.answer.limitations.append(
                        Limitation(code="NAMED_TOOL_MISMATCH", text=consent.reason, unblock=f"Use {consent.proposed.title}, or cancel.",
                                   nearest_apps=[consent.proposed])
                    )
            else:
                r.status = "Executing"

        if not await self._guarded(handle, show_plan):
            return await self._end_stopped(handle)
        if consent is not None:
            return self._pause_terminal(handle)
        await self._continue(handle, compiled, None)

    async def _guarded(self, handle: RunHandle, mutate: Callable[[RunRecord], None]) -> bool:
        """A status change by the run's own task (a pause, Answering, an end, an applied edit) that a cancel has not
        overtaken. Once a cancel began (`stop` is set before its transition), the task changes nothing: the cancel's
        Stopping, or a waiting run's Cancelled, stands. Returns False then, and the caller ends the run stopped."""
        assert handle.writer is not None
        applied = False

        def change(r: RunRecord) -> bool:
            nonlocal applied
            if handle.stop.is_set():
                return False
            mutate(r)
            applied = True
            return True

        await handle.writer.transition(change)
        return applied

    def _compiler_call(self, name: str, *args: object) -> object:
        """A compiler method other than compile (clarify, unsupported, edit), when this compiler has it."""
        method = getattr(self.compiler, name, None)
        if not callable(method):
            return None
        return method(*args)

    def _ledger_use(self, output: ir.PlannerOutput | None, ledger: SessionLedger | None) -> LedgerUse:
        if not isinstance(output, ir.Plan):
            return LedgerUse()
        wanted: list[str] = []
        if output.base_cohort is not None and output.base_cohort.source == "thread_cohort" and output.base_cohort.ledger_ref:
            wanted.append(output.base_cohort.ledger_ref)
        wanted += [c.definition.ledger_ref for c in output.cohorts if isinstance(c.definition, ir.ThreadDef)]
        refs: list[LedgerRefUse] = []
        for run in reversed(ledger.runs if ledger is not None else []):
            if output.follows is not None and run.run_id != output.follows:
                continue
            for cohort in run.cohorts:
                if cohort.ref in wanted and all(u.ref != cohort.ref for u in refs):
                    refs.append(LedgerRefUse(run_id=run.run_id, ref=cohort.ref, name=cohort.name))
        return LedgerUse(follows=output.follows, refs=refs)

    def _unsupported_limitation(self, output: ir.Unsupported) -> Limitation:
        texts = {
            "UNSUPPORTED_ANALYSIS": "ORION cannot run this analysis. It compares survival, ranks mutated genes, runs differential expression and counts cohorts.",
            "CLINICAL_ADVICE": "ORION gives no clinical or treatment advice.",
            "OUT_OF_SCOPE": "This question is outside what ORION covers in the Virtual Lab.",
        }
        # The Unsupported card names the first nearest app and offers Open on this cohort for it.
        nearest = [NearestApp.of(app_id) for app_id in output.nearest_app_ids]
        return Limitation(code=output.reason, text=texts[output.reason], unblock=None, nearest_apps=nearest)

    def _clarify_pending(self, output: ir.Clarify) -> ClarifyPending:
        """The Clarification card from code templates (C-37): one question, each option a field with a value preview."""
        options: list[ClarifyOption] = []
        for option_id in output.option_ids:
            spec = self.vocab.field(option_id) if self.vocab is not None else None
            if spec is not None:
                values = [str(v) for v in (spec.values or ())]
                preview = ", ".join(values[:4]) + (", …" if len(values) > 4 else "")
                options.append(ClarifyOption(option_id=option_id, field=spec.id, label=spec.label, preview=preview))
            else:
                label = SCOPE_LABELS.get(option_id, option_id)
                options.append(ClarifyOption(option_id=option_id, field=None, label=label, preview=""))
        return ClarifyPending(kind="clarify", slot=output.slot, question=CLARIFY_QUESTIONS[output.slot], options=options)

    def _consent(self, compiled: CompiledPlan, output: ir.Plan) -> ConsentPending | None:
        """The substitution the compiler found (C-FIT): the plan's own `consent`, else read off a NAMED_TOOL_MISMATCH line."""
        given = getattr(compiled, "consent", None)
        if isinstance(given, ConsentPending):
            return given
        mismatch = next((item for item in compiled.limitations if item.code == "NAMED_TOOL_MISMATCH"), None)
        if mismatch is None:
            return None
        named_ids = [NAMED_TOOL_APPS[t.tool] for t in output.named_tools if NAMED_TOOL_APPS.get(t.tool) is not None]
        kinds = [a.kind for a in output.analyses]
        proposed = mismatch.nearest_apps[0] if mismatch.nearest_apps else NearestApp.of(ANALYSIS_APPS[kinds[0]])
        named = next((NearestApp.of(a) for a in named_ids if a is not None and a != proposed.app_id), None)
        if named is None:
            return None
        return ConsentPending(kind="consent", named=named, proposed=proposed, reason=mismatch.text)

    # -- execution ------------------------------------------------------------------

    def _run_context(self, record: RunRecord, plan: CompiledPlan, ledger: SessionLedger | None = None) -> RunContext:
        context = _scope_context(record)
        planned = plan_cohorts(plan, context, self.vocab)
        return RunContext(
            run_id=record.run_id, cohort_context=context, cohorts=list(record.cohorts), cohort_steps=planned.steps,
            cohort_ids=planned.ids, cohort_definitions=planned.definitions, ledger=ledger,
        )

    def _resume_state(self, record: RunRecord, **extra: object) -> ResumeState:
        return ResumeState(
            steps=list(record.steps), facts=list(record.facts), calls=list(record.calls), cohorts=list(record.cohorts),
            **extra,  # type: ignore[arg-type]
        )

    async def _run_executor(self, handle: RunHandle, plan: CompiledPlan, resume: ResumeState | None) -> ExecutionResult:
        """Run the executor; after an abort it has STOP_GRACE_S to return before the controller cancels it."""
        assert handle.writer is not None
        ledger = await self._ledger(handle) if plan.write_step() is not None else None
        session = _Session(handle, resume, self._run_context(handle.writer.record, plan, ledger))
        task = asyncio.ensure_future(self.executor.execute(plan, session))
        abort = asyncio.ensure_future(handle.abort.wait())
        try:
            done, _ = await asyncio.wait({task, abort}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            abort.cancel()
        if task in done:
            return task.result()
        grace = min(self.settings.stop_grace_s, STOP_GRACE_S)
        try:
            return await asyncio.wait_for(asyncio.shield(task), grace)
        except TimeoutError:
            log.warning("run %s: the executor did not stop within %.1f s; cancelling it", handle.run_id, grace)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            for step_id in list(handle.open_steps):
                await session.skip_step(step_id, None, STOPPED_SUMMARY)
            return ExecutionResult(status="Cancelled", code=None, limitations=[])

    async def _continue(self, handle: RunHandle, plan: CompiledPlan, resume: ResumeState | None) -> None:
        while True:
            result = await self._run_executor(handle, plan, resume)
            if handle.edit is not None and not handle.stop.is_set():
                applied = await self._apply_live_edit(handle)
                if applied is None:
                    return await self._end_stopped(handle)
                plan, resume = applied
                continue
            if handle.stop.is_set() or result.status == "Cancelled":
                return await self._end_stopped(handle)
            if result.status == "AwaitingConfirmation" and result.pending is not None:
                return await self._pause_confirm(handle, result.pending)
            if result.status in READY:
                outcome = await self._answer(handle, plan)
                if outcome == "edit":
                    applied = await self._apply_live_edit(handle)
                    if applied is None:
                        return await self._end_stopped(handle)
                    plan, resume = applied
                    continue
                return
            return await self._end_execution(handle, plan, result)

    def _render(self, record: RunRecord, plan: CompiledPlan) -> ObservedResult:
        return self.renderer.render(plan, record.steps, record.facts, record.calls, record.cohorts)

    async def _pause_confirm(self, handle: RunHandle, pending: ConfirmPending) -> None:
        assert handle.writer is not None

        def pause(r: RunRecord) -> None:
            r.status, r.code, r.pending = "AwaitingConfirmation", None, pending
            _set_step(r, pending.step_id, status="AwaitingUser")

        if not await self._guarded(handle, pause):
            return await self._end_stopped(handle)
        for frame in events.save_cohorts_call(SaveCohortsArgs.model_validate(pending.model_dump(mode="json"))):
            handle.publish(frame)
        self._pause_terminal(handle)

    def _pause_terminal(self, handle: RunHandle) -> None:
        assert handle.writer is not None
        handle.publish(terminal_for(handle.writer.record))
        log.info("run %s waits: %s", handle.run_id, handle.writer.record.status)

    async def _end_execution(self, handle: RunHandle, plan: CompiledPlan, result: ExecutionResult) -> None:
        """Partial, Limited or Failed: Observed and Limitations only, the composer is not called."""
        assert handle.writer is not None
        record = handle.writer.record
        rendered = self._render(record, plan)
        status = result.status if result.status in END_STATUSES else "Limited"
        withheld = {"Partial": FAILED_STEP_WITHHELD, "Limited": LIMITED_WITHHELD, "Failed": RUN_FAILED_WITHHELD}.get(status, RUN_FAILED_WITHHELD)
        leftover = FAILED_SUMMARY if status == "Failed" else ENDED_EARLY_SUMMARY

        def end(r: RunRecord) -> None:
            r.steps = [
                s.model_copy(update={"status": "Skipped", "output_summary": leftover})
                if s.status in ("Queued", "AwaitingUser", "Running", "Retrying") else s
                for s in r.steps
            ]
            r.answer.observed = list(rendered.observed)
            r.answer.limitations = _merge_limitations(plan.limitations, rendered.limitations, result.limitations)
            r.answer.viz_specs = _renumber(rendered.viz_specs, r)
            r.answer.withheld_reason = withheld if r.answer.observed else None

        await self._finish(handle, status, result.code, mutate=end)

    async def _end_stopped(self, handle: RunHandle) -> None:
        """Cancelled after a Stop: open and queued steps Skipped "Not run: stopped", Observed for the finished steps."""
        assert handle.writer is not None
        record = handle.writer.record
        plan = record.plan_versions[-1] if record.plan_versions else None
        rendered = self._render(record, plan) if plan is not None else None

        def end(r: RunRecord) -> None:
            r.steps = [
                s.model_copy(update={"status": "Skipped", "output_summary": STOPPED_SUMMARY})
                if s.status in ("Queued", "AwaitingUser", "Running", "Retrying") else s
                for s in r.steps
            ]
            if rendered is not None and plan is not None:
                r.answer.observed = list(rendered.observed)
                r.answer.limitations = _merge_limitations(plan.limitations, rendered.limitations)
                r.answer.viz_specs = _renumber(rendered.viz_specs, r)
            r.answer.interpretation = r.answer.hypotheses = None
            r.answer.follow_ups, r.answer.open_links = [], []
            r.answer.withheld_reason = STOPPED_WITHHELD if r.answer.observed else None
            _void_pending(r)

        handle.open_steps.clear()
        await self._finish(handle, "Cancelled", None, mutate=end)

    # -- answering --------------------------------------------------------------------

    async def _answer(self, handle: RunHandle, plan: CompiledPlan) -> Literal["done", "edit"]:
        """Observed first (Answering), then the composer and the verifier; one re-compose; ObservedOnly when nothing survives."""
        assert handle.writer is not None
        writer = handle.writer
        record = writer.record
        rendered = self._render(record, plan)

        def answering(r: RunRecord) -> None:
            r.status, r.code = "Answering", None
            r.answer.observed = list(rendered.observed)
            r.answer.limitations = _merge_limitations(plan.limitations, rendered.limitations)
            r.answer.viz_specs = _renumber(rendered.viz_specs, r)
            r.answer.interpretation = r.answer.hypotheses = None
            r.answer.follow_ups, r.answer.open_links, r.answer.withheld_reason = [], [], None

        if not await self._guarded(handle, answering):
            await self._end_stopped(handle)
            return "done"
        if self.composer is None or self.verifier is None or self.vocab is None:
            await self._end_answer(handle, NOT_CONFIGURED_WITHHELD)
            return "done"
        return await self._answer_with(handle, plan, rendered)

    async def _answer_with(self, handle: RunHandle, plan: CompiledPlan, rendered: ObservedResult) -> Literal["done", "edit"]:
        """Compose, verify and re-compose once (orion_agent.model.answer.write_answer) inside the run's model call
        scope; aborted at once by a Stop or an edit (the cancelled call is recorded)."""
        assert handle.writer is not None and self.composer is not None and self.verifier is not None and self.vocab is not None
        writer = handle.writer
        record = writer.record
        ledger = await self._ledger(handle)
        loop = asyncio.get_running_loop()
        started, started_at = loop.time(), iso_utc(utc_now())
        with _ModelScope(record) as scope:
            aborted, value = await self._abortable(
                handle,
                write_answer(
                    self.composer, self.verifier, question=record.question, facts=list(record.facts), plan=plan, observed=rendered,
                    ledger=ledger, vocab=self.vocab,
                ),
            )
        recorded = scope.records()
        if aborted:
            calls = recorded if any(c.outcome == "cancelled" for c in recorded) else [
                *recorded, self._cancelled_call(record, "composer", plan.version, started_at, started, recorded)
            ]
            await writer.stage(partial(_keep_model_calls, calls=calls))
            if handle.edit is not None and not handle.stop.is_set():
                return "edit"
            await self._end_stopped(handle)
            return "done"
        outcome = cast(AnswerOutcome, value)
        calls = recorded or self._renumber_model_calls(record, outcome.model_calls, "composer", plan.version)
        await writer.stage(partial(_keep_model_calls, calls=calls))
        if handle.edit is not None and not handle.stop.is_set():
            return "edit"
        if handle.stop.is_set():
            await self._end_stopped(handle)
            return "done"
        complete = outcome.status == "Complete" and any(s.dropped is None for s in outcome.interpretation or [])
        withheld = outcome.withheld_reason or CHECKS_WITHHELD

        def end(r: RunRecord) -> None:
            r.answer.observed = [*r.answer.observed, *outcome.inserted_observed]
            r.answer.limitations = _merge_limitations(r.answer.limitations, outcome.inserted_limitations)
            if complete:
                r.answer.interpretation = list(outcome.interpretation or [])
                r.answer.hypotheses = list(outcome.hypotheses or [])
                r.answer.follow_ups = list(outcome.follow_ups)
                r.answer.open_links = _open_links(plan, outcome.open_links)
                r.answer.withheld_reason = None
            else:
                r.answer.interpretation = r.answer.hypotheses = None
                r.answer.follow_ups, r.answer.open_links = [], []
                r.answer.withheld_reason = withheld if r.answer.observed else None

        run_code = None if complete else outcome.code
        await self._finish(handle, "Complete" if complete else "ObservedOnly", run_code, mutate=end, message=True)
        return "done"

    async def _end_answer(self, handle: RunHandle, withheld: str) -> None:
        """ObservedOnly without model text: no composer, verifier or vocabulary is configured."""
        assert handle.writer is not None

        def end(r: RunRecord) -> None:
            r.answer.interpretation = r.answer.hypotheses = None
            r.answer.follow_ups, r.answer.open_links = [], []
            r.answer.withheld_reason = withheld if r.answer.observed else None

        await self._finish(handle, "ObservedOnly", None, mutate=end, message=True)

    # -- ending -------------------------------------------------------------------------

    async def _finish(
        self,
        handle: RunHandle,
        status: RunStatus,
        code: Code | None,
        limitations: Sequence[Limitation] | None = None,
        *,
        mutate: Callable[[RunRecord], None] | None = None,
        message: bool = False,
    ) -> None:
        assert handle.writer is not None

        def end(r: RunRecord) -> None:
            if mutate is not None:
                mutate(r)
            r.status, r.code = status, code
            if status not in WAITING_STATUSES:
                r.pending = None
            r.answer.limitations = _merge_limitations(r.answer.limitations, limitations or ())

        if handle.edit is not None and status in END_STATUSES and status not in ACTION_STATUSES["edit_plan"]:
            handle.attach_joiners()  # a Stop or a failure won over a waiting edit: its stream sees this end
            handle.edit = None
        if status in ("Cancelled", "Failed"):
            await handle.writer.transition(end)
        elif not await self._guarded(handle, end):
            # A cancel that began meanwhile wins: the run ends Cancelled, not with the status it reached.
            return await self._end_stopped(handle)
        record = handle.writer.record
        if message and status in ("Complete", "ObservedOnly"):
            version = record.plan_versions[-1].version if record.plan_versions else 1
            markdown = answer_markdown(
                record.answer.observed, record.answer.interpretation, record.answer.hypotheses, record.answer.withheld_reason,
                record.answer.limitations, record.answer.notice,
            )
            for frame in events.text_message(f"{record.run_id}.v{version}", markdown):
                handle.publish(frame)
        if status in END_STATUSES:
            await self._clear_active(handle.thread_id, handle.run_id)
        handle.publish(terminal_for(record))
        log.info("run %s ended %s %s", handle.run_id, status, code or "")

    async def _fail(self, handle: RunHandle) -> None:
        """Close every open step, skip the queued ones, end the run Failed, and still send exactly one RUN_ERROR."""
        try:
            assert handle.writer is not None
            for step_id in list(handle.open_steps):
                handle.publish(events.step_finished(step_id))
                await handle.writer.transition(partial(_set_step, step_id=step_id, status="Failed", code="INTERNAL_ERROR"))
            handle.open_steps.clear()

            def skip(r: RunRecord) -> None:
                r.steps = [
                    s.model_copy(update={"status": "Skipped", "output_summary": FAILED_SUMMARY})
                    if s.status in ("Queued", "AwaitingUser") else s
                    for s in r.steps
                ]
                r.pending = None

            await self._finish(handle, "Failed", "INTERNAL_ERROR", mutate=skip)
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

    # -- viz reports ---------------------------------------------------------------------------

    async def _ride_along(self, thread_id: str, reports: Sequence[VizReport], target: RunRecord | None) -> list[VizReport]:
        """Record each ride-along report on its run, following "Who writes a record". Returns the target run's reports,
        which the action's own transition records. Unknown runs, other threads and unknown vizIds are dropped and logged."""
        mine: list[VizReport] = []
        for report in reports:
            if target is not None and report.run_id == target.run_id:
                if report.viz_id in _viz_ids(target):
                    mine.append(report)
                else:
                    log.info("viz report dropped: run %s has no %s", report.run_id, report.viz_id)
                continue
            handle = RUNS.get(report.run_id)
            record = handle.writer.record if handle is not None and handle.writer is not None else await self.store.load_run(report.run_id)
            if record is None or record.thread_id != thread_id or report.viz_id not in _viz_ids(record):
                log.info("viz report dropped: thread %s run %s viz %s", thread_id, report.run_id, report.viz_id)
                continue
            await self._write_report(record, handle, report)
        return mine

    async def _write_report(self, record: RunRecord, handle: RunHandle | None, report: VizReport) -> None:
        if handle is not None and handle.writer is not None:
            await handle.writer.stage(partial(_keep_report, report=report))
        elif record.status in LEASED_STATUSES and not is_expired(record.lease, utc_now()):
            log.info("viz report logged, not written (run %s leased elsewhere): %s %s", record.run_id, report.viz_id, report.mounted)
        else:
            writer = RunWriter(self.store, record, self.owner, self.settings.lease_ttl_s)
            await writer.save_quietly(partial(_keep_report, report=report))

    async def _viz_report(self, thread_id: str, run_id: str, action: VizReportAction) -> AsyncGenerator[BaseEvent, None]:
        """A standalone report: RUN_STARTED, then RUN_FINISHED Recorded; no snapshot, no revision change."""
        replies: list[BaseEvent] = []
        async with self._thread_locks[thread_id]:
            try:
                handle, record = await self._target(thread_id, run_id)
                report = VizReport(
                    run_id=run_id, viz_id=action.viz_id, mounted=action.mounted, error=action.error,
                    pinned_hash_verified=action.pinned_hash_verified,
                )
                if handle is None and record.status in LEASED_STATUSES and is_expired(record.lease, utc_now()):
                    # The expiry is this turn's outcome; the reports it carried are recorded with it (the target's in
                    # the expiry's transition, a report naming a viz the run lacks dropped and logged).
                    await self._take_reports(record, None, [*action.viz_reports, report])
                    view = await self._expire(record)
                    await self._clear_index(thread_id, run_id)
                    replies = [events.state_snapshot(view), terminal_for(record)]
                else:
                    if action.viz_id not in _viz_ids(record):
                        raise _Reject("INVALID_INPUT", f"This run has no viz {action.viz_id}.")
                    await self._ride_along(thread_id, action.viz_reports, None)
                    await self._write_report(record, handle, report)
                    replies = [events.run_finished(thread_id, run_id, "Recorded", None)]
            except _Reject as error:
                log.info("viz_report rejected: thread %s run %s %s", thread_id, run_id, error.code)
                replies = [events.run_error(error.code, error.message)]
        for event in replies:
            yield event

    # -- resumes ---------------------------------------------------------------------------------

    async def _target(self, thread_id: str, run_id: str) -> tuple[RunHandle | None, RunRecord]:
        """Check 3: the run exists in this thread. Returns its live handle (if its task is here) and record."""
        handle = RUNS.get(run_id)
        record = handle.writer.record if handle is not None and handle.writer is not None else await self.store.load_run(run_id)
        if record is None or record.thread_id != thread_id:
            raise _Reject("RUN_NOT_FOUND", "No run with this runId exists in this thread.")
        return handle, record

    async def _clear_index(self, thread_id: str, run_id: str) -> None:
        index = await self.store.load_thread(thread_id)
        if index.active_run_id == run_id:
            index.active_run_id = None
            await self.store.save_thread(index)

    async def _other_busy(self, index: ThreadIndex, run_id: str, preface: list[BaseEvent]) -> None:
        """Check 5: another run of the thread is unfinished (an expired one is ended and reported instead)."""
        for handle in RUNS.values():
            if handle.thread_id == index.thread_id and handle.run_id != run_id and handle.writer is not None and handle.status not in END_STATUSES:
                raise _Reject("RUN_BUSY", "Another run is active in this thread; wait for it or cancel it.")
        active = index.active_run_id
        if active is None or active == run_id:
            return
        other = await self.store.load_run(active)
        if other is None or other.status in END_STATUSES:
            return
        if other.status in WAITING_STATUSES or not is_expired(other.lease, utc_now()):
            raise _Reject("RUN_BUSY", "Another run is active in this thread; wait for it or cancel it.")
        preface.append(events.state_snapshot(await self._expire(other)))
        index.active_run_id = None
        await self.store.save_thread(index)

    async def _resume(self, thread_id: str, run_id: str, action: OrionAction) -> AsyncGenerator[BaseEvent, None]:
        follow: tuple[RunHandle, asyncio.Queue[BaseEvent]] | None = None
        replies: list[BaseEvent] = []
        name: ActionName = action.action
        async with self._thread_locks[thread_id]:
            handle: RunHandle | None = None
            try:
                handle, record = await self._target(thread_id, run_id)
                live = handle is not None and handle.task is not None and not handle.task.done()
                if not live and record.status in LEASED_STATUSES and is_expired(record.lease, utc_now()):
                    # Check 4: recorded as of the lease's expiry, reported, and the turn ends with that status. The
                    # turn is accepted, so its reports are recorded: the target's go into the expiry's transition.
                    await self._take_reports(record, None, action.viz_reports)
                    view = await self._expire(record)
                    await self._clear_index(thread_id, run_id)
                    replies = [events.state_snapshot(view), terminal_for(record)]
                elif isinstance(action, CancelAction):
                    reported = await self._take_reports(record, handle if live else None, action.viz_reports)
                    follow, replies = await self._cancel(handle if live else None, record, reported)
                else:
                    preface: list[BaseEvent] = []
                    index = await self.store.load_thread(thread_id)
                    await self._other_busy(index, run_id, preface)
                    if not live and record.status in LEASED_STATUSES:
                        raise _Reject("RUN_BUSY", "This run is owned by another controller; try again shortly.")
                    follow, replies = await self._accept(handle if live else None, record, index, action, preface)
            except _Reject as error:
                log.info("%s rejected: thread %s run %s %s %s", name, thread_id, run_id, error.code, error.message)
                if handle is not None and handle.writer is not None and handle.task is not None and not handle.task.done():
                    await handle.writer.stage(partial(_log_action, action=name, outcome="rejected", code=error.code))
                replies = [events.run_error(error.code, error.message)]
        if follow is not None:
            async with aclosing(self._drain(*follow)) as stream:
                async for event in stream:
                    yield event
            return
        for event in replies:
            yield event

    async def _cancel(
        self, handle: RunHandle | None, record: RunRecord, reported: bool = False
    ) -> tuple[tuple[RunHandle, asyncio.Queue[BaseEvent]] | None, list[BaseEvent]]:
        """`reported`: the turn carried reports for this run, kept on the at-rest `record` (saved even when nothing else changes)."""
        if handle is not None and handle.status not in END_STATUSES:
            if not handle.stop.is_set():
                # A cancel that finds the stop signalled but not yet shown (a first cancel still saving Stopping)
                # joins it; otherwise it asks for the stop, which is signalled even if the Stopping save fails.
                try:
                    if await self._request_stop(handle):
                        await self._clear_index(handle.thread_id, handle.run_id)
                except Exception:
                    if handle.status in END_STATUSES:
                        raise  # a waiting run's Cancelled end was not saved: the turn fails, the record keeps the pause
                    log.info("run %s: Stopping was not saved; the stop still applies and the run ends Cancelled", handle.run_id)
            # Subscribe once Stopping is applied, with no await in between: the turn's first snapshot is the run
            # with the cancel applied (contract "What the agent emits on a resume"), never a snapshot the run's
            # task published while the stop waited for the record writer. A run already Stopping, or one that
            # ended meanwhile, sends its current snapshot first (and its terminal event, if it has one).
            queue = handle.subscribe(current=True)
            return (handle, queue), []
        if record.status in END_STATUSES:
            if reported:
                await RunWriter(self.store, record, self.owner, self.settings.lease_ttl_s).save_quietly(lambda r: None)
            return None, [events.state_snapshot(record.view()), terminal_for(record)]
        if record.status in LEASED_STATUSES:
            raise _Reject("RUN_BUSY", "This run is owned by another controller; try again shortly.")
        writer = RunWriter(self.store, record, self.owner, self.settings.lease_ttl_s)

        def cancel(r: RunRecord) -> None:
            _cancel_mark(r)
            _log_action(r, "cancel", "accepted")

        view = await writer.transition(cancel)
        await self._clear_index(record.thread_id, record.run_id)
        return None, [events.state_snapshot(view), terminal_for(record)]

    async def _request_stop(self, handle: RunHandle) -> bool:
        """Stopping shows at once; then the in-flight call is aborted (contract "cancel").

        `stop` is set before the Stopping transition, so the run's task makes no other status change once
        a cancel has begun (`_guarded`); `abort` is set after it, so Stopping shows before the aborted
        call's Skipped row. Both are set even when the invocation is cancelled at the save (a client
        disconnect, the BFF preempting the turn) or the save fails, and the shielded transition still
        completes, so a lost cancel can never leave a run Stopping that is not stopping.

        A run whose task just paused (a cancel that arrived while the pause was being saved) is waiting
        when the transition applies: it ends Cancelled at once, as a waiting run's cancel does, and its
        terminal replaces the pause's. Returns True in that case.
        """
        assert handle.writer is not None
        writer = handle.writer
        ended = False

        def stopping(r: RunRecord) -> bool:
            nonlocal ended
            if r.status in END_STATUSES or r.status == "Stopping":
                return False
            if r.status in WAITING_STATUSES:
                _cancel_mark(r)
                ended = True
            else:
                r.status = "Stopping"
            _log_action(r, "cancel", "accepted")
            return True

        async def apply() -> bool:
            await writer.transition(stopping)
            if ended:
                handle.publish(terminal_for(writer.record))
            return ended

        handle.stop.set()
        work = asyncio.ensure_future(apply())
        work.add_done_callback(partial(_log_stop_failure, run_id=handle.run_id))
        try:
            return await asyncio.shield(work)
        finally:
            handle.abort.set()

    async def _accept(
        self, handle: RunHandle | None, record: RunRecord, index: ThreadIndex, action: OrionAction, preface: list[BaseEvent]
    ) -> tuple[tuple[RunHandle, asyncio.Queue[BaseEvent]] | None, list[BaseEvent]]:
        """Checks 6 to 8 for a resume, then apply it. Returns a stream to follow, or the replies to send.

        Ride-along reports are recorded once the checks pass (a rejected turn records none).
        """
        name: ActionName = action.action
        if isinstance(action, ConfirmAction):
            recorded = next((c for c in record.confirmations if c.nonce == action.nonce and c.outcome != "voided"), None)
            if recorded is not None:
                await self._take_reports(record, handle, action.viz_reports)
                return await self._replay(handle, record, name, preface)
        latest = bool(index.run_ids) and index.run_ids[-1] == record.run_id
        if record.status not in ACTION_STATUSES[name]:
            raise _Reject("STALE_ACTION", f"The run is {record.status}; it cannot take {name} now.")
        if isinstance(action, (RetryStepAction, EditScopeAction, EditMethodAction, EditTopNAction, EditThresholdsAction, EditCohortNameAction)) and not latest:
            raise _Reject("STALE_ACTION", f"{name} applies only to the thread's latest run.")
        if handle is not None:
            if isinstance(action, (EditScopeAction, EditMethodAction, EditTopNAction, EditThresholdsAction, EditCohortNameAction)):
                return await self._edit_live(handle, record, action, preface)
            raise _Reject("STALE_ACTION", f"The run is {record.status}; it cannot take {name} now.")
        if isinstance(action, ConfirmAction):
            applied = self._apply_confirm(record, action)
        elif isinstance(action, ClarifyAnswerAction):
            applied = self._apply_clarify(record, action)
        elif isinstance(action, ConsentAction):
            if not isinstance(record.pending, ConsentPending):
                raise _Reject("STALE_ACTION", "This run is not waiting for consent.")
            await self._take_reports(record, None, action.viz_reports)
            return await self._apply_consent(record, action, preface)
        elif isinstance(action, RetryStepAction):
            applied = self._apply_retry(record, action)
        elif isinstance(action, (EditScopeAction, EditMethodAction, EditTopNAction, EditThresholdsAction, EditCohortNameAction)):
            edit = await self._build_edit(record, action)
            if edit is None:
                await self._take_reports(record, None, action.viz_reports)
                return await self._replay(None, record, name, preface)
            applied = self._apply_edit_at_rest(record, edit)
        else:
            raise _Reject("NOT_IMPLEMENTED", f"The {name} action is not implemented.")
        await self._take_reports(record, None, action.viz_reports)
        return self._started(record, preface, *applied)

    async def _take_reports(self, record: RunRecord, handle: RunHandle | None, reports: Sequence[VizReport]) -> bool:
        """Ride-along reports of an accepted turn: the target run's go into its record (staged on a live writer,
        or into the at-rest record the applied transition saves); other runs' follow "Who writes a record".
        Returns True when the at-rest record took any, so a turn that changes nothing else still saves it."""
        kept = False
        for report in await self._ride_along(record.thread_id, reports, record):
            if handle is not None and handle.writer is not None:
                await handle.writer.stage(partial(_keep_report, report=report))
            else:
                _keep_report(record, report)
                kept = True
        return kept

    async def _replay(
        self, handle: RunHandle | None, record: RunRecord, name: ActionName, preface: list[BaseEvent]
    ) -> tuple[tuple[RunHandle, asyncio.Queue[BaseEvent]] | None, list[BaseEvent]]:
        """A repeated confirm or a no-op edit: joins the live stream, or sends the current snapshot and terminal."""
        if handle is not None and handle.task is not None and not handle.task.done():
            assert handle.writer is not None
            await handle.writer.stage(partial(_log_action, action=name, outcome="repeat", code=None))
            return (handle, handle.subscribe(current=True, preface=preface)), []
        writer = RunWriter(self.store, record, self.owner, self.settings.lease_ttl_s)
        await writer.save_quietly(partial(_log_action, action=name, outcome="repeat", code=None))
        return None, [*preface, events.state_snapshot(record.view()), terminal_for(record)]

    def _started(
        self, record: RunRecord, preface: list[BaseEvent], mutate: Callable[[RunRecord], None], body: Callable[[RunHandle], Awaitable[None]],
    ) -> tuple[tuple[RunHandle, asyncio.Queue[BaseEvent]] | None, list[BaseEvent]]:
        """Take over a run at rest: one applied transition (revision + 1), then its task streams on."""
        handle = self._new_handle(record)
        queue = handle.subscribe(preface=preface)

        async def go() -> None:
            assert handle.writer is not None
            if not await self._guarded(handle, mutate):
                return  # a cancel reached the taken-over run before its applied transition; the cancel ends it
            async with self._thread_locks[handle.thread_id]:
                index = await self.store.load_thread(handle.thread_id)
                if handle.writer.record.status not in END_STATUSES and index.active_run_id != handle.run_id:
                    index.active_run_id = handle.run_id
                    await self.store.save_thread(index)
            await body(handle)

        self._start(handle, go)
        return (handle, queue), []

    # confirm ---------------------------------------------------------------------------------

    def _apply_confirm(
        self, record: RunRecord, action: ConfirmAction
    ) -> tuple[Callable[[RunRecord], None], Callable[[RunHandle], Awaitable[None]]]:
        pending = record.pending
        if not isinstance(pending, ConfirmPending) or pending.nonce != action.nonce:
            raise _Reject("STALE_ACTION", "This confirmation's nonce is not the pending one.")
        refs = [c.ref for c in pending.cohorts]
        if action.decision == "create" and sorted(s.ref for s in action.saved) != sorted(refs):
            raise _Reject("INVALID_INPUT", "saved must list every pending cohort exactly once.")
        problems = verify_read_back(pending, action) if action.decision == "create" else []
        plan = record.plan_versions[-1]
        at = iso_utc(utc_now())

        def apply(r: RunRecord) -> None:
            r.confirmations.append(
                ConfirmationRecord(
                    nonce=action.nonce, decision=action.decision, saved=list(action.saved), make_active=action.make_active, at=at,
                    outcome="verify_failed" if problems else "accepted",
                )
            )
            r.status, r.code, r.pending = "Executing", None, None
            if action.decision == "create":
                _set_step(r, pending.step_id, status="Running")
            else:
                _set_step(r, pending.step_id, status="Skipped", output_summary=DONT_SAVE_SUMMARY, code="WRITE_SKIPPED")
                r.cohorts = [c.model_copy(update={"save": "skipped"}) if c.ref in refs else c for c in r.cohorts]
                r.answer.limitations = _merge_limitations(r.answer.limitations, [save_skipped(len(refs))])
            _log_action(r, "confirm", "accepted")

        async def body(handle: RunHandle) -> None:
            assert handle.writer is not None
            resume = self._resume_state(handle.writer.record, confirmation=action, pending=pending)
            await self._continue(handle, plan, resume)

        return apply, body

    # clarify and consent ------------------------------------------------------------------------

    def _apply_clarify(
        self, record: RunRecord, action: ClarifyAnswerAction
    ) -> tuple[Callable[[RunRecord], None], Callable[[RunHandle], Awaitable[None]]]:
        pending = record.pending
        if not isinstance(pending, ClarifyPending):
            raise _Reject("STALE_ACTION", "This run is not waiting for a clarification.")
        option = next((o for o in pending.options if o.option_id == action.option_id), None)
        if option is None:
            raise _Reject("INVALID_INPUT", f"{action.option_id} is not one of the offered options.")
        clarification = Clarification(slot=pending.slot, option_id=option.option_id, field=option.field)

        def apply(r: RunRecord) -> None:
            r.status, r.code, r.pending = "Planning", None, None
            _log_action(r, "clarify_answer", "accepted")

        async def body(handle: RunHandle) -> None:
            await self._plan(handle, clarification)

        return apply, body

    async def _apply_consent(
        self, record: RunRecord, action: ConsentAction, preface: list[BaseEvent]
    ) -> tuple[tuple[RunHandle, asyncio.Queue[BaseEvent]] | None, list[BaseEvent]]:
        plan = record.plan_versions[-1]
        if not action.accept:
            writer = RunWriter(self.store, record, self.owner, self.settings.lease_ttl_s)

            def decline(r: RunRecord) -> None:
                r.steps = [s.model_copy(update={"status": "Skipped", "output_summary": DECLINED_SUMMARY}) if s.status == "Queued" else s for s in r.steps]
                r.status, r.code, r.pending = "Cancelled", None, None
                _log_action(r, "consent", "accepted")

            view = await writer.transition(decline)
            await self._clear_index(record.thread_id, record.run_id)
            return None, [*preface, events.state_snapshot(view), terminal_for(record)]

        def accept(r: RunRecord) -> None:
            r.status, r.code, r.pending = "Executing", None, None  # the NAMED_TOOL_MISMATCH line stays
            _log_action(r, "consent", "accepted")

        async def body(handle: RunHandle) -> None:
            await self._continue(handle, plan, None)

        return self._started(record, preface, accept, body)

    # retry_step ---------------------------------------------------------------------------------

    def _apply_retry(
        self, record: RunRecord, action: RetryStepAction
    ) -> tuple[Callable[[RunRecord], None], Callable[[RunHandle], Awaitable[None]]]:
        plan = record.plan_versions[-1]
        specs = {s.id: s for s in plan.steps}
        if action.step_id not in specs:
            raise _Reject("INVALID_INPUT", f"{action.step_id} is not a step of the current plan.")
        step = next(s for s in record.steps if s.id == action.step_id)
        if step.status != "Failed" or step.kind == "client":
            raise _Reject("STALE_ACTION", f"{action.step_id} is {step.status}; only a failed read can be retried.")
        requeue = {action.step_id}
        changed = True
        while changed:
            changed = False
            for spec in plan.steps:
                view = next(s for s in record.steps if s.id == spec.id)
                if spec.id not in requeue and view.status == "Skipped" and view.code != "WRITE_SKIPPED" and set(spec.depends_on) & requeue:
                    requeue.add(spec.id)
                    changed = True

        def apply(r: RunRecord) -> None:
            r.steps = [
                s.model_copy(update={"status": "Queued", "output_summary": None, "duration_ms": None, "code": None})
                if s.id in requeue else s
                for s in r.steps
            ]
            r.status, r.code = "Executing", None
            r.answer.withheld_reason = None
            _log_action(r, "retry_step", "accepted")

        async def body(handle: RunHandle) -> None:
            assert handle.writer is not None
            resume = self._resume_state(handle.writer.record, retry_step_id=action.step_id)
            await self._continue(handle, plan, resume)

        return apply, body

    # edit_plan ---------------------------------------------------------------------------------

    async def _build_edit(self, record: RunRecord, action: EditPlanAction) -> PendingEdit | None:
        """Checks 7 and 8 for an edit, and plan version n+1 compiled without a model call. None: a no-op edit."""
        if not record.plan_versions:
            # A run that ended Limited before planning (a second clarify, a clarify or compile stop) has nothing to edit.
            raise _Reject("INVALID_INPUT", "This run has no plan to edit.")
        plan = record.plan_versions[-1]
        tools = {s.tool for s in plan.steps}
        if isinstance(action, (EditMethodAction, EditThresholdsAction)) and "de_run" not in tools:
            raise _Reject("INVALID_INPUT", f"This plan has no differential expression step to take a {action.field} edit.")
        if isinstance(action, EditTopNAction):
            editable = next((e for e in plan.editable if e.field == "topN"), None)
            limit = editable.max if editable is not None else (MAX_TOP_N_DE if "de_run" in tools else MAX_TOP_N_GENES)
            if not ({"rank_top_n", "top_mutated_genes"} & tools) or action.value > limit:
                raise _Reject("INVALID_INPUT", f"This plan cannot take a top N of {action.value}.")
        if isinstance(action, EditCohortNameAction):
            cohort = next((c for c in record.cohorts if c.ref == action.ref and c.role == "planned"), None)
            if cohort is None:
                raise _Reject("INVALID_INPUT", f"{action.ref} is not a planned cohort of this run.")
            if cohort.save != "proposed":
                raise _Reject("STALE_ACTION", f"{cohort.name} is no longer proposed for saving.")
        if self._unchanged(plan, record, action):
            return None
        context = action.cohort_context if isinstance(action, EditScopeAction) else _scope_context(record)
        patched_ir = _current_ir(record)
        compiler_edit = getattr(self.compiler, "edit", None)
        if callable(compiler_edit) and patched_ir is not None:
            ledger = None
            if self.ledger_store is not None:
                with contextlib.suppress(Exception):
                    ledger = await self.ledger_store.ledger(record.thread_id, before_run_id=record.run_id)
            try:
                edited = compiler_edit(plan, patched_ir, action, context, ledger)
            except Exception as error:
                code, message = getattr(error, "code", None), getattr(error, "message", None)
                if isinstance(code, str) and isinstance(message, str):  # compiler.EditRejected
                    raise _Reject("STALE_ACTION" if code == "STALE_ACTION" else "INVALID_INPUT", message) from None
                raise
            result = getattr(edited, "result", None)
            if result is None:
                return None
            if isinstance(result, CompileStop):
                text = result.limitations[0].text if result.limitations else result.code
                raise _Reject("INVALID_INPUT", f"The plan cannot take this edit: {text}")
            if not isinstance(result, CompiledPlan):
                raise TypeError("the compiler's edit returned no plan")
            new_ir = getattr(edited, "ir", patched_ir)
            return PendingEdit(action=action, plan=result, ir=new_ir if isinstance(new_ir, ir.Plan) else patched_ir, context=context)
        if isinstance(action, (EditScopeAction, EditMethodAction, EditTopNAction)):
            if patched_ir is None:
                raise _Reject("INVALID_INPUT", "This run has no plan to edit.")
            patched_ir = _patch_ir(patched_ir, action)
            ledger = None
            if self.ledger_store is not None:
                with contextlib.suppress(Exception):
                    ledger = await self.ledger_store.ledger(record.thread_id, before_run_id=record.run_id)
            compiled = self.compiler.compile(patched_ir, context, ledger, plan.version + 1)
            if isinstance(compiled, CompileStop):
                text = compiled.limitations[0].text if compiled.limitations else compiled.code
                raise _Reject("INVALID_INPUT", f"The plan cannot take this edit: {text}")
            for entry in record.plan_history:
                if isinstance(entry.edit, (EditThresholdsAction, EditCohortNameAction)):
                    compiled = _patch_plan(compiled, entry.edit, reapply=True)
        else:
            compiled = _patch_plan(plan, action)
        return PendingEdit(action=action, plan=compiled, ir=patched_ir, context=context)

    def _unchanged(self, plan: CompiledPlan, record: RunRecord, action: EditPlanAction) -> bool:
        """An edit is a no-op when the edited value equals the current editable value (contract "What an edit changes")."""
        for item in plan.editable:
            if item.field != action.field:
                continue
            if isinstance(action, EditCohortNameAction):
                if getattr(item, "ref", None) == action.ref and item.value == action.value:
                    return True
                continue
            current = item.value.model_dump(mode="json") if isinstance(item.value, BaseModel) else item.value
            given = action.value.model_dump(mode="json") if isinstance(action.value, BaseModel) else action.value
            return bool(current == given)
        if isinstance(action, EditCohortNameAction):
            cohort = next((c for c in record.cohorts if c.ref == action.ref), None)
            return cohort is not None and cohort.name == action.value
        return False

    def _edit_mutation(self, edit: PendingEdit) -> Callable[[RunRecord], dict[str, StepOrigin]]:
        """The edit sequence's one snapshot: Executing, version n+1, superseded, a voided nonce, Reused steps."""
        vocab = self.vocab

        def apply(r: RunRecord) -> dict[str, StepOrigin]:
            at = iso_utc(utc_now())
            old = r.plan_versions[-1]
            new = edit.plan
            r.superseded.append(
                SupersededVersion(
                    plan_version=old.version, at=at, status=r.status, steps=list(r.steps), facts=list(r.facts),
                    cohorts=list(r.cohorts), answer=r.answer.model_copy(deep=True),
                )
            )
            reused = reuse_map(new, old, r.steps)
            old_views = {s.id: s for s in r.steps}
            old_origin = {
                (s.reused_from.plan_version, s.reused_from.step_id) if s.reused_from else (old.version, s.id): s for s in r.steps
            }
            steps: list[StepView] = []
            facts: list[FactRecord] = []
            for spec in new.steps:
                origin = reused.get(spec.id)
                if origin is None:
                    steps.append(spec.queued())
                    continue
                before = old_origin.get((origin.plan_version, origin.step_id)) or old_views.get(origin.step_id)
                steps.append(
                    spec.queued().model_copy(
                        update={
                            "status": "Reused", "reused_from": origin,
                            "output_summary": before.output_summary if before else None,
                            "duration_ms": before.duration_ms if before else None,
                            "code": before.code if before else None,
                        }
                    )
                )
                facts += _carried_facts(r, spec.id, spec.row, origin)
            cohorts = plan_cohorts(new, edit.context, vocab)
            old_cohorts = {c.ref: c for c in r.cohorts}
            old_steps = plan_cohorts(old, _scope_context(r), vocab).steps
            new_cohorts: list[CohortRef] = []
            for cohort in cohorts.cohorts:
                step = cohorts.steps.get(cohort.ref)
                before_cohort = old_cohorts.get(cohort.ref)
                origin = reused.get(step) if step is not None else None
                old_step = old_steps.get(cohort.ref)
                same = (
                    before_cohort is not None and origin is not None and old_step is not None
                    and (origin.step_id == old_step or (old_views.get(old_step) is not None and old_views[old_step].reused_from == origin))
                    and (cohort.def_sha256 is None or cohort.def_sha256 == before_cohort.def_sha256)
                )
                if same and before_cohort is not None and step is not None:
                    fact = before_cohort.case_count_fact_id
                    renamed = None if fact is None else f"f.{step}." + fact.split(".", 2)[2]
                    update: dict[str, object] = {
                        "def_sha256": before_cohort.def_sha256, "case_count": before_cohort.case_count, "case_count_fact_id": renamed,
                        "definition": before_cohort.definition,
                    }
                    save_step = next((s.id for s in new.steps if s.tool == "save_cohorts" and cohort.ref in _save_refs(s)), None)
                    if save_step is not None and save_step in reused and before_cohort.save in ("saved", "reused"):
                        update |= {"save": before_cohort.save, "cohort_id": before_cohort.cohort_id, "saved_name": before_cohort.saved_name}
                    cohort = cohort.model_copy(update=update)
                new_cohorts.append(cohort)
            _void_pending(r)
            r.plan_versions.append(new)
            r.plan_history.append(PlanHistoryEntry(version=new.version, cause="edit_plan", at=at, ir=edit.ir, edit=edit.action))
            r.steps, r.facts, r.cohorts = steps, facts, new_cohorts
            r.answer = AnswerRecord(limitations=list(new.limitations))
            r.status, r.code = "Executing", None
            _log_action(r, "edit_plan", "accepted")
            return reused

        return apply

    def _apply_edit_at_rest(
        self, record: RunRecord, edit: PendingEdit
    ) -> tuple[Callable[[RunRecord], None], Callable[[RunHandle], Awaitable[None]]]:
        mutation = self._edit_mutation(edit)
        reused: dict[str, StepOrigin] = {}

        def apply(r: RunRecord) -> None:
            reused.update(mutation(r))

        async def body(handle: RunHandle) -> None:
            assert handle.writer is not None
            resume = self._resume_state(handle.writer.record, reused=dict(reused))
            await self._continue(handle, edit.plan, resume)

        return apply, body

    async def _edit_live(
        self, handle: RunHandle, record: RunRecord, action: EditPlanAction, preface: list[BaseEvent]
    ) -> tuple[tuple[RunHandle, asyncio.Queue[BaseEvent]] | None, list[BaseEvent]]:
        """An edit of a run whose task is live here: wait for the running step (heartbeats only), or abort it when
        version n+1 changes its input, or abort the composer; the task applies the edit and streams on."""
        if handle.edit is not None or handle.stop.is_set():
            raise _Reject("STALE_ACTION", "This run is already stopping or taking another edit.")
        edit = await self._build_edit(record, action)
        await self._take_reports(record, handle, action.viz_reports)
        if edit is None:
            return await self._replay(handle, record, action.action, preface)
        queue: asyncio.Queue[BaseEvent] = asyncio.Queue()
        for event in preface:
            queue.put_nowait(event)
        handle.joiners.append(queue)
        handle.edit = edit
        running = handle.running_step
        old_hashes = input_hashes(record.plan_versions[-1])
        new_hashes = input_hashes(edit.plan)
        if record.status == "Answering" or (running is not None and old_hashes.get(running) != new_hashes.get(running)):
            handle.abort.set()
        return (handle, queue), []

    async def _apply_live_edit(self, handle: RunHandle) -> tuple[CompiledPlan, ResumeState] | None:
        """Inside the run's task, at a step boundary: the applied snapshot reaches the edit stream first.

        None: a cancel began before the edit applied; the edit's stream (now a subscriber) sees the stop's end.
        """
        assert handle.writer is not None and handle.edit is not None
        edit = handle.edit
        handle.edit = None
        handle.abort = asyncio.Event()
        handle.terminal = None
        handle.open_steps.clear()
        handle.attach_joiners()
        mutation = self._edit_mutation(edit)
        reused: dict[str, StepOrigin] = {}

        def apply(r: RunRecord) -> None:
            reused.update(mutation(r))

        if not await self._guarded(handle, apply):
            return None
        async with self._thread_locks[handle.thread_id]:
            index = await self.store.load_thread(handle.thread_id)
            if index.active_run_id != handle.run_id:
                index.active_run_id = handle.run_id
                await self.store.save_thread(index)
        return edit.plan, self._resume_state(handle.writer.record, reused=dict(reused))


def _code(value: object) -> Code | None:
    """A contract code another builder's object carries, or None when it is not one."""
    return cast(Code, value) if isinstance(value, str) and value in CODES else None


def _log_stop_failure(work: "asyncio.Future[bool]", run_id: str) -> None:
    """Retrieve a failed stop transition's error, also when the cancel invocation was gone before it ended."""
    if not work.cancelled() and work.exception() is not None:
        log.warning("run %s: the stop transition failed: %r", run_id, work.exception())


def _keep_model_calls(r: RunRecord, calls: Sequence[ModelCallRecord]) -> None:
    r.model_calls.extend(calls)


def _keep_report(r: RunRecord, report: VizReport) -> None:
    """The record keeps the latest report per vizId."""
    entry = VizReportRecord(
        viz_id=report.viz_id, mounted=report.mounted, error=report.error, pinned_hash_verified=report.pinned_hash_verified,
        at=iso_utc(utc_now()),
    )
    r.viz_reports = [v for v in r.viz_reports if v.viz_id != report.viz_id] + [entry]


def _save_refs(step: StepSpec) -> list[str]:
    return [str(c["ref"]) for c in _items(step.args, "cohorts") if isinstance(c.get("ref"), str)]


def _patch_ir(plan: ir.Plan, action: EditScopeAction | EditMethodAction | EditTopNAction) -> ir.Plan:
    """The IR an edit compiles (planHistory keeps it): a new base cohort, DE method, or the first top N."""
    if isinstance(action, EditScopeAction):
        choice = action.value
        base = ir.BaseCohort(source=choice.source, cohort_name=choice.cohort_name, ledger_ref=choice.ledger_ref)
        return plan.model_copy(update={"base_cohort": base})
    analyses = list(plan.analyses)
    for i, analysis in enumerate(analyses):
        if isinstance(action, EditMethodAction) and isinstance(analysis, ir.DifferentialExpression):
            analyses[i] = analysis.model_copy(update={"method": action.value})
            break
        if isinstance(action, EditTopNAction) and isinstance(analysis, ir.DifferentialExpression):
            analyses[i] = analysis.model_copy(update={"top_n": action.value})
            break
        if isinstance(action, EditTopNAction) and isinstance(analysis, ir.TopMutatedGenes):
            analyses[i] = analysis.model_copy(update={"n": action.value})
            break
    return plan.model_copy(update={"analyses": analyses})


def _items(args: dict[str, JsonValue], key: str) -> list[dict[str, JsonValue]]:
    value = args.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _replace_text(value: str, old: str, new: str) -> str:
    return value.replace(old, new) if old else value


def _patch_plan(plan: CompiledPlan, action: EditThresholdsAction | EditCohortNameAction, *, reapply: bool = False) -> CompiledPlan:
    """The edits that live in step args, not in the IR: DE thresholds and a proposed cohort's name (plan version n+1).

    `reapply` re-applies an earlier edit to a plan the compiler rebuilt (a later scope, method or top N edit),
    so edits accumulate; it skips quietly when the plan no longer has what the edit names.
    """
    steps = [s.model_copy(deep=True) for s in plan.steps]
    editable = list(plan.editable)
    assumptions = list(plan.assumptions)
    rows = list(plan.rows)
    paraphrase = plan.paraphrase
    if isinstance(action, EditThresholdsAction):
        value: JsonValue = action.value.model_dump(mode="json")
        de_ids = {s.id for s in steps if s.tool == "de_run"}
        for step in steps:
            if step.tool == "de_run" or (step.tool == "rank_top_n" and step.args.get("rows") in de_ids):
                step.args["thresholds"] = value
        editable = [e.model_copy(update={"value": action.value}) if e.field == "thresholds" else e for e in editable]
        assumptions = [
            a.model_copy(update={"code": "DE_THRESHOLDS_PLAN", "text": _thresholds_text(action.value)}) if a.code.startswith("DE_THRESHOLDS") else a
            for a in assumptions
        ]
    else:
        old = next((str(e.value) for e in editable if e.field == "cohortName" and getattr(e, "ref", None) == action.ref), None)
        if old is None:
            old = next(
                (str(c["name"]) for s in steps if s.tool == "save_cohorts" for c in _items(s.args, "cohorts") if c.get("ref") == action.ref),
                None,
            )
        if old is None:
            if reapply:
                return plan
            raise _Reject("INVALID_INPUT", f"{action.ref} is not a cohort this plan names.")
        new = action.value
        for step in steps:
            if step.tool == "save_cohorts":
                for item in _items(step.args, "cohorts"):
                    if item.get("ref") == action.ref:
                        item["name"] = new
            if step.tool == "de_run":
                for item in _items(step.args, "groups"):
                    if item.get("cohort") == action.ref:
                        item["name"] = new
            step.input_summary = _replace_text(step.input_summary, old, new)
        editable = [
            e.model_copy(update={"value": new}) if e.field == "cohortName" and getattr(e, "ref", None) == action.ref else e for e in editable
        ]
        assumptions = [a.model_copy(update={"text": _replace_text(a.text, old, new)}) for a in assumptions]
        rows = [r.model_copy(update={"label": _replace_text(r.label, old, new)}) for r in rows]
        paraphrase = _replace_text(paraphrase, old, new)
    version = plan.version if reapply else plan.version + 1
    signed = plan_signature(plan.scope, steps, assumptions, plan.writes)
    return plan.model_copy(
        update={"version": version, "signature": signed, "steps": steps, "editable": editable, "assumptions": assumptions,
                "rows": rows, "paraphrase": paraphrase}
    )


def _thresholds_text(value: DeThresholds) -> str:
    return f"Thresholds: {value.p_value_type} p < {value.p_value_cutoff:g} and |log2FC| > {value.fold_change_cutoff:g}, set on the plan"


__all__ = [
    "Controller",
    "ExecutionResult",
    "Executor",
    "Planner",
    "ResumeState",
    "RunHandle",
    "RUNS",
    "StepSession",
    "is_busy",
    "reset_registry",
    "terminal_for",
]
