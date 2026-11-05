"""Shared test helpers: an ASGI driver for the real AGUIApp, the stream invariant checker and doubles."""

import asyncio
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bedrock_agentcore.runtime import AGUIApp
from pydantic import JsonValue
from starlette.types import Message

from orion_agent import controller as controller_module
from orion_agent.app import create_app
from orion_agent.compiler import CompiledPlan, Compiler
from orion_agent.controller import Controller, ExecutionResult, Executor, Planner, StepSession
from orion_agent.ids import new_id
from orion_agent.models import (
    REJECTION_CODES,
    SNAPSHOT_LIMIT_BYTES,
    Code,
    CohortContext,
    Limitation,
    RunErrorFrame,
    RunFinishedFrame,
    RunStartedFrame,
    StateSnapshotFrame,
    StepFinishedFrame,
    StepStartedFrame,
    WireEvent,
    iter_sse_data,
    parse_frame,
)
from orion_agent.run_store import RunStore
from orion_agent.settings import DefaultScope, Settings
from orion_agent.stubs import StubExecutor, StubPlanner
from orion_agent.vocab import Vocabulary

FIELDS_PATH = Path(os.environ.get("ORION_FIELDS_PATH", "/config/cohort-fields.json"))
CONTRACTS = Path(os.environ.get("ORION_CONTRACTS_DIR", "/contracts"))
FIXTURE = CONTRACTS / "fixtures" / "j1-stub.events.jsonl"
PINNED_VECTORS = CONTRACTS / "fixtures" / "viz-pinned-vectors.json"
J1_QUESTION = "Compare survival of CGS high-risk vs. CGS standard-risk patients"
_VOCAB: Vocabulary | None = None


def vocabulary() -> Vocabulary:
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = Vocabulary.load(FIELDS_PATH)
    return _VOCAB


def context(
    root: dict[str, dict[str, JsonValue]] | None = None,
    name: str = "All cases",
    case_count: int | None = 1000,
    saved: list[dict[str, JsonValue]] | None = None,
) -> CohortContext:
    return CohortContext.model_validate(
        {
            "active": {"id": None, "name": name, "filters": {"mode": "and", "root": root or {}}, "caseCount": case_count},
            "saved": saved or [],
        }
    )


def ask_body(
    thread_id: str, run_id: str, question: str = J1_QUESTION, cohort_context: CohortContext | None = None
) -> dict[str, JsonValue]:
    ctx = (cohort_context or context()).model_dump(mode="json")
    return {
        "threadId": thread_id,
        "runId": run_id,
        "state": {},
        "messages": [{"id": new_id(), "role": "user", "content": question}],
        "tools": [],
        "context": [],
        "forwardedProps": {"orion": {"action": "ask", "question": question, "cohortContext": ctx}},
    }


def action_body(thread_id: str, run_id: str, orion: dict[str, JsonValue] | None) -> dict[str, JsonValue]:
    return {
        "threadId": thread_id,
        "runId": run_id,
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {} if orion is None else {"orion": orion},
    }


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    text: str

    def frames(self) -> list[WireEvent]:
        return [parse_frame(data) for data in iter_sse_data(self.text)]


async def asgi_call(
    app: AGUIApp, method: str, path: str, body: dict[str, JsonValue] | None = None, disconnect_after: int | None = None
) -> Response:
    """Drive the ASGI app the way uvicorn 0.54 does (ASGI spec 2.3, so Starlette watches for disconnects)."""
    payload = json.dumps(body).encode() if body is not None else b""
    disconnect = asyncio.Event()
    sent_body = False
    status = 0
    headers: dict[str, str] = {}
    chunks: list[bytes] = []

    async def receive() -> Message:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            headers.update({k.decode(): v.decode() for k, v in message["headers"]})
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            frames = b"".join(chunks).count(b"data:")
            if disconnect_after is not None and frames >= disconnect_after:
                disconnect.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"accept", b"text/event-stream"), (b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    await app(scope, receive, send)
    return Response(status, headers, b"".join(chunks).decode("utf-8"))


def check_stream(case: unittest.TestCase, text: str, expect_run_id: str | None = None) -> list[WireEvent]:
    """The contract's invariants for one invocation's stream with valid ids.

    Snapshots belong to the invocation's run, except that an ask may first report an earlier run
    of the same thread that it found with an expired lease (A2 "Disconnects"). A rejection
    touches no run, so its RUN_ERROR comes right after RUN_STARTED.
    """
    raw = list(iter_sse_data(text))
    frames = [parse_frame(data) for data in raw]
    case.assertTrue(frames, "empty stream")
    first = frames[0]
    case.assertIsInstance(first, RunStartedFrame, "first event must be RUN_STARTED")
    assert isinstance(first, RunStartedFrame)
    if expect_run_id is not None:
        case.assertEqual(first.run_id, expect_run_id)
    terminals = [i for i, f in enumerate(frames) if isinstance(f, (RunFinishedFrame, RunErrorFrame))]
    case.assertEqual(len(terminals), 1, f"exactly one terminal event, got {len(terminals)}")
    case.assertEqual(terminals[0], len(frames) - 1, "the terminal event ends the stream")
    last = frames[-1]
    if isinstance(last, RunFinishedFrame):
        case.assertEqual((last.thread_id, last.run_id), (first.thread_id, first.run_id))
    if isinstance(last, RunFinishedFrame) and last.result.status == "Interrupted":
        report = frames[1]
        case.assertIsInstance(report, StateSnapshotFrame, "an Interrupted run's snapshot comes right after RUN_STARTED")
        assert isinstance(report, StateSnapshotFrame)
        case.assertEqual((report.snapshot.run_id, report.snapshot.status), (first.run_id, "Interrupted"))
        case.assertEqual(last.result.code, "RUN_INTERRUPTED")
    if isinstance(last, RunErrorFrame) and last.code in REJECTION_CODES:
        case.assertEqual(len(frames), 2, f"a {last.code} rejection follows RUN_STARTED directly")
    revisions: list[int] = []
    plan_seen = False
    open_steps: set[str] = set()
    for data, frame in zip(raw, frames):
        if isinstance(frame, StateSnapshotFrame):
            case.assertLessEqual(len(f"data: {data}\n\n".encode()), SNAPSHOT_LIMIT_BYTES)
            case.assertEqual(frame.snapshot.thread_id, first.thread_id)
            limitations = json.loads(data)["snapshot"]["limitations"]
            case.assertTrue(all("nearestApps" in item for item in limitations), "every limitation carries nearestApps")
            if frame.snapshot.run_id != first.run_id:
                case.assertEqual(frame.snapshot.status, "Interrupted", "only an interrupted earlier run is reported")
                case.assertFalse(revisions, "an earlier run is reported before this run's own snapshots")
                continue
            revisions.append(frame.snapshot.revision)
            plan_seen = plan_seen or frame.snapshot.plan is not None
        elif isinstance(frame, StepStartedFrame):
            case.assertTrue(plan_seen, f"STEP_STARTED {frame.step_name} before the plan snapshot")
            open_steps.add(frame.step_name)
        elif isinstance(frame, StepFinishedFrame):
            case.assertIn(frame.step_name, open_steps, f"STEP_FINISHED {frame.step_name} without a start")
            open_steps.discard(frame.step_name)
    case.assertEqual(revisions, sorted(set(revisions)), f"revisions must rise: {revisions}")
    case.assertFalse(open_steps, f"steps started but never finished: {open_steps}")
    return frames


def snapshots(frames: list[WireEvent]) -> list[StateSnapshotFrame]:
    return [f for f in frames if isinstance(f, StateSnapshotFrame)]


class GatedExecutor:
    """A test double that starts each step and waits for the test to release it. It produces no facts."""

    def __init__(self, output_summary: str = "test double") -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.output_summary = output_summary

    async def execute(self, plan: CompiledPlan, session: StepSession) -> ExecutionResult:
        for step in plan.steps:
            if session.stop_requested:
                break
            await session.start_step(step.id)
            self.started.set()
            await self.release.wait()
            await session.finish_step(step.id, "Done", self.output_summary, 1, None)
        limitation = Limitation(code="NOT_IMPLEMENTED", text="Test double: no data was read.", unblock=None)
        return ExecutionResult(status="Limited", code="NOT_IMPLEMENTED", limitations=[limitation])


class FailingExecutor:
    async def execute(self, plan: CompiledPlan, session: StepSession) -> ExecutionResult:
        await session.start_step(plan.steps[0].id)
        raise RuntimeError("simulated executor bug")


class ControllerCase(unittest.IsolatedAsyncioTestCase):
    """Each case gets a fresh run directory and an empty run registry."""

    def setUp(self) -> None:
        controller_module.reset_registry()
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        for handle in list(controller_module.RUNS.values()):
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()
        controller_module.reset_registry()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make(
        self,
        executor: Executor | None = None,
        planner: Planner | None = None,
        default_scope: DefaultScope = "active_cohort",
        heartbeat_s: float = 15.0,
        lease_renew_s: float = 30.0,
        lease_ttl_s: float = 180.0,
    ) -> tuple[Controller, AGUIApp]:
        settings = Settings(
            run_dir=self.run_dir, fields_path=FIELDS_PATH, default_scope=default_scope,
            heartbeat_s=heartbeat_s, lease_renew_s=lease_renew_s, lease_ttl_s=lease_ttl_s,
        )
        controller = Controller(
            settings=settings,
            store=RunStore(self.run_dir),
            compiler=Compiler(vocabulary(), default_scope),
            planner=planner or StubPlanner(),
            executor=executor or StubExecutor(),
        )
        return controller, create_app(controller)

    async def wait_for(self, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() > deadline:
                self.fail("condition not met in time")
            await asyncio.sleep(0.01)

    def read_json(self, path: Path) -> dict[str, Any]:  # Any: assertions walk arbitrary record JSON.
        value: dict[str, Any] = json.loads(path.read_text())
        return value

    def stored(self) -> dict[str, str]:
        """Every run record and thread index on disk, to show that an invocation changed none."""
        return {str(p.relative_to(self.run_dir)): p.read_text() for p in sorted(self.run_dir.rglob("*.json"))}

    async def assert_rejected(self, app: AGUIApp, body: dict[str, JsonValue], code: Code) -> None:
        """RUN_STARTED, then one RUN_ERROR with `code`; no run record or thread index was created or changed."""
        before = self.stored()
        frames = check_stream(self, (await asgi_call(app, "POST", "/invocations", body)).text)
        self.assertEqual([type(f) for f in frames], [RunStartedFrame, RunErrorFrame])
        last = frames[-1]
        assert isinstance(last, RunErrorFrame)
        self.assertEqual(last.code, code)
        self.assertEqual(self.stored(), before)
