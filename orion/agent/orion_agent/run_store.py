"""Run records and thread indexes on a local volume (mode L; S3 in modes A and H).

One JSON record per run, rewritten after every transition with a rising revision. Writes are
atomic (temp file, then os.replace), and each run has a single writer guarded by an
asyncio.Lock, so a lease renewal never overwrites a newer status. Only the run's owner writes:
the RunWriter of the task running it, or, for a run at rest (paused or ended), the invocation
that accepted an action on it (contract "Who writes a record").
"""

import asyncio
import os
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from . import ir
from .compiler import CompiledPlan
from .events import iso_utc, utc_now
from .lease import Lease, new_lease
from .models import (
    LEASED_STATUSES,
    RENEWED_STATUSES,
    RUN_VIEW_SCHEMA,
    STOP_LEASE_S,
    WAITING_STATUSES,
    ActionLogEntry,
    AnswerRecord,
    AskAction,
    CallRecord,
    Code,
    CohortRef,
    ConfirmationRecord,
    FactRecord,
    HexId,
    IsoTime,
    LedgerUse,
    ModelCallRecord,
    Pending,
    PlanHistoryEntry,
    RunStatus,
    RunView,
    Sha256Hex,
    StepView,
    SupersededVersion,
    Transport,
    ViewEnvironment,
    VizReportRecord,
)

RUN_RECORD_SCHEMA: Final = "orion.run-record/v1"
THREAD_INDEX_SCHEMA: Final = "orion.thread-index/v1"


class StoreModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )


class Environment(StoreModel):
    mode: Literal["L", "A", "H"]
    planner: str
    executor: str
    agent_version: str
    # v1.2: how tools were reached and the fixture fingerprint s1 recorded (null until it ran).
    tool_transport: Transport | None = None
    fingerprint_sha256: Sha256Hex | None = None
    # v1.2: the ORION_FAULTS in force (E11), as sorted `<target>:<kind>` strings; empty in a real run.
    faults: list[str] = Field(default_factory=list)

    def view(self) -> ViewEnvironment:
        return ViewEnvironment(mode=self.mode, fingerprint_sha256=self.fingerprint_sha256)


# The answer parts of the record; the view's answer fields come from here.
Answer = AnswerRecord


class RunRecord(StoreModel):
    schema_: Literal["orion.run-record/v1"] = Field(
        default=RUN_RECORD_SCHEMA, validation_alias="schema", serialization_alias="schema"
    )
    thread_id: HexId
    run_id: HexId
    revision: int = 0
    status: RunStatus
    code: Code | None = None
    question: str
    created_at: IsoTime
    updated_at: IsoTime
    environment: Environment
    lease: Lease | None = None
    request: AskAction
    # The latest planner output (a Plan, Clarify or Unsupported); planHistory keeps each version's IR.
    planner_output: ir.PlannerOutput | None = None
    plan_versions: list[CompiledPlan] = Field(default_factory=list)
    # v1.2: why each plan version exists, joined to planVersions by version.
    plan_history: list[PlanHistoryEntry] = Field(default_factory=list)
    model_calls: list[ModelCallRecord] = Field(default_factory=list)
    # The current plan version's steps.
    steps: list[StepView] = Field(default_factory=list)
    calls: list[CallRecord] = Field(default_factory=list)
    facts: list[FactRecord] = Field(default_factory=list)
    cohorts: list[CohortRef] = Field(default_factory=list)
    # The open pause, with full cohort definitions; null unless the run is in a waiting status.
    pending: Pending | None = None
    confirmations: list[ConfirmationRecord] = Field(default_factory=list)
    viz_reports: list[VizReportRecord] = Field(default_factory=list)
    actions: list[ActionLogEntry] = Field(default_factory=list)
    ledger: LedgerUse = Field(default_factory=LedgerUse)
    answer: Answer = Field(default_factory=Answer)
    superseded: list[SupersededVersion] = Field(default_factory=list)
    # The RunView as of the last transition, untrimmed; the BFF view route serves it.
    run_view: RunView | None = None

    def view(self) -> RunView:
        return RunView(
            schema_=RUN_VIEW_SCHEMA,
            thread_id=self.thread_id,
            run_id=self.run_id,
            revision=self.revision,
            status=self.status,
            code=self.code,
            question=self.question,
            updated_at=self.updated_at,
            plan=self.plan_versions[-1].view() if self.plan_versions else None,
            steps=self.steps,
            facts=[fact.to_view() for fact in self.facts],
            observed=self.answer.observed,
            limitations=self.answer.limitations,
            interpretation=self.answer.interpretation,
            hypotheses=self.answer.hypotheses,
            withheld_reason=self.answer.withheld_reason,
            viz_specs=self.answer.viz_specs,
            pending=self.pending if self.status in WAITING_STATUSES else None,
            cohorts=[cohort.to_view() for cohort in self.cohorts],
            follow_ups=self.answer.follow_ups,
            open_links=self.answer.open_links,
            superseded=[version.summary() for version in self.superseded],
            notice=self.answer.notice,
            environment=self.environment.view(),
        )


class ThreadIndex(StoreModel):
    schema_: Literal["orion.thread-index/v1"] = Field(
        default=THREAD_INDEX_SCHEMA, validation_alias="schema", serialization_alias="schema"
    )
    thread_id: HexId
    run_ids: list[HexId] = Field(default_factory=list)
    active_run_id: HexId | None = None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def run_path(self, run_id: str) -> Path:
        return self.root / "runs" / f"{run_id}.json"

    def thread_path(self, thread_id: str) -> Path:
        return self.root / "threads" / f"{thread_id}.json"

    async def load_run(self, run_id: str) -> RunRecord | None:
        text = await asyncio.to_thread(_read, self.run_path(run_id))
        return None if text is None else RunRecord.model_validate_json(text)

    async def save_run(self, record: RunRecord) -> None:
        await asyncio.to_thread(_atomic_write, self.run_path(record.run_id), record.model_dump_json(indent=1))

    async def load_thread(self, thread_id: str) -> ThreadIndex:
        text = await asyncio.to_thread(_read, self.thread_path(thread_id))
        return ThreadIndex(thread_id=thread_id) if text is None else ThreadIndex.model_validate_json(text)

    async def save_thread(self, index: ThreadIndex) -> None:
        await asyncio.to_thread(_atomic_write, self.thread_path(index.thread_id), index.model_dump_json(indent=1))


Publish = Callable[[RunView], Awaitable[None]]


class RunWriter:
    """The only writer of one run record. Every transition bumps the revision, saves, then publishes."""

    def __init__(
        self, store: RunStore, record: RunRecord, owner: str, lease_ttl_s: float, publish: Publish | None = None
    ) -> None:
        self._store = store
        self._record = record
        self._owner = owner
        self._lease_ttl_s = lease_ttl_s
        self._publish = publish
        self._lock = asyncio.Lock()

    @property
    def record(self) -> RunRecord:
        return self._record

    def _stamp(self, now: datetime, before: RunStatus, at: datetime | None) -> None:
        record = self._record
        record.revision += 1
        record.updated_at = iso_utc(at or now)
        if record.status == "Stopping":
            # Stop aborts the in-flight call at once, so Stopping is short: its lease is set on entry,
            # lapses STOP_LEASE_S later and is never renewed (an expired Stopping run ends Cancelled).
            if before != "Stopping" or record.lease is None:
                record.lease = new_lease(self._owner, now, min(self._lease_ttl_s, STOP_LEASE_S))
        elif record.status in LEASED_STATUSES:
            record.lease = new_lease(self._owner, now, self._lease_ttl_s)
        else:
            record.lease = None

    async def transition(self, mutate: Callable[[RunRecord], bool | None], *, at: datetime | None = None) -> RunView:
        """Apply `mutate` under the lock; it returns False to say nothing changed (no write, no snapshot).

        `at` sets updatedAt instead of now: an expired run is recorded as of its lease's expiry,
        exactly as the BFF view route shows it.
        """
        async with self._lock:
            before = self._record.status
            if mutate(self._record) is False:
                return self._record.view()
            self._stamp(utc_now(), before, at)
            view = self._record.view()
            self._record.run_view = view
            await self._store.save_run(self._record)
            if self._publish is not None:
                await self._publish(view)
            return view

    async def stage(self, mutate: Callable[[RunRecord], None]) -> None:
        """Record data the view does not show; the next transition saves it."""
        async with self._lock:
            mutate(self._record)

    async def renew_lease(self) -> bool:
        async with self._lock:
            if self._record.status not in RENEWED_STATUSES:
                return False
            self._record.lease = new_lease(self._owner, utc_now(), self._lease_ttl_s)
            await self._store.save_run(self._record)
            return True
