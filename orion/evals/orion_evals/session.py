"""One conversation thread, driven like the browser drives it (contract "Actions").

`EvalThread` keeps what a browser keeps: the thread id, every run's latest view (by revision,
applied by `snapshot.runId`), the `save_cohorts` args of each pending nonce, and the simulated
cohort store. Its methods send one action each and return the turn. `during` runs a second action
while a turn streams (Stop during a step, an edit while J1's steps run).
"""

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import wire
from .client import CLOSE, AgentClient, EventHook, RecordSource, TurnResult, wait_settled
from .simstore import CreateOutcome, SimulatedCohortStore

Trigger = Callable[[dict[str, Any], TurnResult], bool]


def step_started(step_id: str) -> Trigger:
    def trigger(event: dict[str, Any], _turn: TurnResult) -> bool:
        return event["type"] == "STEP_STARTED" and event.get("stepName") == step_id

    return trigger


def tool_started(tool: str) -> Trigger:
    """The STEP_STARTED of the first step that runs `tool`, read from the latest snapshot's plan."""

    def trigger(event: dict[str, Any], turn: TurnResult) -> bool:
        if event["type"] != "STEP_STARTED":
            return False
        view = turn.last_view()
        steps = [] if view is None else view.get("steps", [])
        return any(s.get("id") == event.get("stepName") and s.get("tool") == tool for s in steps)

    return trigger


@dataclass
class ConcurrentTurn:
    """A second action fired from a running turn's stream.

    Without `close_first` it runs on its own thread while the first stream continues. With
    `close_first` the first stream is dropped and the action runs right after it closes, so only
    one call is ever in flight for the session.
    """

    action: Callable[[], TurnResult]
    close_first: bool
    fired: bool = False
    turn: TurnResult | None = None
    thread: threading.Thread | None = None

    def start(self) -> None:
        def run() -> None:
            self.turn = self.action()

        self.thread = threading.Thread(target=run, name="orion-eval-concurrent", daemon=True)
        self.thread.start()

    def finish(self, timeout_s: float) -> TurnResult | None:
        """Call after the first turn returned: run a deferred action, or wait for the concurrent one."""
        if not self.fired:
            return None
        if self.close_first and self.turn is None:
            self.turn = self.action()
        elif self.thread is not None:
            self.thread.join(timeout_s)
        return self.turn


@dataclass
class EvalThread:
    client: AgentClient
    store: SimulatedCohortStore
    records: RecordSource
    label: str = ""
    thread_id: str = field(default_factory=wire.new_id)
    turns: list[TurnResult] = field(default_factory=list)
    views: dict[str, dict[str, Any]] = field(default_factory=dict)
    save_args: dict[str, dict[str, Any]] = field(default_factory=dict)
    creates: list[CreateOutcome] = field(default_factory=list)
    run_order: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- bookkeeping ---------------------------------------------------------------------------

    def _absorb(self, turn: TurnResult) -> None:
        with self._lock:
            self.turns.append(turn)
            for snapshot in turn.snapshots():
                run_id = str(snapshot.get("runId"))
                current = self.views.get(run_id)
                if current is None or int(snapshot.get("revision", 0)) >= int(current.get("revision", 0)):
                    self.views[run_id] = snapshot
                if run_id not in self.run_order:
                    self.run_order.append(run_id)
            args = turn.tool_call_args()
            if args is not None and isinstance(args.get("nonce"), str):
                self.save_args[args["nonce"]] = args

    def send(
        self,
        run_id: str,
        orion: Mapping[str, Any],
        *,
        question: str | None = None,
        on_event: EventHook | None = None,
    ) -> TurnResult:
        body = wire.run_agent_input(self.thread_id, run_id, orion, question=question)
        turn = self.client.turn(body, on_event)
        self._absorb(turn)
        return turn

    def view(self, run_id: str) -> dict[str, Any] | None:
        return self.views.get(run_id)

    def record(self, run_id: str) -> dict[str, Any] | None:
        return self.records.record(run_id, self.thread_id)

    def settle(self, run_id: str, timeout_s: float) -> dict[str, Any] | None:
        """For a turn the harness dropped: poll the view route until the run rests."""
        view = wait_settled(self.records, run_id, self.thread_id, timeout_s)
        if view is not None:
            with self._lock:
                current = self.views.get(run_id)
                if current is None or int(view.get("revision", 0)) >= int(current.get("revision", 0)):
                    self.views[run_id] = view
        return view

    # --- actions -------------------------------------------------------------------------------

    def ask(
        self,
        question: str,
        cohort_context: Mapping[str, Any] | None = None,
        *,
        cancel_waiting: str | None = None,
        on_event: EventHook | None = None,
        run_id: str | None = None,
    ) -> TurnResult:
        context = self.store.cohort_context() if cohort_context is None else cohort_context
        payload = wire.ask(question, context, cancel_waiting=cancel_waiting)
        return self.send(run_id or wire.new_id(), payload, question=question, on_event=on_event)

    def cancel(self, run_id: str, on_event: EventHook | None = None) -> TurnResult:
        return self.send(run_id, wire.cancel(), on_event=on_event)

    def clarify(self, run_id: str, option_id: str) -> TurnResult:
        return self.send(run_id, wire.clarify_answer(option_id))

    def consent(self, run_id: str, accept: bool) -> TurnResult:
        return self.send(run_id, wire.consent(accept))

    def edit(
        self,
        run_id: str,
        field_name: str,
        value: Any,
        *,
        ref: str | None = None,
        cohort_context: Mapping[str, Any] | None = None,
        on_event: EventHook | None = None,
    ) -> TurnResult:
        return self.send(run_id, wire.edit_plan(field_name, value, ref=ref, cohort_context=cohort_context), on_event=on_event)

    def retry(self, run_id: str, step_id: str) -> TurnResult:
        return self.send(run_id, wire.retry_step(step_id))

    def pending(self, run_id: str) -> dict[str, Any] | None:
        view = self.views.get(run_id)
        pending = None if view is None else view.get("pending")
        return pending if isinstance(pending, dict) else None

    def pending_args(self, run_id: str) -> dict[str, Any] | None:
        """The pending confirm with every definition: the TOOL_CALL_ARGS, else the record, else the view."""
        pending = self.pending(run_id)
        if pending is None or pending.get("kind") != "confirm":
            return None
        nonce = pending["nonce"]
        if nonce in self.save_args:
            return self.save_args[nonce]
        record = self.record(run_id)
        stored = None if record is None else record.get("pending")
        if isinstance(stored, dict) and stored.get("nonce") == nonce:
            return stored
        if all(c.get("definition") is not None for c in pending.get("cohorts", [])):
            return pending
        return None

    def create(self, run_id: str, make_active: str | None = None, nonce: str | None = None) -> CreateOutcome:
        """The Create click, inside the click handler: save (simulated browser) and read back.

        `nonce` resends Create for a card the browser still shows after its confirm was lost
        (E19): the run may have moved on, but the card's args and the save ledger are the nonce's.
        """
        args = self.save_args.get(nonce) if nonce is not None else self.pending_args(run_id)
        if args is None:
            raise RuntimeError(f"run {run_id} has no pending confirm with its definitions")
        outcome = self.store.create(args, make_active)
        self.creates.append(outcome)
        return outcome

    def send_confirm(self, run_id: str, outcome: CreateOutcome, on_event: EventHook | None = None) -> TurnResult:
        return self.send(run_id, wire.confirm(outcome.nonce, "create", outcome.saved, outcome.make_active), on_event=on_event)

    def confirm_create(
        self,
        run_id: str,
        make_active: str | None = None,
        on_event: EventHook | None = None,
        nonce: str | None = None,
    ) -> TurnResult:
        return self.send_confirm(run_id, self.create(run_id, make_active, nonce), on_event)

    def confirm_dont_save(self, run_id: str) -> TurnResult:
        pending = self.pending(run_id)
        if pending is None or pending.get("kind") != "confirm":
            raise RuntimeError(f"run {run_id} is not waiting for a confirmation")
        return self.send(run_id, wire.confirm(pending["nonce"], "dont_save", [], None))

    # --- a second action while a turn streams ----------------------------------------------------

    def during(
        self,
        trigger: Trigger,
        action: Callable[[], TurnResult],
        *,
        close_first: bool = False,
    ) -> tuple[EventHook, ConcurrentTurn]:
        """An event hook that fires `action` once when `trigger` matches.

        With `close_first` the running stream is dropped and the action is sent after it closes,
        as the BFF aborts the open upstream call before it forwards a cancel, edit or confirm
        (contract "Stop, edit and confirm"). Call `state.finish()` after the first turn returns.
        """
        state = ConcurrentTurn(action, close_first)

        def hook(event: dict[str, Any], turn: TurnResult) -> str | None:
            if state.fired or not trigger(event, turn):
                return None
            state.fired = True
            if close_first:
                return CLOSE
            state.start()
            return None

        return hook, state


def close_on(trigger: Trigger) -> EventHook:
    """Drop the stream when `trigger` matches (a closed tab)."""

    def hook(event: dict[str, Any], turn: TurnResult) -> str | None:
        return CLOSE if trigger(event, turn) else None

    return hook
