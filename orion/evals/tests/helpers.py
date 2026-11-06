"""Shared test helpers: the frozen contract fixtures as turns, evidence and a fake agent."""

import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orion_evals.client import TurnResult
from orion_evals.evidence import RunEvidence
from orion_evals.sse import parse_text

EVALS_ROOT = Path(__file__).resolve().parent.parent


def contracts_dir() -> Path:
    """The contract directory: $ORION_CONTRACTS_DIR, /contracts in a container, or the repo copy."""
    for candidate in (os.environ.get("ORION_CONTRACTS_DIR"), "/contracts", str(EVALS_ROOT.parent / "contracts")):
        if candidate and Path(candidate, "fixtures").is_dir():
            return Path(candidate)
    raise FileNotFoundError("the contract fixtures are not mounted (set ORION_CONTRACTS_DIR)")


def fixture_path(name: str) -> Path:
    return contracts_dir() / "fixtures" / name


def fixture_text(name: str) -> str:
    return fixture_path(name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


def fixture_events(name: str) -> list[dict[str, Any]]:
    return parse_text(fixture_text(name))


def fixture_turn(name: str, action: str = "ask", question: str | None = None) -> TurnResult:
    """A fixture stream as the harness would have read it, with plausible timings."""
    events = fixture_events(name)
    first = events[0]
    thread_id = str(first.get("threadId") or events[1]["snapshot"]["threadId"])
    run_id = str(first.get("runId") or events[-1].get("runId"))
    orion: dict[str, Any] = {"action": action}
    if question is not None:
        orion["question"] = question
    turn = TurnResult(action, {"threadId": thread_id, "runId": run_id, "forwardedProps": {"orion": orion}}, thread_id, run_id, time.time())
    turn.events = events
    turn.event_times = [0.1 * i for i in range(len(events))]
    turn.http_status = 200
    turn.content_type = "text/event-stream"
    return turn


def fixture_evidence(label: str, *names: tuple[str, str], record: Mapping[str, Any] | None = None, run_id: str | None = None) -> RunEvidence:
    """Evidence from fixture streams, each given as (file, action)."""
    turns = [fixture_turn(name, action) for name, action in names]
    question = ""
    for turn in turns:
        view = turn.last_view()
        if view:
            question = str(view.get("question", ""))
            break
    return RunEvidence.from_turns(label, question, turns, run_id=run_id, record=None if record is None else dict(record))


# --- a fake agent --------------------------------------------------------------------------------


@dataclass
class FakeResponse:
    status: int
    content_type: str
    body: str

    def lines(self) -> Iterator[str]:
        for line in self.body.splitlines(keepends=True):
            yield line

    def read_text(self) -> str:
        return self.body

    def set_timeout(self, seconds: float) -> None:
        return None

    def close(self) -> None:
        return None


Route = Callable[[Mapping[str, Any]], str | None]


@dataclass
class FixtureTransport:
    """Answers each POST with a fixture stream, its thread and run ids rewritten to the request's.

    `routes` maps an action (or `ask:<question>`) to a fixture file, or to a function of the body
    returning a fixture file or the stream text itself.
    Every body is kept, so a test can check what the harness sent.
    """

    routes: dict[str, str | Route]
    bodies: list[dict[str, Any]] = field(default_factory=list)
    # Route key -> a run-record fixture written (ids rewritten) to run_dir/runs/<runId>.json.
    records: dict[str, str] = field(default_factory=dict)
    run_dir: Path | None = None

    def post(self, body: Mapping[str, Any]) -> FakeResponse:
        self.bodies.append(json.loads(json.dumps(body)))
        orion = body["forwardedProps"]["orion"]
        key = f"ask:{orion.get('question')}" if orion["action"] == "ask" else str(orion["action"])
        route = self.routes.get(key, self.routes.get(str(orion["action"])))
        if route is None:
            return FakeResponse(400, "application/json", json.dumps({"error": {"code": "INVALID_INPUT", "message": f"no fixture for {key}"}}))
        name = route(body) if callable(route) else route
        if name is None:
            return FakeResponse(503, "application/json", json.dumps({"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "down"}}))
        text = name if name.startswith("data:") else fixture_text(name)
        if key in self.records and self.run_dir is not None:
            self.write_record(self.records[key], body["threadId"], body["runId"])
        return FakeResponse(200, "text/event-stream", rewrite_ids(text, body["threadId"], body["runId"]))

    def write_record(self, name: str, thread_id: str, run_id: str) -> None:
        assert self.run_dir is not None
        record = json.loads(fixture_text(name))
        text = fixture_text(name).replace(record["threadId"], thread_id).replace(record["runId"], run_id)
        (self.run_dir / "runs").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "runs" / f"{run_id}.json").write_text(text, encoding="utf-8")


def rewrite_ids(text: str, thread_id: str, run_id: str) -> str:
    """Replace the fixture's own thread id and its invocation's run id with the request's."""
    events = parse_text(text)
    old_thread = events[0].get("threadId") or events[1]["snapshot"]["threadId"]
    old_run = events[0].get("runId")
    text = text.replace(old_thread, thread_id)
    return text.replace(old_run, run_id) if old_run else text
