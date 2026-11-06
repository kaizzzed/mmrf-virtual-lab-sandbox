"""`python -m orion_evals run|oracle|list|prompts|timings|usage` (run it from orion/evals, in a
container on the Compose network; see the README)."""

import argparse
import ast
import json
import os
import shlex
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import report, timings
from .client import DEFAULT_AGENT_URL, AgentClient, BffRecordSource, DirRecordSource, HttpTransport, NoRecordSource, RecordSource
from .evals import EVALS
from .evidence import AgentTarget, EvalContext
from .oracle import DEFAULT_ORIGIN, Gateway, Oracle, OracleError, to_json
from .runner import environment, gates, run_evals, select

HERE = Path(__file__).resolve().parent.parent
PROMPTS = HERE / "prompts.json"
REPORTS = HERE / "reports"
PROBE_TIMEOUT_S = 10.0


def load_prompts(path: Path = PROMPTS) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "orion.eval-prompts/v1":
        raise ValueError(f"{path} is not an orion.eval-prompts/v1 file")
    return dict(data)


def all_prompts(prompts: dict[str, Any]) -> list[str]:
    """Every question an eval asks (C-31: none may be a planner few-shot example)."""
    out = [q for journey in prompts["reference"].values() for q in journey.values()]
    out += [q for qs in prompts["heldOut"].values() for q in qs]
    out += list(prompts["edge"].values()) + list(prompts["outOfKind"])
    out += [q for qs in prompts.get("edgeAlternates", {}).values() for q in qs]
    return out


def _normal(text: str) -> str:
    return " ".join(text.replace("–", "-").casefold().split())


def missing_from_agent(prompts: dict[str, Any], eval_prompts_file: Path) -> list[str]:
    """Harness prompts the agent's C-31 exclusion list does not hold, read with `ast` (never imported)."""
    tree = ast.parse(eval_prompts_file.read_text(encoding="utf-8"))
    known = {_normal(node.value) for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    return [q for q in all_prompts(prompts) if _normal(q) not in known]


def _records(kind: str, run_dir: str | None, bff_origin: str | None) -> RecordSource:
    if kind == "dir" and run_dir:
        return DirRecordSource(Path(run_dir))
    if kind == "bff" and bff_origin:
        return BffRecordSource(bff_origin)
    if kind == "auto":
        if run_dir and Path(run_dir, "runs").is_dir():
            return DirRecordSource(Path(run_dir))
        if bff_origin:
            return BffRecordSource(bff_origin)
        # A fresh run volume has no runs/ until the first run writes it: without a BFF it is the only source.
        if run_dir and Path(run_dir).is_dir():
            return DirRecordSource(Path(run_dir))
    return NoRecordSource()


def _fault_agent(spec: str, timeout_s: float) -> tuple[str, AgentTarget]:
    """`<target>:<kind>=<invocations url>[@<run dir>]`."""
    fault, _, rest = spec.partition("=")
    url, _, run_dir = rest.partition("@")
    if ":" not in fault or not url:
        raise ValueError(f"--fault-agent takes <target>:<kind>=<url>[@<run dir>], got {spec!r}")
    records: RecordSource = DirRecordSource(Path(run_dir)) if run_dir else NoRecordSource()
    return fault, AgentTarget(f"{fault} {url}", AgentClient(HttpTransport(url, "agent"), timeout_s), records)


def _oracle(args: argparse.Namespace) -> Oracle | None:
    access = Path(args.access_file) if args.access_file else None
    if not Gateway(args.oracle_origin, access, timeout_s=PROBE_TIMEOUT_S).reachable():
        return None
    return Oracle(Gateway(args.oracle_origin, access))


def _log(message: str) -> None:
    print(f"[orion-evals {datetime.now(UTC).strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def cmd_run(args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    prompts = load_prompts(Path(args.prompts))
    bff_origin = args.bff_origin
    if args.target == "bff":
        if not bff_origin:
            _log("--target bff needs --bff-origin (the fork's origin, such as http://frontend:3000)")
            return 2
        url = f"{bff_origin.rstrip('/')}/api/orion/agui"
    else:
        url = args.agent_url
    records = _records(args.record_source, args.run_dir, bff_origin)
    agent = AgentTarget(f"{args.target} {url}", AgentClient(HttpTransport(url, args.target), args.turn_timeout_s), records)
    faults = dict(_fault_agent(spec, args.turn_timeout_s) for spec in args.fault_agent)
    oracle = _oracle(args)
    if oracle is None:
        _log(f"the oracle cannot reach {args.oracle_origin}; oracle checks will be not-run")
    ctx = EvalContext(
        agent=agent,
        fault_agents=faults,
        oracle=oracle,
        repeats=args.repeats,
        pause_wait_s=args.pause_wait_s,
        settle_timeout_s=args.settle_timeout_s,
        lease_wait_s=args.lease_wait_s,
        restart_cmd=shlex.split(args.restart_cmd) if args.restart_cmd else None,
        frontend_root=Path(args.frontend_root) if args.frontend_root else None,
        agent_tests_root=Path(args.agent_tests_root) if args.agent_tests_root else None,
        e2e_script=Path(args.e2e_script) if args.e2e_script else None,
        prompts=prompts,
        log=_log,
        planner=args.planner,
    )
    specs = select(args.evals.split(",") if args.evals else None)
    fingerprint = None
    if oracle is not None:
        try:
            fingerprint = oracle.fingerprint()
        except OracleError as error:
            _log(f"oracle fingerprint failed: {error}")
    results, evidence = run_evals(specs, ctx)
    mode = "L (agent direct)" if args.target == "agent" else f"BFF {bff_origin}"
    doc = report.document(
        results,
        gates(results, evidence),
        environment(ctx, evidence, fingerprint, mode),
        {"fingerprint": fingerprint, "values": {k: to_json(v) for k, v in ctx.cache.items() if k.startswith("oracle:")}} if oracle else None,
        started,
        datetime.now(UTC),
    )
    json_path, md_path = report.write(doc, Path(args.reports_dir))
    _log(f"report: {json_path} and {md_path}")
    return 1 if any(r.status == "fail" for r in results) else 0


def cmd_oracle(args: argparse.Namespace) -> int:
    """Compute and record the oracle's values for the three journeys (no agent involved)."""
    prompts = load_prompts(Path(args.prompts))
    access = Path(args.access_file) if args.access_file else None
    if not Gateway(args.oracle_origin, access, timeout_s=PROBE_TIMEOUT_S).reachable():
        _log(f"the stack at {args.oracle_origin} is not reachable")
        return 2
    gateway = Gateway(args.oracle_origin, access)
    oracle = Oracle(gateway)
    inputs = prompts["oracleInputs"]
    race = prompts["cohorts"]["race"]["definition"]["clauses"]
    fingerprint = oracle.fingerprint()
    j1 = oracle.j1(inputs["J1"]["values"], inputs["J1"]["field"])
    j1_race = oracle.j1(inputs["J1"]["values"], inputs["J1"]["field"], race)
    j2 = oracle.j2(inputs["J2"]["clauses"], int(inputs["J2"]["n"]))
    j3 = oracle.j3(inputs["J3"]["gene"], [], int(inputs["J3"]["topN"]))
    e9 = oracle.j3(inputs["E9"]["gene"], inputs["E9"]["scope"], run_de=False)
    highlights = {
        "J1 groups (n, events)": [(g.label, g.cases, g.events) for g in j1.groups],
        "J1 log-rank p (oracle, engine)": [j1.p_value, j1.engine_p_value],
        "J1 in the race cohort (n, events), p": [[(g.label, g.cases, g.events) for g in j1_race.groups], j1_race.p_value],
        "J2 cohort size, denominator": [j2.cohort_size, j2.denominator],
        "J2 ranking (top n, ties)": [j2.shown, j2.ties],
        "J3 tested, mutated, not detected, outside": [j3.tested, j3.mutated_in_scope, j3.not_detected, j3.outside_tested],
        "J3 samples, eligible": [j3.samples, j3.eligible],
        "J3 KRAS term sample counts": j3.term_counts,
        "J3 top 10 (gene)": None if j3.de is None else [r["gene"] for r in j3.de["top"]],
        "E9 eligible (needs a group under 10)": e9.eligible,
    }
    doc = {
        "schema": "orion.oracle-values/v1",
        "computedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "origin": gateway.origin,
        "authenticated": gateway.authenticated,
        "requests": len(gateway.log),
        "fingerprint": fingerprint,
        "highlights": highlights,
        "j1": to_json(j1),
        "j1Race": to_json(j1_race),
        "j2": to_json(j2),
        "j3": to_json(j3),
        "e9": to_json(e9),
        "requestLog": [vars(r) for r in gateway.log],
    }
    directory = Path(args.reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"oracle-{report.stamp()}"
    (directory / f"{name}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    (directory / f"{name}.md").write_text(report.oracle_markdown(doc), encoding="utf-8")
    _log(f"oracle values: {directory / name}.json")
    for key, value in highlights.items():
        print(f"{key}: {json.dumps(value, default=str)}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    for spec in EVALS:
        print(f"{spec.id}{' (stretch)' if spec.stretch else ''} [{spec.group}] {spec.case}")
        print(f"    Passes when: {spec.passes_when}")
        print(f"    FR: {', '.join(spec.fr)}; needs: {', '.join(spec.needs)}")
    return 0


def cmd_prompts(args: argparse.Namespace) -> int:
    prompts = load_prompts(Path(args.prompts))
    if args.check_agent:
        missing = missing_from_agent(prompts, Path(args.check_agent))
        print(json.dumps({"file": args.check_agent, "missing": missing}, indent=1, ensure_ascii=False))
        return 1 if missing else 0
    print(json.dumps(all_prompts(prompts), indent=1, ensure_ascii=False))
    return 0


def cmd_timings(args: argparse.Namespace) -> int:
    """NFR-01 timings through the agent (orion_evals.timings); every ask calls the agent's model roles."""
    prompts = load_prompts(Path(args.prompts))
    asks: list[tuple[str, str]] = []
    for item in args.journeys.split(","):
        journey, _, phrasing = item.partition(":")
        question = prompts["reference"].get(journey, {}).get(phrasing)
        if not isinstance(question, str):
            _log(f"no reference prompt {item!r}: use journey:phrasing, such as J3:open-ended")
            return 2
        asks.append((journey, question))
    started = datetime.now(UTC)
    client = AgentClient(HttpTransport(args.agent_url, "agent"), args.turn_timeout_s)
    rows = timings.run_timings(
        client, DirRecordSource(Path(args.run_dir)), asks, mode=args.mode, rounds=args.rounds, idle_s=args.idle_s, log=_log
    )
    doc = {
        "schema": "orion.bedrock-timings/v1",
        "mode": args.mode,
        "idleS": None if args.mode == "warm" else args.idle_s,
        "startedAt": started.isoformat(timespec="seconds"),
        "finishedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": timings.summarise(rows),
        "asks": rows,
    }
    directory = Path(args.reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"timings-{args.mode}-{report.stamp()}.json"
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(doc["summary"], indent=1))
    _log(f"timings: {path}")
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    """Model calls and tokens of an eval report's runs, from their run records."""
    doc = json.loads(Path(args.report).read_text(encoding="utf-8"))
    print(json.dumps(timings.usage(doc, Path(args.run_dir)), indent=1))
    return 0


def parser() -> argparse.ArgumentParser:
    env = os.environ.get
    top = argparse.ArgumentParser(prog="python -m orion_evals", description=__doc__)
    top.add_argument("--prompts", default=str(PROMPTS))
    sub = top.add_subparsers(dest="command", required=True)

    def oracle_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--oracle-origin", default=env("ORION_VLAB_ORIGIN", DEFAULT_ORIGIN), help="identity gateway the oracle reads")
        p.add_argument("--access-file", default=env("ORION_EVAL_ACCESS_FILE"), help="hosted gateway only: the repo's access.json, read at run time, never printed")
        p.add_argument("--reports-dir", default=env("ORION_EVAL_REPORTS_DIR", str(REPORTS)))

    run = sub.add_parser("run", help="drive the agent through the selected evals and write a report")
    run.add_argument("--evals", default=env("ORION_EVAL_IDS"), help="comma-separated ids such as E1,E7, or all (default)")
    run.add_argument("--target", choices=("agent", "bff"), default=env("ORION_EVAL_TARGET", "agent"))
    run.add_argument("--agent-url", default=env("ORION_EVAL_AGENT_URL", DEFAULT_AGENT_URL))
    run.add_argument("--bff-origin", default=env("ORION_EVAL_BFF_ORIGIN"), help="the fork's origin, for --target bff or the record routes")
    run.add_argument("--record-source", choices=("auto", "dir", "bff", "none"), default=env("ORION_EVAL_RECORD_SOURCE", "auto"))
    run.add_argument("--run-dir", default=env("ORION_RUN_DIR", "/data/runs"), help="the agent's run volume, mounted read-only")
    run.add_argument("--fault-agent", action="append", default=[], help="<target>:<kind>=<invocations url>[@<run dir>], once per fault (E11)")
    run.add_argument("--repeats", type=int, default=int(env("ORION_EVAL_REPEATS", "5")))
    run.add_argument("--pause-wait-s", type=float, default=float(env("ORION_EVAL_PAUSE_WAIT_S", "300")), help="E20: time left at the save pause")
    run.add_argument("--settle-timeout-s", type=float, default=600.0)
    run.add_argument("--lease-wait-s", type=float, default=200.0, help="E20: wait after the restart for the 180 s lease to lapse")
    run.add_argument("--restart-cmd", default=env("ORION_EVAL_RESTART_CMD"), help="E20: an operator command that kills the agent's controller mid-run")
    run.add_argument("--turn-timeout-s", type=float, default=900.0)
    run.add_argument("--frontend-root", default=env("ORION_EVAL_FRONTEND_ROOT"), help="the fork's root, to find browser tests")
    run.add_argument("--agent-tests-root", default=env("ORION_EVAL_AGENT_TESTS_ROOT"), help="orion/agent/tests, to find lease tests")
    run.add_argument("--e2e-script", default=env("ORION_EVAL_E2E_SCRIPT"), help="orion/scripts/e2e-mode-l.mjs, to find browser E2E steps")
    run.add_argument("--planner", default=env("ORION_EVAL_PLANNER"), help="override the planner name read from the records")
    oracle_args(run)
    run.set_defaults(func=cmd_run)

    orc = sub.add_parser("oracle", help="compute and record the oracle's values only")
    oracle_args(orc)
    orc.set_defaults(func=cmd_oracle)

    sub.add_parser("list", help="list the evals").set_defaults(func=cmd_list)
    prm = sub.add_parser("prompts", help="print every eval prompt (C-31 exclusion list)")
    prm.add_argument("--check-agent", help="orion_agent/model/eval_prompts.py: list the harness prompts it lacks")
    prm.set_defaults(func=cmd_prompts)

    tim = sub.add_parser("timings", help="NFR-01: plan-visible time and the Writing interpretation wait through the agent")
    tim.add_argument("--agent-url", default=env("ORION_EVAL_AGENT_URL", DEFAULT_AGENT_URL))
    tim.add_argument("--run-dir", default=env("ORION_RUN_DIR", "/data/runs"), help="the agent's run volume, mounted read-only")
    tim.add_argument("--mode", choices=timings.MODES, default="warm")
    tim.add_argument("--rounds", type=int, default=4, help="rounds over --journeys (default 4)")
    tim.add_argument("--journeys", default="J1:open-ended,J1:tool-named,J2:open-ended,J2:tool-named,J3:open-ended,J3:tool-named",
                     help="reference prompts as journey:phrasing")
    tim.add_argument("--idle-s", type=float, default=330.0, help="cold modes: idle before each ask, past the 5-minute cache TTL")
    tim.add_argument("--turn-timeout-s", type=float, default=900.0)
    tim.add_argument("--reports-dir", default=env("ORION_EVAL_REPORTS_DIR", str(REPORTS)))
    tim.set_defaults(func=cmd_timings)

    use = sub.add_parser("usage", help="model calls and tokens of an eval report's runs, from the run records")
    use.add_argument("report", help="an orion-evals-*.json report")
    use.add_argument("--run-dir", default=env("ORION_RUN_DIR", "/data/runs"))
    use.set_defaults(func=cmd_usage)
    return top


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    code: int = args.func(args)
    return code
