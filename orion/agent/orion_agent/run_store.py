"""Run records and thread indexes on a local volume (mode L; S3 in modes A and H).

One JSON record per run, rewritten after every transition with a rising revision. Writes are
atomic (temp file, then os.replace), and each run has a single writer guarded by an
asyncio.Lock, so a lease renewal never overwrites a newer status.
"""

import asyncio
import os
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel

from . import ir
from .compiler import CompiledPlan
from .events import iso_utc, utc_now
from .lease import Lease, new_lease
from .models import (
    LEASED_STATUSES,
    RUN_VIEW_SCHEMA,
    AskAction,
    Code,
    Fact,
    HexId,
    IsoTime,
    Limitation,
    RunStatus,
    RunView,
    Sentence,
    StepView,
    VizSpec,
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


class Answer(StoreModel):
    observed: list[Sentence] = Field(default_factory=list)
    limitations: list[Limitation] = Field(default_factory=list)
    interpretation: list[Sentence] | None = None
    hypotheses: list[Sentence] | None = None
    withheld_reason: str | None = None
    viz_specs: list[VizSpec] = Field(default_factory=list)


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
    planner_output: ir.PlannerOutput | None = None
    plan_versions: list[CompiledPlan] = Field(default_factory=list)
    steps: list[StepView] = Field(default_factory=list)
    calls: list[dict[str, JsonValue]] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    answer: Answer = Field(default_factory=Answer)

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
            facts=self.facts,
            observed=self.answer.observed,
            limitations=self.answer.limitations,
            interpretation=self.answer.interpretation,
            hypotheses=self.answer.hypotheses,
            withheld_reason=self.answer.withheld_reason,
            viz_specs=self.answer.viz_specs,
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

    def _stamp(self, now: datetime) -> None:
        record = self._record
        record.revision += 1
        record.updated_at = iso_utc(now)
        record.lease = new_lease(self._owner, now, self._lease_ttl_s) if record.status in LEASED_STATUSES else None

    async def transition(self, mutate: Callable[[RunRecord], bool | None]) -> RunView:
        """Apply `mutate` under the lock; it returns False to say nothing changed (no write, no snapshot)."""
        async with self._lock:
            if mutate(self._record) is False:
                return self._record.view()
            self._stamp(utc_now())
            await self._store.save_run(self._record)
            view = self._record.view()
            if self._publish is not None:
                await self._publish(view)
            return view

    async def stage(self, mutate: Callable[[RunRecord], None]) -> None:
        """Record data the view does not show; the next transition saves it."""
        async with self._lock:
            mutate(self._record)

    async def renew_lease(self) -> bool:
        async with self._lock:
            if self._record.status not in LEASED_STATUSES:
                return False
            self._record.lease = new_lease(self._owner, utc_now(), self._lease_ttl_s)
            await self._store.save_run(self._record)
            return True
