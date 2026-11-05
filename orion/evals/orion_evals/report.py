"""Reports: one JSON document and one Markdown page per run of the harness, under `reports/`."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .checks import JsonObj
from .evidence import EvalResult

SCHEMA = "orion.eval-report/v1"
MARKS = {"pass": "pass", "fail": "FAIL", "not-run": "not run", "covered": "covered", "not-covered": "not covered"}


def stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def document(results: Sequence[EvalResult], gates: Sequence[JsonObj], environment: JsonObj, oracle: JsonObj | None, started: datetime, finished: datetime) -> JsonObj:
    counts = {s: sum(1 for r in results if r.status == s) for s in ("pass", "fail", "not-run")}
    return {
        "schema": SCHEMA,
        "startedAt": started.isoformat(timespec="seconds"),
        "finishedAt": finished.isoformat(timespec="seconds"),
        "environment": environment,
        "summary": counts,
        "gates": list(gates),
        "oracle": oracle,
        "evals": [r.to_json() for r in results],
    }


def _cell(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def markdown(doc: JsonObj) -> str:
    env = doc["environment"]
    lines = [
        "# ORION eval report",
        "",
        f"Run {doc['startedAt']} to {doc['finishedAt']}. Harness {env.get('harness')}. "
        f"{doc['summary']['pass']} pass, {doc['summary']['fail']} fail, {doc['summary']['not-run']} not run.",
        "",
        "Cohort saves and their read-back come from the **simulated browser** (an in-memory cohort store",
        "that follows the contract's Create steps); browser-only checks name the frontend test that covers them.",
        "",
        "## Environment",
        "",
        "| Item | Value |",
        "| --- | --- |",
    ]
    lines += [f"| {key} | {_cell(value)} |" for key, value in env.items()]
    lines += ["", "## Gates (R4 success metrics)", "", "| Gate | Target | Status | Measured |", "| --- | --- | --- | --- |"]
    lines += [f"| {g['gate']} | {_cell(g['target'])} | {MARKS.get(g['status'], g['status'])} | {_cell(g['measured'], 300)} |" for g in doc["gates"]]
    lines += ["", "## Evals", "", "| Eval | Case | Status | Checks | Runs |", "| --- | --- | --- | --- | --- |"]
    for e in doc["evals"]:
        ok = sum(1 for c in e["checks"] if c["status"] in ("pass", "covered"))
        label = e["id"] + (" (stretch)" if e["stretch"] else "")
        lines.append(f"| {label} | {_cell(e['case'], 90)} | {MARKS.get(e['status'], e['status'])} | {ok} of {len(e['checks'])} | {len(e['runs'])} |")
    if doc.get("oracle"):
        lines += ["", "## Oracle", "", "Values the oracle computed from the local stack with its own requests and arithmetic.", "", "```json", json.dumps(doc["oracle"], indent=1, ensure_ascii=False, default=str)[:20000], "```"]
    for e in doc["evals"]:
        lines += ["", f"## {e['id']}: {e['case']}", "", f"**Status: {MARKS.get(e['status'], e['status'])}.** Passes when: \"{e['passesWhen']}\""]
        for fr in e["fr"]:
            lines.append(f"- {fr['id']}: \"{fr['check']}\"")
        lines += ["", "| Check | Status | Source | Detail |", "| --- | --- | --- | --- |"]
        for c in e["checks"]:
            gate = "" if c.get("gate", True) else " (not gating)"
            lines.append(f"| {_cell(c['name'], 90)}{gate} | {MARKS.get(c['status'], c['status'])} | {c['source']} | {_cell(c['detail'], 260)} |")
        if e["runs"]:
            lines += ["", "| Run | Run id | Status | Signature | Record |", "| --- | --- | --- | --- | --- |"]
            for run in e["runs"]:
                sig = (run.get("signature") or "")[:12]
                lines.append(f"| {_cell(run['label'], 60)} | `{run['runId']}` | {run.get('status')} {run.get('code') or ''} | `{sig}` | {_cell(run.get('recordPath') or 'n/a', 80)} |")
        if e.get("oracle"):
            lines += ["", "Oracle values:", "", "```json", json.dumps(e["oracle"], indent=1, ensure_ascii=False, default=str)[:6000], "```"]
        for note in e["notes"]:
            lines += ["", "> " + note.replace("\n", "\n> ")]
    return "\n".join(lines) + "\n"


def write(doc: JsonObj, directory: Path, prefix: str = "orion-evals") -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}-{stamp(datetime.fromisoformat(doc['startedAt']))}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    json_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    md_path.write_text(markdown(doc), encoding="utf-8")
    return json_path, md_path


def oracle_markdown(doc: JsonObj) -> str:
    lines = [
        "# ORION oracle values",
        "",
        f"Computed {doc['computedAt']} from {doc['origin']} (credential used: {'yes' if doc['authenticated'] else 'no'}).",
        f"{doc['requests']} requests. Fingerprint `{doc['fingerprint']['sha256']}`.",
        "",
        "| Value | Oracle |",
        "| --- | --- |",
    ]
    for key, value in doc["highlights"].items():
        lines.append(f"| {key} | {_cell(value, 200)} |")
    lines += ["", "```json", json.dumps({k: v for k, v in doc.items() if k != "highlights"}, indent=1, ensure_ascii=False, default=str), "```"]
    return "\n".join(lines) + "\n"
