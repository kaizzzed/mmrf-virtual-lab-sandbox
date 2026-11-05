"""What an eval keeps from the runs it drove, and what it reports."""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .canonical import canonical_json
from .checks import Check, JsonObj, outcome, signature_of
from .client import AgentClient, RecordSource, TurnResult
from .oracle import Oracle
from .simstore import CreateOutcome, SimulatedCohortStore

EvalStatus = Literal["pass", "fail", "not-run"]


class AgentUnavailable(RuntimeError):
    """The agent did not answer the first turn: the eval cannot run."""


class ScenarioStop(Exception):
    """The run took a path the scenario cannot continue from; the check says why."""

    def __init__(self, check: Check, run: "RunEvidence | None" = None) -> None:
        super().__init__(check.detail)
        self.check = check
        self.run = run


def views_by_revision(turns: Sequence[TurnResult]) -> dict[str, JsonObj]:
    views: dict[str, JsonObj] = {}
    for turn in turns:
        for snap in turn.snapshots():
            run_id = str(snap.get("runId"))
            if run_id not in views or int(snap.get("revision", 0)) >= int(views[run_id].get("revision", 0)):
                views[run_id] = snap
    return views


@dataclass
class RunEvidence:
    """One run: every turn that touched it, its latest view, the save pause, its record."""

    label: str
    thread_id: str
    run_id: str
    question: str
    turns: list[TurnResult]
    views: dict[str, JsonObj]
    record: JsonObj | None = None
    record_location: str | None = None
    store: SimulatedCohortStore | None = None
    creates: list[CreateOutcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def view(self) -> JsonObj | None:
        return self.views.get(self.run_id)

    @property
    def run_turns(self) -> list[TurnResult]:
        return [t for t in self.turns if t.run_id == self.run_id]

    def snapshots(self) -> list[JsonObj]:
        return [s for t in self.turns for s in t.snapshots(self.run_id)]

    @property
    def pause_view(self) -> JsonObj | None:
        """The first snapshot of this run at the save pause (AwaitingConfirmation)."""
        return next((s for s in self.snapshots() if s.get("status") == "AwaitingConfirmation"), None)

    def pauses(self) -> list[JsonObj]:
        """Distinct save pauses, one per pending nonce."""
        seen: dict[str, JsonObj] = {}
        for snap in self.snapshots():
            pending = snap.get("pending")
            if snap.get("status") == "AwaitingConfirmation" and isinstance(pending, dict):
                seen.setdefault(str(pending.get("nonce")), snap)
        return list(seen.values())

    @property
    def signature(self) -> str | None:
        first_plan = next((s for s in self.snapshots() if s.get("plan")), None)
        return signature_of(first_plan)

    @classmethod
    def from_turns(cls, label: str, question: str, turns: Sequence[TurnResult], run_id: str | None = None, **extra: Any) -> "RunEvidence":
        first = turns[0]
        return cls(label, first.thread_id, run_id or first.run_id, question, list(turns), views_by_revision(turns), **extra)

    def to_json(self) -> JsonObj:
        view = self.view or {}
        return {
            "label": self.label,
            "threadId": self.thread_id,
            "runId": self.run_id,
            "question": self.question,
            "status": view.get("status"),
            "code": view.get("code"),
            "planVersion": (view.get("plan") or {}).get("version"),
            "signature": self.signature,
            "recordPath": self.record_location if self.record is not None else None,
            "recordAvailable": self.record is not None,
            "turns": [t.summary() for t in self.turns if t.run_id == self.run_id or t.thread_id == self.thread_id],
            "notes": self.notes,
        }


@dataclass
class AgentTarget:
    name: str
    client: AgentClient
    records: RecordSource


@dataclass
class EvalContext:
    """Everything a scenario needs; built by the runner from the command line."""

    agent: AgentTarget | None
    fault_agents: dict[str, AgentTarget]
    oracle: Oracle | None
    repeats: int = 5
    pause_wait_s: float = 300.0
    settle_timeout_s: float = 600.0
    lease_wait_s: float = 200.0
    restart_cmd: Sequence[str] | None = None
    frontend_root: Path | None = None
    agent_tests_root: Path | None = None
    prompts: JsonObj = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)
    log: Callable[[str], None] = lambda message: None
    planner: str | None = None

    def memo(self, key: str, compute: Callable[[], Any]) -> Any:
        if key not in self.cache:
            self.cache[key] = compute()
        return self.cache[key]

    def store(self) -> SimulatedCohortStore:
        counter = None
        if self.oracle is not None:
            oracle = self.oracle

            def counter_fn(definition: Any) -> int | None:
                if definition.get("kind") == "case_ids":
                    return len(set(definition["caseIds"]))
                clauses = definition.get("clauses", [])
                count: int = self.memo(f"count:{canonical_json(clauses)}", lambda: oracle.count(clauses))
                return count

            counter = counter_fn
        return SimulatedCohortStore(counter=counter)


@dataclass
class EvalResult:
    id: str
    case: str
    passes_when: str
    fr: list[JsonObj]
    status: EvalStatus
    checks: list[Check]
    runs: list[JsonObj]
    oracle: JsonObj | None
    notes: list[str]
    stretch: bool
    group: str
    started_at: float = field(default_factory=time.time)
    duration_s: float = 0.0

    @classmethod
    def build(
        cls,
        spec_id: str,
        case: str,
        passes_when: str,
        fr: list[JsonObj],
        checks: list[Check],
        runs: Sequence[RunEvidence],
        oracle: JsonObj | None,
        notes: list[str],
        stretch: bool,
        group: str,
    ) -> "EvalResult":
        return cls(spec_id, case, passes_when, fr, outcome(checks), checks, [r.to_json() for r in runs], oracle, notes, stretch, group)

    def to_json(self) -> JsonObj:
        return {
            "id": self.id,
            "case": self.case,
            "passesWhen": self.passes_when,
            "fr": self.fr,
            "group": self.group,
            "stretch": self.stretch,
            "status": self.status,
            "checks": [c.to_json() for c in self.checks],
            "runs": self.runs,
            "oracle": self.oracle,
            "notes": self.notes,
            "durationS": round(self.duration_s, 3),
        }
