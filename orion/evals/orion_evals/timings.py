"""Stage 2 measurements: NFR-01 timings through the agent, and the model calls and tokens of a report's runs.

`python -m orion_evals timings` (RFA rev 25, NFR-01; Open questions 10 and 12) drives the agent like
the browser, with the harness's own client, simulated cohort store and Create, and measures per ask:

- `planS`: seconds from the POST to the first snapshot carrying the plan (the gate's firstPlanS);
- `interpretationWaitS`: the "Writing interpretation" wait, from the first snapshot showing Observed
  to the first carrying the verified Interpretation, or the run's end when it has none;
- from the run record: each planner and composer call's time, output tokens and cache reads and
  writes, and whether the answer was re-composed.

Modes: `warm` asks back to back after one warm-up turn; `cold` waits `--idle-s` (past the 5-minute
cache TTL) before each ask and sends no warm-up; `cold-warmup` waits, sends the warm-up action as the
Assistant does when it opens, then asks. Every ask calls the model roles the agent runs.

`python -m orion_evals usage <report.json>` sums the `modelCalls` of every run an eval report drove,
read from the run records: calls per role, repairs and re-composes, outcomes, models, efforts,
elapsed times and tokens. Warm-up calls make no record, and E11's fault agents call no model.
"""

import json
import math
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from . import wire
from .client import AgentClient, DirRecordSource, TurnResult
from .session import EvalThread
from .simstore import SimulatedCohortStore

JsonObj = dict[str, Any]
TOKEN_KEYS: Final = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
RECORD_WAIT_S: Final = 10.0
MODES: Final = ("warm", "cold", "cold-warmup")


def p95(values: Sequence[float]) -> float | None:
    """The gate's p95 (runner._p95): the ceil(0.95 n)-th smallest value."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def stats(values: Sequence[float]) -> JsonObj:
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50": round(statistics.median(values), 3), "p95": p95(values), "max": max(values)}


def interpretation_wait(turns: Sequence[TurnResult], run_id: str) -> float | None:
    """Seconds from the first run view with Observed to the first with Interpretation or a withheld
    reason (the view's own `observed`, `interpretation` and `withheldReason`), within the turn that
    showed Observed; that turn's last event when neither came."""
    for turn in turns:
        observed_at: float | None = None
        for event, at in zip(turn.events, turn.event_times, strict=True):
            if event["type"] != "STATE_SNAPSHOT" or event["snapshot"].get("runId") != run_id:
                continue
            view = event["snapshot"]
            if observed_at is None and view.get("observed"):
                observed_at = at
            if observed_at is not None and (view.get("interpretation") or view.get("withheldReason")):
                return at - observed_at
        if observed_at is not None and turn.event_times:
            return turn.event_times[-1] - observed_at
    return None


def _call(call: Mapping[str, Any]) -> JsonObj:
    keys = ("callId", "role", "model", "fallback", "effort", "outcome", "elapsedMs", *TOKEN_KEYS)
    return {key: call.get(key) for key in keys}


def _is_first(call: Mapping[str, Any]) -> bool:
    return str(call.get("callId", "")).endswith(".c1")


def ask_once(
    client: AgentClient, records: DirRecordSource, journey: str, question: str, *, warmup: bool, record_wait_s: float = RECORD_WAIT_S
) -> JsonObj:
    """One ask on a fresh thread (after a warm-up turn when `warmup`); J2 and J3 click Create at the pause."""
    thread = EvalThread(client, SimulatedCohortStore(), records, f"timing {journey}")
    warm: JsonObj | None = None
    if warmup:
        turn = thread.send(wire.new_id(), wire.warmup())
        warm = {"status": turn.status, "elapsedS": round(turn.elapsed_s, 3)}
    turn = thread.ask(question)
    run_id = turn.run_id
    view = thread.view(run_id)
    if journey in ("J2", "J3") and view is not None and view.get("status") == "AwaitingConfirmation":
        thread.confirm_create(run_id)
    final = thread.view(run_id) or {}
    deadline = time.monotonic() + record_wait_s
    record = thread.record(run_id)
    while (record is None or record.get("revision", 0) < final.get("revision", 0)) and time.monotonic() < deadline:
        time.sleep(0.5)
        record = thread.record(run_id)
    calls = [_call(c) for c in (record or {}).get("modelCalls") or [] if isinstance(c, dict)]
    plan_s = turn.first_plan_s()
    wait = interpretation_wait(thread.turns, run_id)
    return {
        "journey": journey,
        "question": question,
        "threadId": thread.thread_id,
        "runId": run_id,
        "status": final.get("status"),
        "code": final.get("code"),
        "warmup": warm,
        "planS": None if plan_s is None else round(plan_s, 3),
        "interpretationWaitS": None if wait is None else round(wait, 3),
        "recomposed": sum(1 for c in calls if c["role"] == "composer") > 1,
        "modelCalls": calls,
    }


def _role(calls: Sequence[Mapping[str, Any]], name: str) -> JsonObj:
    mine = [c for c in calls if c.get("role") == name]
    first = [c for c in mine if _is_first(c)]
    return {
        "calls": len(mine),
        "firstCalls": len(first),
        # After the first: the planner's IR repair or its plan after a clarification or an edit; the composer's re-compose
        # or its answer after an edit; SDK retries.
        "laterCalls": len(mine) - len(first),
        "outcomes": {o: sum(1 for c in mine if str(c.get("outcome")) == o) for o in sorted({str(c.get("outcome")) for c in mine})},
        "models": {m: sum(1 for c in mine if str(c.get("model")) == m) for m in sorted({str(c.get("model")) for c in mine})},
        "fallbackCalls": sum(1 for c in mine if c.get("fallback")),
        "efforts": sorted({str(c.get("effort")) for c in mine}),
        "elapsedS": stats([c["elapsedMs"] / 1000 for c in mine if isinstance(c.get("elapsedMs"), int)]),
        "firstCallElapsedS": stats([c["elapsedMs"] / 1000 for c in first if isinstance(c.get("elapsedMs"), int)]),
        "laterCallElapsedS": stats([c["elapsedMs"] / 1000 for c in mine if not _is_first(c) and isinstance(c.get("elapsedMs"), int)]),
        "outputTokensPerCall": stats([float(c["outputTokens"]) for c in mine if isinstance(c.get("outputTokens"), int)]),
        "tokens": {key: sum(int(c.get(key) or 0) for c in mine) for key in TOKEN_KEYS},
        "cacheWriteCalls": sum(1 for c in mine if int(c.get("cacheWriteTokens") or 0) > 0),
    }


def summarise(rows: Sequence[Mapping[str, Any]]) -> JsonObj:
    plans = [float(r["planS"]) for r in rows if r.get("planS") is not None]
    waits = [float(r["interpretationWaitS"]) for r in rows if r.get("interpretationWaitS") is not None]
    calls = [c for r in rows for c in r.get("modelCalls") or []]
    statuses = sorted({str(r.get("status")) for r in rows})
    return {
        "asks": len(rows),
        "statuses": {s: sum(1 for r in rows if str(r.get("status")) == s) for s in statuses},
        "planS": stats(plans),
        "planWithin5s": sum(1 for p in plans if p <= 5.0),
        "interpretationWaitS": stats(waits),
        "interpretationWaitByJourneyS": {
            j: stats([float(r["interpretationWaitS"]) for r in rows if r.get("journey") == j and r.get("interpretationWaitS") is not None])
            for j in sorted({str(r.get("journey")) for r in rows})
        },
        "recomposed": sum(1 for r in rows if r.get("recomposed")),
        "planner": _role(calls, "planner"),
        "composer": _role(calls, "composer"),
        "tokens": {key: sum(int(c.get(key) or 0) for c in calls) for key in TOKEN_KEYS},
    }


def run_timings(
    client: AgentClient,
    records: DirRecordSource,
    asks: Sequence[tuple[str, str]],
    *,
    mode: str,
    rounds: int,
    idle_s: float,
    log: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    record_wait_s: float = RECORD_WAIT_S,
) -> list[JsonObj]:
    if mode not in MODES:
        raise ValueError(f"mode is one of {MODES}, not {mode!r}")
    rows: list[JsonObj] = []
    if mode == "warm":
        EvalThread(client, SimulatedCohortStore(), records, "timing warm-up").send(wire.new_id(), wire.warmup())
    for round_no in range(rounds):
        for journey, question in asks:
            if mode != "warm":
                log(f"idle {idle_s:.0f} s before a cold {journey} ask")
                sleep(idle_s)
            row = ask_once(client, records, journey, question, warmup=mode == "cold-warmup", record_wait_s=record_wait_s)
            row["round"] = round_no + 1
            rows.append(row)
            planner: JsonObj = next((c for c in row["modelCalls"] if c["role"] == "planner"), {})
            log(
                f"{mode} {journey} round {round_no + 1}: {row['status']} plan {row['planS']} s (planner {planner.get('elapsedMs')} ms, "
                f"{planner.get('outputTokens')} tokens, cache read {planner.get('cacheReadTokens')} write {planner.get('cacheWriteTokens')}), "
                f"interpretation wait {row['interpretationWaitS']} s{', re-composed' if row['recomposed'] else ''}"
            )
    return rows


def _record(run_dir: Path, run_id: str) -> JsonObj | None:
    try:
        data = json.loads((run_dir / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def usage(report: Mapping[str, Any], run_dir: Path) -> JsonObj:
    """Model calls and tokens of every run an eval report drove (each run once: E13 and E16 reuse E1–E6's)."""
    runs: list[str] = []
    plan_times: dict[str, float] = {}
    for result in report.get("evals", []):
        for run in result.get("runs") or []:
            run_id = str(run.get("runId"))
            if run_id not in runs:
                runs.append(run_id)
            for turn in run.get("turns") or []:
                if turn.get("action") == "ask" and turn.get("firstPlanS") is not None and turn.get("runId"):
                    plan_times[str(turn["runId"])] = float(turn["firstPlanS"])
    calls: list[JsonObj] = []
    missing = with_calls = 0
    for run_id in runs:
        record = _record(run_dir, run_id)
        if record is None:
            missing += 1
            continue
        mine = [c for c in record.get("modelCalls") or [] if isinstance(c, dict)]
        with_calls += bool(mine)
        calls += mine
    return {
        "report": report.get("startedAt"),
        "runs": len(runs),
        "runsWithoutRecord": missing,
        "runsWithModelCalls": with_calls,
        "modelCalls": len(calls),
        "planner": _role(calls, "planner"),
        "composer": _role(calls, "composer"),
        "tokens": {key: sum(int(c.get(key) or 0) for c in calls) for key in TOKEN_KEYS},
        "planVisibleS": stats(list(plan_times.values())),
    }
