"""Run the selected evals, then score the success-metric gates (R4 "Success metrics")."""

import math
import time
import traceback
from collections.abc import Sequence
from typing import Any

from . import HARNESS_VERSION
from .checks import Check, JsonObj, counted_set_visible, expect, not_run, numbers_cited, writes_confirmed
from .evals import BY_ID, EDGE_GATE, EvalSpec
from .evidence import AgentUnavailable, EvalContext, EvalResult, RunEvidence, ScenarioStop
from .oracle import OracleError
from .simstore import LABEL as SIMULATED

PLAN_P95_TARGET_S = 5.0
PLAN_MIN_RUNS = 20


def select(ids: Sequence[str] | None) -> list[EvalSpec]:
    if not ids or list(ids) == ["all"]:
        return list(BY_ID.values())
    unknown = [i for i in ids if i not in BY_ID]
    if unknown:
        raise ValueError(f"unknown evals {unknown}; known: {', '.join(BY_ID)}")
    return [BY_ID[i] for i in ids]


def _planner_from(runs: Sequence[RunEvidence]) -> str | None:
    for ev in runs:
        env = (ev.record or {}).get("environment") or {}
        if env.get("planner"):
            return str(env["planner"])
    return None


def run_one(spec: EvalSpec, ctx: EvalContext) -> tuple[EvalResult, list[RunEvidence]]:
    started = time.monotonic()
    runs: list[RunEvidence] = []
    notes: list[str] = []
    oracle: JsonObj | None = None
    try:
        checks, runs, oracle, notes = spec.run(ctx)
    except AgentUnavailable as error:
        checks = [not_run("the agent answers", f"agent unavailable: {error}", source="harness")]
    except ScenarioStop as stop:
        checks = [stop.check]
        runs = [stop.run] if stop.run is not None else []
    except OracleError as error:
        checks = [not_run("the oracle answers", str(error), source="oracle")]
    except Exception as error:  # noqa: BLE001 - one eval's crash must not stop the others
        checks = [expect("the harness could follow the run", False, f"{type(error).__name__}: {error}", source="harness")]
        notes = [traceback.format_exc(limit=8)]
    result = EvalResult.build(spec.id, spec.case, spec.passes_when, spec.fr_json(), checks, runs, oracle, notes, spec.stretch, spec.group)
    result.duration_s = time.monotonic() - started
    return result, runs


def run_evals(specs: Sequence[EvalSpec], ctx: EvalContext) -> tuple[list[EvalResult], list[RunEvidence]]:
    results: list[EvalResult] = []
    evidence: dict[str, RunEvidence] = {}
    for spec in specs:
        if spec.id == "E22" and ctx.planner is None:
            ctx.planner = _planner_from(list(evidence.values()))
        ctx.log(f"{spec.id}: {spec.case}")
        result, runs = run_one(spec, ctx)
        results.append(result)
        for ev in runs:
            evidence[ev.run_id] = ev
        if ctx.planner is None:
            ctx.planner = _planner_from(runs)
        ctx.log(f"{spec.id}: {result.status}")
    return results, list(evidence.values())


# --- gates ----------------------------------------------------------------------------------------


def _p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def _gate(name: str, target: str, status: str, measured: str) -> JsonObj:
    return {"gate": name, "target": target, "status": status, "measured": measured}


def _status_of(results: dict[str, EvalResult], ids: Sequence[str]) -> str:
    present = [results[i] for i in ids if i in results]
    if len(present) < len(ids):
        return "not-run"
    if any(r.status == "fail" for r in present):
        return "fail"
    return "not-run" if any(r.status == "not-run" for r in present) else "pass"


def _share(checks: Sequence[Check]) -> tuple[str, str]:
    ran = [c for c in checks if c.status in ("pass", "fail")]
    passed = [c for c in ran if c.status == "pass"]
    status = "not-run" if not ran else ("pass" if len(passed) == len(ran) else "fail")
    return status, f"{len(passed)} of {len(ran)} runs" + ("" if ran else " (no record or answer to check)")


def gates(results: Sequence[EvalResult], evidence: Sequence[RunEvidence]) -> list[JsonObj]:
    by_id = {r.id: r for r in results}
    reference = [ev for ev in evidence if ev.label.startswith(("J1 ", "J2 ", "J3 "))]
    out: list[JsonObj] = []
    e16 = by_id.get("E16")
    if e16 is None:
        out.append(_gate("Reference prompts completed", "6 of 6", "not-run", "E16 not run"))
    else:
        complete = [c for c in e16.checks if c.status == "pass"]
        out.append(_gate("Reference prompts completed", "6 of 6", e16.status, f"{len(complete)} of {len(e16.checks)} prompts Complete in every run"))
    out.append(_gate("Phrasing parity", "identical signatures in 5 of 5 runs per prompt and per pair, plus the held-out paraphrases", _status_of(by_id, ["E1", "E2", "E3", "E4", "E5", "E6", "E22"]), "E1-E6 signature checks and E22"))
    out.append(_gate("Interpretation present", "every reference run Complete with Interpretation, 5 of 5 per prompt", "not-run" if e16 is None else e16.status, "E16"))
    edge_items: list[tuple[str, str]] = []
    for eid in EDGE_GATE:
        result = by_id.get(eid)
        if eid != "E11":
            edge_items.append((eid, "not-run" if result is None else result.status))
            continue
        for case in ("timeout", "refusal", "empty count"):
            if result is None:
                edge_items.append((f"E11 {case}", "not-run"))
                continue
            if case == "refusal":
                names = ("planner refusal", "composer refusal")
                found = [c for c in result.checks if c.name.startswith(names) and c.gate]
            else:
                found = [c for c in result.checks if c.name.startswith(case) and c.gate]
            status = "not-run" if not found or all(c.status == "not-run" for c in found) else ("fail" if any(c.status == "fail" for c in found) else "pass")
            if case == "refusal" and found:
                by_case = {n: [c for c in found if c.name.startswith(n)] for n in names}
                passing = [n for n, cs in by_case.items() if cs and all(c.ok for c in cs)]
                status = "pass" if passing else status
            edge_items.append((f"E11 {case}", status))
    passed = [name for name, s in edge_items if s == "pass"]
    edge_status = "pass" if len(passed) == 9 else ("fail" if any(s == "fail" for _, s in edge_items) else "not-run")
    out.append(_gate("Honest limits", "9 of 9 edge cases", edge_status, f"{len(passed)} of 9: " + ", ".join(f"{n} {s}" for n, s in edge_items)))
    status, measured = _share([counted_set_visible(ev.view, ev.record) for ev in reference if ev.view])
    out.append(_gate("Visible tool calls", "100% of the counted set", status, measured))
    status, measured = _share([numbers_cited(ev.view, ev.question) for ev in evidence if ev.view and ev.view.get("observed")])
    out.append(_gate("Cited numbers", "100% of numbers in the answer", status, measured))
    status, measured = _share([writes_confirmed(ev.record) for ev in evidence if ev.record is not None])
    out.append(_gate("Zero writes without confirmation", "0", status, measured + f"; cohort writes made by the {SIMULATED} only inside Create"))
    plan_times = [t.first_plan_s() for ev in evidence for t in ev.run_turns if t.action == "ask"]
    times = [t for t in plan_times if t is not None]
    p95 = _p95(times)
    if len(times) < PLAN_MIN_RUNS:
        status = "not-run"
    else:
        status = "pass" if p95 is not None and p95 <= PLAN_P95_TARGET_S else "fail"
    # Whether each thread was warmed before its first ask (--warmup-threads) changes what the time holds in
    # mode A (a new thread is a new Runtime session), so the gate says so.
    warmed = any(t.action == "warmup" for ev in evidence for t in ev.turns)
    condition = "; each thread warmed when it opened, as the browser does" if warmed else "; no thread warm-up"
    out.append(_gate("Plan visible p95 (Proposal)", f"<= {PLAN_P95_TARGET_S:.0f} s over at least {PLAN_MIN_RUNS} runs", status, f"p95 {'n/a' if p95 is None else f'{p95:.2f} s'} over {len(times)} asks{condition}"))
    out.append(_gate("Count accuracy", "J3 group sizes equal a direct API computation", _status_of(by_id, ["E5", "E6"]), "E5, E6 group-size checks"))
    return out


def environment(ctx: EvalContext, evidence: Sequence[RunEvidence], fingerprint: JsonObj | None, mode: str) -> JsonObj:
    records = [ev.record for ev in evidence if ev.record is not None]
    env: dict[str, Any] = {}
    for record in records:
        env = record.get("environment") or {}
        if env:
            break
    # The record names its composer role (contract v1.2 environment.composer, stub or bedrock); the models that
    # served it come from modelCalls. Older records without the field fall back to the served models.
    roles = sorted({str(r["environment"]["composer"]) for r in records if (r.get("environment") or {}).get("composer")})
    composers = sorted({str(m.get("model")) for r in records for m in r.get("modelCalls", []) if m.get("role") == "composer"})
    planners = sorted({str(m.get("model")) for r in records for m in r.get("modelCalls", []) if m.get("role") == "planner"})
    agent_fps = sorted({str((ev.view or {}).get("environment", {}).get("fingerprintSha256")) for ev in evidence if ev.view and (ev.view.get("environment") or {}).get("fingerprintSha256")})
    return {
        "harness": HARNESS_VERSION,
        "mode": mode,
        "agent": None if ctx.agent is None else ctx.agent.name,
        "recordSource": None if ctx.agent is None else ctx.agent.records.name,
        "faultAgents": sorted(ctx.fault_agents),
        "planner": ctx.planner or env.get("planner"),
        "plannerModels": planners,
        "composer": roles[0] if len(roles) == 1 else (roles or (composers[0] if len(composers) == 1 else (composers or "none recorded"))),
        "composerModels": composers,
        "executor": env.get("executor"),
        "agentVersion": env.get("agentVersion"),
        "toolTransport": env.get("toolTransport"),
        "faults": env.get("faults"),
        "agentFingerprint": agent_fps,
        "oracleFingerprint": None if fingerprint is None else fingerprint.get("sha256"),
        "fingerprintMatch": None if fingerprint is None or not agent_fps else agent_fps == [fingerprint.get("sha256")],
        "oracleOrigin": None if ctx.oracle is None else ctx.oracle.gw.origin,
        "oracleAuthenticated": None if ctx.oracle is None else ctx.oracle.gw.authenticated,
        "cohortStore": f"{SIMULATED} (in memory, defSha256 per the shared vectors)",
        "repeats": ctx.repeats,
        "threadWarmup": ctx.warmup_threads,
        "frontendRoot": None if ctx.frontend_root is None else str(ctx.frontend_root),
    }
