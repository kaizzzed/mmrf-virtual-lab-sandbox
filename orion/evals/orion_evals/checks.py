"""Check primitives over what the agent sent: turns (streams), views (snapshots) and run records.

Each returns a `Check`. Statuses: `pass`, `fail`, `not-run` (a prerequisite is missing), and for
browser-only checks `covered` (a frontend test exists) or `not-covered`.
"""

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from .canonical import pinned_hash
from .client import TurnResult
from .factformat import format_fact
from .sse import FRAME_LIMIT_BYTES
from .wire import DATA_STEP_STATUSES

Status = Literal["pass", "fail", "not-run", "covered", "not-covered"]
Source = Literal["agent", "record", "oracle", "browser", "simulated browser", "harness"]
JsonObj = dict[str, Any]

# Contract "Numbers on screen": digits allowed outside a fact segment.
CONTRACT_ALLOWED_TOKENS: Final = frozenset({"Gen3", "log2FC", "log2", "six-gene", "TP53", "DIS3"})
VIEWER_FOOTNOTE: Final = "p = 0"
# Code constants a limitation line may state (guards and caps from TOOLS.md 7.2), never tool output.
LIMITATION_CONSTANTS: Final = frozenset({"10", "4,000", "10,000", "8"})
DIGIT_TOKEN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.,+\-]*")


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    evidence: JsonObj = field(default_factory=dict)
    source: Source = "agent"
    # A non-gating check is reported but does not decide the eval (extra evidence beyond its row).
    gate: bool = True

    @property
    def ok(self) -> bool:
        return self.status in ("pass", "covered")

    def to_json(self) -> JsonObj:
        return {
            "name": self.name,
            "status": self.status,
            "source": self.source,
            "gate": self.gate,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def expect(name: str, ok: bool, detail: str, *, source: Source = "agent", gate: bool = True, **evidence: Any) -> Check:
    return Check(name, "pass" if ok else "fail", detail, dict(evidence), source, gate)


def not_run(name: str, detail: str, *, source: Source = "agent", gate: bool = True, **evidence: Any) -> Check:
    return Check(name, "not-run", detail, dict(evidence), source, gate)


def outcome(all_checks: Sequence[Check]) -> Literal["pass", "fail", "not-run"]:
    """Over the gating checks: fail if any failed; else not-run if any is not-run or not-covered; else pass."""
    checks = [c for c in all_checks if c.gate]
    if any(c.status == "fail" for c in checks):
        return "fail"
    if not checks or any(c.status in ("not-run", "not-covered") for c in checks):
        return "not-run"
    return "pass"


# --- View helpers ---------------------------------------------------------------------------


def facts_by_id(view: Mapping[str, Any]) -> dict[str, JsonObj]:
    return {str(f["id"]): f for f in view.get("facts", [])}


def step_by_id(view: Mapping[str, Any]) -> dict[str, JsonObj]:
    return {str(s["id"]): s for s in view.get("steps", [])}


def steps_with_tool(view: Mapping[str, Any], tool: str) -> list[JsonObj]:
    return [s for s in view.get("steps", []) if s.get("tool") == tool]


def sentence_text(sentence: Mapping[str, Any]) -> str:
    return "".join(str(seg.get("text", "")) for seg in sentence.get("segments", []))


def answer_parts(view: Mapping[str, Any]) -> Iterator[tuple[str, list[JsonObj]]]:
    """Every segment list the answer shows: Observed, model text, limitations, frozen tables."""
    for i, s in enumerate(view.get("observed", [])):
        yield f"observed[{i}]", list(s.get("segments", []))
    for part in ("interpretation", "hypotheses"):
        for i, s in enumerate(view.get(part) or []):
            yield f"{part}[{i}]", list(s.get("segments", []))
    for i, lim in enumerate(view.get("limitations", [])):
        segments = lim.get("segments")
        yield f"limitations[{i}]", list(segments) if segments else [{"text": lim.get("text", "")}]
    for spec in view.get("vizSpecs", []):
        frozen = spec.get("frozen")
        if not frozen:
            continue
        for r, row in enumerate(frozen.get("rows", [])):
            yield f"{spec.get('vizId')}.frozen.rows[{r}]", list(row)
        if frozen.get("note"):
            yield f"{spec.get('vizId')}.frozen.note", list(frozen["note"])


def fact_segments(view: Mapping[str, Any]) -> list[tuple[str, JsonObj]]:
    return [(where, seg) for where, segs in answer_parts(view) for seg in segs if "factId" in seg]


def _tokens(text: str) -> set[str]:
    return {m.group(0).strip(".,") for m in DIGIT_TOKEN.finditer(text)}


def allowed_tokens(view: Mapping[str, Any], question: str | None = None) -> set[str]:
    """Digit-bearing tokens that are not tool-returned numbers: the contract list, the question's
    own words, cohort and scope names, and the plan's own settings."""
    allowed = set(CONTRACT_ALLOWED_TOKENS)
    texts = [question or view.get("question", "")]
    plan = view.get("plan") or {}
    texts.append(str((plan.get("scope") or {}).get("name", "")))
    for cohort in view.get("cohorts", []):
        texts += [str(cohort.get("name") or ""), str(cohort.get("savedName") or "")]
    for editable in plan.get("editable", []):
        value = editable.get("value")
        if isinstance(value, Mapping):
            texts += [str(v) for v in value.values()]
        else:
            texts.append(str(value))
    for text in texts:
        allowed |= _tokens(text)
    return allowed


def _explained(token: str, allowed: set[str]) -> bool:
    """A token is explained when it is allowed whole, or is a hyphenated compound whose digit-bearing parts are
    each allowed: the panel gene in Observed's "TP53-mutated" label. A signed number ("-1.78") is not."""
    if not any(ch.isdigit() for ch in token) or token in allowed:
        return True
    parts = [part for part in token.split("-") if part]
    return "-" in token.strip("-") and all(not any(ch.isdigit() for ch in part) or part in allowed for part in parts)


def unexplained_digits(view: Mapping[str, Any], question: str | None = None) -> list[JsonObj]:
    """Digits outside fact segments that no allow-list explains (contract "Numbers on screen")."""
    allowed = allowed_tokens(view, question)
    found: list[JsonObj] = []
    for where, segments in answer_parts(view):
        for seg in segments:
            if "factId" in seg:
                continue
            text = str(seg.get("text", "")).replace(VIEWER_FOOTNOTE, "")
            for token in _tokens(text):
                if _explained(token, allowed):
                    continue
                if where.startswith("limitations") and token in LIMITATION_CONSTANTS:
                    continue
                found.append({"where": where, "token": token, "text": text[:160]})
    return found


# --- Checks over a view ----------------------------------------------------------------------


def numbers_cited(view: Mapping[str, Any], question: str | None = None) -> Check:
    """FR-16 / NFR-03: every number in the answer is a fact chip whose text is format_fact(value)."""
    facts = facts_by_id(view)
    problems: list[JsonObj] = []
    for where, seg in fact_segments(view):
        fact = facts.get(str(seg["factId"]))
        if fact is None:
            problems.append({"where": where, "factId": seg["factId"], "problem": "no such fact"})
            continue
        expected = format_fact(fact["value"], str(fact["unit"]))
        if seg.get("text") != expected:
            problems.append({"where": where, "factId": seg["factId"], "problem": f"chip reads {seg.get('text')!r}, fact formats to {expected!r}"})
    stray = unexplained_digits(view, question)
    chips = len(fact_segments(view))
    return expect(
        "every number in the answer is a fact chip",
        not problems and not stray,
        f"{chips} chips checked; {len(problems)} chip problems; {len(stray)} uncited digit tokens",
        chip_problems=problems,
        uncited=stray,
    )


def no_numbers(view: Mapping[str, Any], question: str | None = None) -> Check:
    """A limitation or Unsupported answer with no tool-returned number: no fact, no chip, no stray digit."""
    stray = unexplained_digits(view, question)
    chips = fact_segments(view)
    return expect(
        "no numbers in the answer",
        not view.get("facts") and not chips and not stray,
        f"{len(view.get('facts', []))} facts, {len(chips)} chips, {len(stray)} digit tokens",
        uncited=stray,
    )


def status_is(view: Mapping[str, Any] | None, statuses: Iterable[str], code: str | None | Literal["any"] = "any") -> Check:
    wanted = sorted(set(statuses))
    if view is None:
        return expect(f"run ends {'/'.join(wanted)}", False, "no snapshot of the run arrived")
    ok = view.get("status") in wanted and (code == "any" or view.get("code") == code)
    want_code = "" if code == "any" else f" with code {code}"
    return expect(
        f"run ends {'/'.join(wanted)}{want_code}",
        ok,
        f"status {view.get('status')}, code {view.get('code')}",
        status=view.get("status"),
        code=view.get("code"),
    )


def four_parts(view: Mapping[str, Any] | None) -> Check:
    """FR-15: a Complete answer has Observed, Interpretation, Hypotheses and next steps, Limitations."""
    if view is None:
        return expect("Complete with all four parts", False, "no snapshot")
    interpretation = [s for s in view.get("interpretation") or [] if s.get("segments") and not s.get("dropped")]
    next_steps = len(view.get("hypotheses") or []) + len(view.get("followUps", [])) + len(view.get("openLinks", []))
    parts = {
        "observed": len(view.get("observed", [])),
        "interpretation": len(interpretation),
        "hypothesesAndNextSteps": next_steps,
        "limitations": len(view.get("limitations", [])),
    }
    ok = (
        view.get("status") == "Complete"
        and parts["observed"] > 0
        and parts["interpretation"] > 0
        and next_steps > 0
        and isinstance(view.get("limitations"), list)
        and view.get("withheldReason") is None
    )
    return expect("Complete with all four parts", ok, f"status {view.get('status')}; parts {parts}", parts=parts, status=view.get("status"))


def viz_parity(view: Mapping[str, Any]) -> Check:
    """Each viz spec's pinnedHash recomputes from `pinned` and equals requestPinnedHash (parity badge)."""
    rows: list[JsonObj] = []
    ok = True
    for spec in view.get("vizSpecs", []):
        pinned = spec.get("pinned")
        if pinned is None:
            rows.append({"vizId": spec.get("vizId"), "pinned": "trimmed; read from the view route"})
            continue
        recomputed = pinned_hash(pinned)
        good = recomputed == spec.get("pinnedHash") == spec.get("requestPinnedHash")
        ok = ok and good
        rows.append({"vizId": spec.get("vizId"), "launcher": spec.get("launcher"), "recomputed": recomputed, "pinnedHash": spec.get("pinnedHash"), "requestPinnedHash": spec.get("requestPinnedHash")})
    return expect("viz pinnedHash recomputes and equals requestPinnedHash", ok, f"{len(rows)} viz specs", specs=rows)


def has_viz(view: Mapping[str, Any], launcher: str, app_id: str) -> Check:
    specs = [s for s in view.get("vizSpecs", []) if s.get("launcher") == launcher and s.get("appId") == app_id]
    return expect(f"a {launcher} card in {app_id}", bool(specs), f"{len(specs)} matching of {len(view.get('vizSpecs', []))} viz specs", vizIds=[s.get("vizId") for s in specs])


def limitation(view: Mapping[str, Any], code: str) -> JsonObj | None:
    return next((lim for lim in view.get("limitations", []) if lim.get("code") == code), None)


def has_limitation(view: Mapping[str, Any], code: str, present: bool = True) -> Check:
    found = limitation(view, code)
    name = f"limitation {code} {'present' if present else 'absent'}"
    return expect(name, (found is not None) == present, found.get("text", "") if found else "none", limitation=found)


# --- Checks over the turns of one run ------------------------------------------------------------


def all_snapshots(turns: Sequence[TurnResult], run_id: str) -> list[JsonObj]:
    return [s for t in turns for s in t.snapshots(run_id)]


def plan_before_first_call(turn: TurnResult) -> Check:
    """FR-05: the plan snapshot reaches the stream before the first step starts or runs."""
    plan_at = first_call_at = None
    for i, event in enumerate(turn.events):
        if event["type"] == "STATE_SNAPSHOT" and event["snapshot"].get("runId") == turn.run_id:
            snap = event["snapshot"]
            if plan_at is None and snap.get("plan") is not None:
                plan_at = i
            if first_call_at is None and any(s.get("status") in DATA_STEP_STATUSES for s in snap.get("steps", [])):
                first_call_at = i
        if event["type"] == "STEP_STARTED" and first_call_at is None:
            first_call_at = i
    ok = plan_at is not None and (first_call_at is None or plan_at < first_call_at)
    return expect("plan shown before the first tool call", ok, f"plan at event {plan_at}, first call at event {first_call_at}", planEvent=plan_at, firstCallEvent=first_call_at)


def stream_contract(turn: TurnResult) -> Check:
    """Exactly one terminal event per invocation, every frame within 32 KB, an event stream at all."""
    problems = []
    if turn.transport_error and not turn.disconnected:
        problems.append(turn.transport_error)
    if not turn.disconnected and turn.terminal_count != 1:
        problems.append(f"{turn.terminal_count} terminal events")
    if turn.oversized_frames:
        problems.append(f"frames over {FRAME_LIMIT_BYTES} bytes: {turn.oversized_frames}")
    return expect(f"{turn.action} turn keeps the stream contract", not problems, "; ".join(problems) or "one terminal event, frames within 32 KB", runId=turn.run_id)


def never_ran_data(turns: Sequence[TurnResult], run_id: str, record: Mapping[str, Any] | None) -> Check:
    """No data call: no record call, and no step of this run ever Running, Done, Empty or Failed."""
    snaps = all_snapshots(turns, run_id)
    touched = sorted({s["id"] for snap in snaps for s in snap.get("steps", []) if s.get("status") in DATA_STEP_STATUSES})
    facts = sorted({f["id"] for snap in snaps for f in snap.get("facts", [])})
    started = [e.get("stepName") for t in turns if t.run_id == run_id for e in t.events if e["type"] == "STEP_STARTED"]
    calls = None if record is None else [c.get("callId") for c in record.get("calls", [])]
    ok = not touched and not facts and not started and not calls
    source: Source = "record" if record is not None else "agent"
    basis = "record calls and snapshots" if record is not None else "snapshots only (no record source)"
    return expect(
        "no data call",
        ok,
        f"{basis}: steps run {touched}, facts {len(facts)}, STEP_STARTED {started}, calls {calls if calls is not None else 'n/a'}",
        source=source,
        stepsRun=touched,
        calls=calls,
    )


def signature_of(view: Mapping[str, Any] | None) -> str | None:
    plan = None if view is None else view.get("plan")
    return None if not plan else str(plan.get("signature"))


def signatures_equal(name: str, signatures: Sequence[str | None]) -> Check:
    distinct = sorted({s for s in signatures if s is not None})
    ok = bool(signatures) and None not in signatures and len(distinct) == 1
    return expect(name, ok, f"{len(signatures)} runs, {len(distinct)} distinct signatures", signatures=list(signatures))


# --- Checks over a run record -----------------------------------------------------------------------


def _pointer(document: Any, pointer: str) -> Any:
    current = document
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, Mapping):
            current = current[part]
        else:
            raise KeyError(pointer)
    return current


def facts_traceable(view: Mapping[str, Any], record: Mapping[str, Any] | None) -> Check:
    """FR-16: each fact names its call, and its JSON pointer into that call's output holds its value."""
    if record is None:
        return not_run("each fact traces to its call", "no record source", source="record")
    calls = {str(c["callId"]): c for c in record.get("calls", [])}
    record_facts = {str(f["id"]): f for f in record.get("facts", [])}
    problems: list[JsonObj] = []
    for fact in view.get("facts", []):
        stored = record_facts.get(str(fact["id"]))
        if stored is None:
            problems.append({"fact": fact["id"], "problem": "not in the record"})
            continue
        call = calls.get(str(stored.get("callId")))
        if call is None:
            problems.append({"fact": fact["id"], "problem": f"call {stored.get('callId')} not in the record"})
            continue
        try:
            value = _pointer({"observed": call.get("observed")}, str(fact["jsonPointer"]))
        except (KeyError, IndexError, ValueError):
            problems.append({"fact": fact["id"], "problem": f"pointer {fact['jsonPointer']} does not resolve"})
            continue
        if value != fact["value"]:
            problems.append({"fact": fact["id"], "problem": f"pointer holds {value!r}, fact says {fact['value']!r}"})
        if stored.get("requestSha256") != call.get("requestSha256") or stored.get("responseSha256") != call.get("responseSha256"):
            problems.append({"fact": fact["id"], "problem": "fact hashes differ from its call's"})
    return expect(
        "each fact traces to its call",
        not problems,
        f"{len(view.get('facts', []))} facts against {len(calls)} calls; {len(problems)} problems",
        source="record",
        problems=problems,
    )


def counted_set_visible(view: Mapping[str, Any], record: Mapping[str, Any] | None) -> Check:
    """FR-06: every counted call of the current plan version is a step on screen, and every finished step has its calls."""
    if record is None:
        return not_run("every counted call is a visible step", "no record source", source="record")
    plan = view.get("plan") or {}
    version = plan.get("version")
    steps = step_by_id(view)
    calls = [c for c in record.get("calls", []) if c.get("planVersion") == version]
    hidden = sorted({c["stepId"] for c in calls if c.get("stepId") not in steps})
    without_calls = []
    for step in steps.values():
        if step.get("status") in ("Done", "Empty", "Failed"):
            if step.get("kind") == "client" and step.get("status") != "Done":
                continue
            if not any(c.get("stepId") == step["id"] for c in calls):
                without_calls.append(step["id"])
        if step.get("status") == "Reused":
            source = step.get("reusedFrom") or {}
            if not any(c.get("stepId") == source.get("stepId") and c.get("planVersion") == source.get("planVersion") for c in record.get("calls", [])):
                without_calls.append(step["id"])
    return expect(
        "every counted call is a visible step",
        not hidden and not without_calls,
        f"{len(calls)} calls in version {version}; hidden steps {hidden}; finished steps without a call {without_calls}",
        source="record",
    )


def writes_confirmed(record: Mapping[str, Any] | None) -> Check:
    """Zero writes without confirmation: every client call follows an accepted Create of its nonce."""
    if record is None:
        return not_run("no write without a confirmation", "no record source", source="record")
    accepted = {c.get("nonce") for c in record.get("confirmations", []) if c.get("decision") == "create" and c.get("outcome") == "accepted"}
    client_calls = [c for c in record.get("calls", []) if c.get("transport") == "client"]
    unconfirmed = [c.get("callId") for c in client_calls if not accepted]
    return expect(
        "no write without a confirmation",
        not unconfirmed,
        f"{len(client_calls)} client writes, {len(accepted)} accepted Create confirmations",
        source="record",
        unconfirmed=unconfirmed,
    )
