"""Drivers: each takes the agent through one path of the contract and returns the run's evidence."""

import subprocess
import time
from collections.abc import Mapping
from typing import Any

from . import wire
from .canonical import filterset_from_definition
from .checks import expect
from .client import TurnResult
from .evidence import AgentTarget, AgentUnavailable, EvalContext, RunEvidence, ScenarioStop
from .oracle import J1Expected, J2Expected, J3Expected
from .session import EvalThread, close_on, tool_started
from .simstore import SimulatedCohortStore

RECORD_RETRY_S = 3.0


def target(ctx: EvalContext, fault: str | None = None) -> AgentTarget:
    if fault is None:
        if ctx.agent is None:
            raise AgentUnavailable("no agent URL")
        return ctx.agent
    found = ctx.fault_agents.get(fault)
    if found is None:
        raise AgentUnavailable(f"no agent started with ORION_FAULTS={fault} (--fault-agent {fault}=<url>)")
    return found


def new_thread(ctx: EvalContext, label: str, store: SimulatedCohortStore | None = None, fault: str | None = None) -> EvalThread:
    agent = target(ctx, fault)
    thread = EvalThread(agent.client, store or ctx.store(), agent.records, label)
    if ctx.warmup_threads:
        # As the browser does when a thread opens (AssistantApp: controller.warmup(threadId)). In mode A the
        # warm-up starts the thread's Runtime session, so its first ask is not the one that waits for a new
        # microVM. A failed warm-up only makes the first answer slower: its result is not checked.
        thread.send(wire.new_id(), wire.warmup())
    return thread


def require(turn: TurnResult) -> TurnResult:
    """The first turn of a scenario must reach the agent; a refused connection means not-run."""
    if turn.http_status is None and turn.transport_error:
        raise AgentUnavailable(turn.transport_error)
    return turn


def evidence(thread: EvalThread, run_id: str, label: str, question: str) -> RunEvidence:
    """The run's evidence; the record is read after its last transition (one short retry)."""
    record = thread.record(run_id)
    view = thread.view(run_id)
    if record is not None and view is not None and record.get("revision", 0) < view.get("revision", 0):
        time.sleep(RECORD_RETRY_S)
        record = thread.record(run_id)
    return RunEvidence(
        label,
        thread.thread_id,
        run_id,
        question,
        list(thread.turns),
        dict(thread.views),
        record,
        thread.records.location(run_id),
        thread.store,
        list(thread.creates),
    )


def expect_status(thread: EvalThread, run_id: str, statuses: set[str], label: str, question: str) -> None:
    view = thread.view(run_id)
    if view is None or view.get("status") not in statuses:
        ev = evidence(thread, run_id, label, question)
        check = expect(f"reaches {'/'.join(sorted(statuses))}", False, f"status {None if view is None else view.get('status')} code {None if view is None else view.get('code')}")
        raise ScenarioStop(check, ev)


# --- journeys --------------------------------------------------------------------------------------


def reference_prompt(ctx: EvalContext, journey: str, phrasing: str) -> str:
    return str(ctx.prompts["reference"][journey][phrasing])


def run_journey(ctx: EvalContext, journey: str, question: str, label: str, store: SimulatedCohortStore | None = None, make_active: str | None = None) -> RunEvidence:
    """Ask; for J2 and J3 click Create at the save pause (simulated browser); run to the end."""
    thread = new_thread(ctx, label, store)
    turn = require(thread.ask(question))
    run_id = turn.run_id
    view = thread.view(run_id)
    if journey in ("J2", "J3") and view is not None and view.get("status") == "AwaitingConfirmation":
        thread.confirm_create(run_id, make_active)
    ctx.log(f"{label}: {(thread.view(run_id) or {}).get('status')}")
    return evidence(thread, run_id, label, question)


def reference_runs(ctx: EvalContext, journey: str, phrasing: str) -> list[RunEvidence]:
    """Each reference prompt runs `repeats` times on fresh threads and stores (E16), memoised."""

    def compute() -> list[RunEvidence]:
        question = reference_prompt(ctx, journey, phrasing)
        return [run_journey(ctx, journey, question, f"{journey} {phrasing} run {i + 1}") for i in range(ctx.repeats)]

    runs: list[RunEvidence] = ctx.memo(f"runs:{journey}:{phrasing}", compute)
    return runs


def oracle_j1(ctx: EvalContext, scope: list[dict[str, Any]] | None = None) -> J1Expected | None:
    if ctx.oracle is None:
        return None
    inputs = ctx.prompts["oracleInputs"]["J1"]
    oracle = ctx.oracle
    key = f"oracle:j1:{scope}"
    result: J1Expected = ctx.memo(key, lambda: oracle.j1(inputs["values"], inputs["field"], scope or []))
    return result


def oracle_j2(ctx: EvalContext) -> J2Expected | None:
    if ctx.oracle is None:
        return None
    inputs = ctx.prompts["oracleInputs"]["J2"]
    oracle = ctx.oracle
    result: J2Expected = ctx.memo("oracle:j2", lambda: oracle.j2(inputs["clauses"], int(inputs["n"])))
    return result


def oracle_j3(ctx: EvalContext, top_n: int | None = None, gene: str | None = None, scope: list[dict[str, Any]] | None = None, run_de: bool = True) -> J3Expected | None:
    if ctx.oracle is None:
        return None
    inputs = ctx.prompts["oracleInputs"]["J3"]
    oracle = ctx.oracle
    n = int(top_n or inputs["topN"])
    g = gene or str(inputs["gene"])
    result: J3Expected = ctx.memo(f"oracle:j3:{g}:{scope}:{n}:{run_de}", lambda: oracle.j3(g, scope or [], n, run_de=run_de))
    return result


# --- shared paths ----------------------------------------------------------------------------------


def seed_cohort(ctx: EvalContext, store: SimulatedCohortStore, key: str, active: bool) -> tuple[str, int | None]:
    """A saved cohort the researcher made earlier, from prompts.json; optionally made active."""
    spec = ctx.prompts["cohorts"][key]
    definition = spec["definition"]
    count = None if ctx.oracle is None else ctx.oracle.count(definition["clauses"])
    cohort = store.seed(spec["name"], filterset_from_definition(definition), count)
    if active:
        store.set_active(cohort.id, origin="harness setup")
    return str(spec["name"]), count


def to_pause(ctx: EvalContext, label: str, question: str, store: SimulatedCohortStore | None = None, fault: str | None = None) -> tuple[EvalThread, str]:
    """Ask J3 (or any saving plan) and stop at the save pause."""
    thread = new_thread(ctx, label, store, fault)
    turn = require(thread.ask(question))
    expect_status(thread, turn.run_id, {"AwaitingConfirmation"}, label, question)
    return thread, turn.run_id


def drop_confirm_after_save(thread: EvalThread, run_id: str, send: bool) -> str:
    """E19: the browser saved both cohorts but its confirm was lost.

    `send=False`: the request never left the browser. `send=True`: it reached the agent and the
    response was dropped right after RUN_STARTED (the agent's run goes on detached).
    """
    outcome = thread.create(run_id)
    if send:
        thread.send_confirm(run_id, outcome, on_event=close_on(lambda e, _t: e["type"] == "RUN_STARTED"))
    return outcome.nonce


def close_during_de(thread: EvalThread, run_id: str) -> TurnResult:
    """E20: Create, then the tab closes while de_run runs."""
    return thread.confirm_create(run_id, on_event=close_on(tool_started("de_run")))


def restart_agent(ctx: EvalContext) -> bool:
    """Run the operator's restart command (E20 lease expiry), if one was given."""
    if not ctx.restart_cmd:
        return False
    subprocess.run(list(ctx.restart_cmd), check=True, timeout=120)
    return True


def first_pending_ref(thread: EvalThread, run_id: str) -> str:
    pending = thread.pending(run_id) or {}
    cohorts = pending.get("cohorts") or [{}]
    return str(cohorts[0].get("ref"))


def scope_value(name: str) -> Mapping[str, Any]:
    return {"source": "saved_cohort", "cohortName": name}
