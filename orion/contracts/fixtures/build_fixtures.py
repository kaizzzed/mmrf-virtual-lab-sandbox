#!/usr/bin/env python3
"""Build every ORION contract fixture from the agent's Pydantic models, deterministically.

Each fixture is validated by the models as it is built, so a fixture that exists is a fixture
that validates. Every snapshot goes through the agent's own `events.fit_view`, so a stream is
byte for byte what the agent sends, trimming included. Every number in them is illustrative:
synthetic values chosen to look plausible (some echo the design's predicted counts), not oracle
results from the live stack.

    PYTHONPATH=orion/agent python3 orion/contracts/fixtures/build_fixtures.py            # write here
    PYTHONPATH=orion/agent python3 orion/contracts/fixtures/build_fixtures.py --check    # write nothing; exit 1 on drift
    ... --mirror .sources/frontend/src/features/orion   # also write the fork's byte-identical copies

Run it with the agent image (see orion/agent/README.md): it needs pydantic, ag_ui and orion_agent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

HERE = Path(__file__).resolve().parent
_AGENT = HERE.parent.parent / "agent"
if (_AGENT / "orion_agent").is_dir() and str(_AGENT) not in sys.path:
    sys.path.insert(0, str(_AGENT))

from pydantic import BaseModel, JsonValue  # noqa: E402

from orion_agent import ir  # noqa: E402
from orion_agent.compiler import CompiledPlan, ResolvedScope, StepSpec  # noqa: E402
from orion_agent.events import fit_view  # noqa: E402
from orion_agent.interfaces import PLANNER_MODEL  # noqa: E402
from orion_agent.models import (  # noqa: E402
    CANONICAL_DEFINITION,
    FROZEN_ROWS_CAP,
    MAX_TOP_N_DE,
    PINNED,
    SNAPSHOT_LIMIT_BYTES,
    VIEW_RANK_CAP,
    ActionLogEntry,
    AnswerRecord,
    AskAction,
    Assumption,
    CallRecord,
    CaseIdsDefinition,
    ClarifyOption,
    ClarifyPending,
    Code,
    CohortRef,
    CohortView,
    ConfirmPending,
    ConsentPending,
    DeThresholds,
    Editable,
    EditableCohortName,
    EditableMethod,
    EditableScope,
    EditableThresholds,
    EditableTopN,
    Fact,
    FactRecord,
    FactValue,
    FilterClause,
    FilterDefinition,
    FilterSet,
    FinishStatus,
    FollowUp,
    FrozenTable,
    HeartbeatFrame,
    HeartbeatValue,
    Limitation,
    ModelCallRecord,
    ModelRole,
    NearestApp,
    Pending,
    PendingCohort,
    PinnedKind,
    PlanHistoryEntry,
    PlanRow,
    PlanView,
    Provenance,
    RunAgentInputModel,
    RunFinishedFrame,
    RunResult,
    RunStartedFrame,
    RunStatus,
    RunView,
    SaveCohortsArgs,
    Scope,
    ScopeChoice,
    Segment,
    Sentence,
    StateSnapshotFrame,
    StepFinishedFrame,
    StepOrigin,
    StepStartedFrame,
    StepStatus,
    StepView,
    SupersededSummary,
    TextMessageContentFrame,
    TextMessageEndFrame,
    TextMessageStartFrame,
    ToolCallArgsFrame,
    ToolCallEndFrame,
    ToolCallStartFrame,
    ToolStatus,
    Transport,
    ViewEnvironment,
    VizSpec,
    call_id,
    canonical_definition,
    canonical_json,
    canonical_pinned,
    def_sha256,
    definition_from_filter_set,
    derive_request_pinned,
    fingerprint_document,
    fingerprint_sha256,
    format_fact,
    model_call_id,
    pinned_hash,
    save_cohorts_args_chunks,
    sha256_hex,
    text_message_chunks,
)
from orion_agent.run_store import Environment, RunRecord  # noqa: E402
from orion_agent.tools import schemas as catalog  # noqa: E402
from orion_agent.tools.base import KmCurve, KmPoint  # noqa: E402
from orion_agent.tools.functions import derive_km_median  # noqa: E402

T0 = datetime(2026, 11, 4, 15, 0, 0, tzinfo=UTC)
ALL_CASES_DEF = FilterDefinition(kind="filter", clauses=[])
SSM_DEF = FilterDefinition(kind="filter", clauses=[FilterClause(field="available_variation_data", op="in", values=["ssm"])])
CGS = "cgs_risk_key_criteria.cgs_risk_category"
CGS_TWIN = "cgs_risk_category"
STOPPED = "Not run: stopped"


def iso(seconds: float) -> str:
    moment = T0 + timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hexid(seed: str) -> str:
    return hashlib.sha256(f"orion-fixture:{seed}".encode()).hexdigest()[:32]


def fake_sha(seed: str) -> str:
    return hashlib.sha256(f"orion-fixture-sha:{seed}".encode()).hexdigest()


def compact(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)


def pretty(value: JsonValue) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def seg(text: str, fact_id: str | None = None) -> Segment:
    return Segment(text=text) if fact_id is None else Segment(text=text, fact_id=fact_id)


def chip(f: Fact) -> Segment:
    """A fact's segment: its value as format_fact writes it, with its citation."""
    return seg(format_fact(f.value, f.unit), f.id)


Part = str | Fact


def segments(*parts: Part) -> list[Segment]:
    return [seg(p) if isinstance(p, str) else chip(p) for p in parts]


def sentence(*parts: Part) -> Sentence:
    return Sentence(segments=segments(*parts))


def model_sentence(parts: Sequence[Part], cites: Sequence[Fact]) -> Sentence:
    """A verified Interpretation or Hypotheses sentence: placeholders resolved, the composer's cites kept."""
    return Sentence(segments=segments(*parts), cites=[f.id for f in cites])


def fact(fact_id: str, label: str, value: FactValue, unit: str, step_id: str, row: int, tool: str, pointer: str) -> Fact:
    return Fact(id=fact_id, label=label, value=value, unit=unit, step_id=step_id, row=row, tool=tool, json_pointer=pointer)


def by_id(facts: Sequence[Fact]) -> dict[str, Fact]:
    return {f.id: f for f in facts}


# --- The fixture fingerprint (s1) -----------------------------------------------------------------
# The four sources fixture_fingerprint reads, in the local stack's shapes (values illustrative).

FP_STATUS: dict[str, JsonValue] = {"status": "OK", "synthetic": True, "cases": 1000, "csrf": "synthetic-only"}
FP_VERSION: dict[str, JsonValue] = {"version": "1.0.3"}
FP_HEALTH: dict[str, JsonValue] = {
    "status": "ok",
    "versionInfo": {"pkgver": "2.216.0-20261003.02:08", "codedate": "20261003.02:08", "launchdate": "Wed Nov 04 2026 20:24:31"},
    "dsSummary": {"total": 1, "done": 1, "nonblocking": 0, "retrying": 0, "failed": 0},
}
FP_RANKING: list[dict[str, JsonValue]] = [
    {"gene": "KRAS", "cases": 480}, {"gene": "NRAS", "cases": 355}, {"gene": "TP53", "cases": 301},
    {"gene": "DIS3", "cases": 288}, {"gene": "BRAF", "cases": 97}, {"gene": "MYC", "cases": 64},
]
FP_DOC = fingerprint_document(FP_STATUS, FP_VERSION, FP_HEALTH, FP_RANKING)
FP_SHA = fingerprint_sha256(FP_DOC)
FP_SUMMARY = f"fingerprint {FP_SHA[:4]}…"


# --- A run as it streams --------------------------------------------------------------------------


@dataclass
class Run:
    thread_id: str
    run_id: str
    question: str
    revision: int = 0
    status: RunStatus = "Planning"
    code: Code | None = None
    plan: PlanView | None = None
    steps: list[StepView] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    observed: list[Sentence] = field(default_factory=list)
    limitations: list[Limitation] = field(default_factory=list)
    interpretation: list[Sentence] | None = None
    hypotheses: list[Sentence] | None = None
    withheld_reason: str | None = None
    viz_specs: list[VizSpec] = field(default_factory=list)
    pending: Pending | None = None
    cohorts: list[CohortView] = field(default_factory=list)
    follow_ups: list[FollowUp] = field(default_factory=list)
    open_links: list[NearestApp] = field(default_factory=list)
    superseded: list[SupersededSummary] = field(default_factory=list)
    fingerprint: str | None = None

    def view(self, at: str) -> RunView:
        self.revision += 1
        return RunView(
            schema_="orion.run-view/v1",
            thread_id=self.thread_id,
            run_id=self.run_id,
            revision=self.revision,
            status=self.status,
            code=self.code,
            question=self.question,
            updated_at=at,
            plan=self.plan,
            steps=list(self.steps),
            facts=list(self.facts),
            observed=list(self.observed),
            limitations=list(self.limitations),
            interpretation=self.interpretation,
            hypotheses=self.hypotheses,
            withheld_reason=self.withheld_reason,
            viz_specs=list(self.viz_specs),
            pending=self.pending,
            cohorts=list(self.cohorts),
            follow_ups=list(self.follow_ups),
            open_links=list(self.open_links),
            superseded=list(self.superseded),
            environment=ViewEnvironment(mode="L", fingerprint_sha256=self.fingerprint),
        )

    def set_step(self, step_id: str, **update: Any) -> None:
        self.steps = [s.model_copy(update=update) if s.id == step_id else s for s in self.steps]

    def set_cohort(self, ref: str, **update: Any) -> None:
        self.cohorts = [c.model_copy(update=update) if c.ref == ref else c for c in self.cohorts]

    def fact(self, fact_id: str) -> Fact:
        return next(f for f in self.facts if f.id == fact_id)


class Stream:
    """One invocation's frames. Snapshots are fitted to 32 KB exactly as the agent fits them."""

    def __init__(self, run: Run) -> None:
        self.run = run
        self.frames: list[BaseModel] = []
        self.views: list[RunView] = []  # untrimmed, as the record (and the view route) keeps them

    def add(self, frame: BaseModel) -> None:
        self.frames.append(frame)

    def started(self) -> None:
        self.add(RunStartedFrame(type="RUN_STARTED", thread_id=self.run.thread_id, run_id=self.run.run_id))

    def snapshot(self, at: str, run: Run | None = None) -> RunView:
        """A snapshot of the invocation's run, or of another run of the thread reported in this stream."""
        view = (run or self.run).view(at)
        self.views.append(view)
        self.add(StateSnapshotFrame(type="STATE_SNAPSHOT", snapshot=RunView.model_validate(fit_view(view))))
        return view

    def heartbeat(self, at: str) -> None:
        self.add(HeartbeatFrame(type="CUSTOM", name="orion.heartbeat", value=HeartbeatValue(ts=at)))

    def finished(self, status: FinishStatus, code: Code | None = None) -> None:
        self.add(
            RunFinishedFrame(
                type="RUN_FINISHED", thread_id=self.run.thread_id, run_id=self.run.run_id, result=RunResult(status=status, code=code)
            )
        )

    def text_message(self, message_id: str, markdown: str) -> None:
        self.add(TextMessageStartFrame(type="TEXT_MESSAGE_START", message_id=message_id, role="assistant"))
        for delta in text_message_chunks(markdown):
            self.add(TextMessageContentFrame(type="TEXT_MESSAGE_CONTENT", message_id=message_id, delta=delta))
        self.add(TextMessageEndFrame(type="TEXT_MESSAGE_END", message_id=message_id))

    def save_cohorts_call(self, args: SaveCohortsArgs) -> None:
        self.add(ToolCallStartFrame(type="TOOL_CALL_START", tool_call_id=args.nonce, tool_call_name="save_cohorts"))
        for delta in save_cohorts_args_chunks(args):
            self.add(ToolCallArgsFrame(type="TOOL_CALL_ARGS", tool_call_id=args.nonce, delta=delta))
        self.add(ToolCallEndFrame(type="TOOL_CALL_END", tool_call_id=args.nonce))

    def last_view(self) -> RunView:
        return [v for v in self.views if v.run_id == self.run.run_id][-1]

    def text(self) -> str:
        out: list[str] = []
        for frame in self.frames:
            line = f"data: {compact(frame)}\n\n"
            if len(line.encode("utf-8")) > SNAPSHOT_LIMIT_BYTES:
                raise ValueError(f"a {type(frame).__name__} frame exceeds {SNAPSHOT_LIMIT_BYTES} bytes")
            out.append(line)
        return "".join(out)


@dataclass(frozen=True)
class StepRun:
    """How one step ends in a fixture, and the facts it yields."""

    step_id: str
    started_at: float
    output_summary: str
    duration_ms: int
    facts: Sequence[Fact] = ()
    status: StepStatus = "Done"


OnFinish = Callable[[Run, StepRun], None]


def run_steps(
    stream: Stream,
    steps: Sequence[StepRun],
    *,
    facts_live: bool,
    heartbeat: tuple[str, float] | None = None,
    on_finish: OnFinish | None = None,
) -> None:
    """STEP_STARTED, then one snapshot (the previous step finished, this one Running), then STEP_FINISHED.

    The last step's outcome lands in the caller's next snapshot. With `facts_live`, each step's
    facts join the view as it finishes; otherwise the caller adds them later.
    """
    run = stream.run
    previous: StepRun | None = None
    for step in steps:
        stream.add(StepStartedFrame(type="STEP_STARTED", step_name=step.step_id))
        if previous is not None:
            finish(run, previous, facts_live, on_finish)
        run.set_step(step.step_id, status="Running")
        stream.snapshot(iso(step.started_at))
        if heartbeat is not None and heartbeat[0] == step.step_id:
            stream.heartbeat(iso(heartbeat[1]))
        stream.add(StepFinishedFrame(type="STEP_FINISHED", step_name=step.step_id))
        previous = step
    if previous is not None:
        finish(run, previous, facts_live, on_finish)


def finish(run: Run, step: StepRun, facts_live: bool, on_finish: OnFinish | None = None) -> None:
    run.set_step(step.step_id, status=step.status, output_summary=step.output_summary, duration_ms=step.duration_ms)
    if facts_live:
        run.facts.extend(step.facts)
    if on_finish is not None:
        on_finish(run, step)


def silent(run: Run) -> Stream:
    """A stream whose frames no fixture keeps: it brings a run to the state a fixture starts from."""
    return Stream(run)


def queued(step_id: str, row: int, tool: str, kind: str, summary: str, folded: bool = False) -> StepView:
    return StepView.model_validate(
        {"id": step_id, "row": row, "tool": tool, "kind": kind, "folded": folded, "inputSummary": summary,
         "status": "Queued", "outputSummary": None, "durationMs": None, "code": None, "reusedFrom": None}
    )


def scope_cohort(name: str = "All cases", case_count: int | None = 1000) -> CohortView:
    # The active cohort is unsaved ("All cases", id null in the cohort context), so cohortId is null.
    return CohortView(
        ref="base", role="scope", source="active_cohort", name=name, definition_text="All cases, no filters",
        def_sha256=def_sha256(ALL_CASES_DEF), case_count=case_count, save="none", cohort_id=None, saved_name=None,
    )


def fingerprinted(run: Run, step: StepRun) -> None:
    """s1 finished: the fixture fingerprint is known and the scope (all cases) is sized by a fact."""
    if step.step_id == "s1":
        run.fingerprint = FP_SHA
        run.set_cohort("base", case_count=1000, case_count_fact_id="f.s1.cases")


def markdown_answer(run: Run, notice: str) -> str:
    def plain(parts: Sequence[Segment]) -> str:
        return "".join(part.text for part in parts)

    lines = ["**Observed**", *(f"- {plain(s.segments)}" for s in run.observed)]
    if run.interpretation:
        lines += ["", "**Interpretation**", *(f"- {plain(s.segments)}" for s in run.interpretation)]
    if run.hypotheses:
        lines += ["", "**Hypotheses and next steps**", *(f"- {plain(s.segments)}" for s in run.hypotheses)]
    if run.withheld_reason:
        lines += ["", f"_Interpretation withheld: {run.withheld_reason}_"]
    lines += ["", "**Limitations**", *(f"- {item.text}" for item in run.limitations), "", f"_{notice}_"]
    return "\n".join(lines)


def end_with_answer(stream: Stream, at: float, status: Literal["Complete", "ObservedOnly"] = "Complete") -> RunView:
    run = stream.run
    run.status = status
    plan = run.plan
    view = stream.snapshot(iso(at))
    stream.text_message(f"{run.run_id}.v{plan.version if plan else 1}", markdown_answer(run, view.notice))
    stream.finished(status)
    return view


# --- J1: survival by CGS risk ------------------------------------------------------------------------

J1_QUESTION = "Compare survival of CGS high-risk vs. CGS standard-risk patients"
J1_SIGNATURE = "3c1f0e7a9b5d2c4e6f8a0b1c3d5e7f9a2b4c6d8e0f1a3b5c7d9e1f3a5b7c9d1e"


# Every plan offers the scope edit (TOOLS.md 6.4); the plan card offers an edit only while the
# run's status accepts edit_plan (ACTION_STATUSES).
SCOPE_EDIT = EditableScope(field="scope", label="Scope", value=ScopeChoice(source="active_cohort"))


def j1_plan_view(editable: list[Editable] | None = None) -> PlanView:
    return PlanView(
        version=1,
        signature=J1_SIGNATURE,
        scope=Scope(source="active_cohort", name="All cases", case_count=1000),
        rows=[
            PlanRow(row=1, label="Count both CGS risk groups", tool="cohort_count", reason="Cohort Builder counts filter cohorts", step_ids=["s1", "s2", "s3"]),
            PlanRow(row=2, label="Compare survival: Kaplan–Meier, split by CGS risk category", tool="survival_compare", reason="Correlation Plot runs survival comparisons", step_ids=["s4", "s5"]),
        ],
        assumptions=[
            Assumption(code="DEFAULT_SCOPE_ACTIVE", text="No cohort named: using the active cohort, All cases"),
            Assumption(code="SPLIT_CGS_RISK", text="High risk vs. Standard risk from CGS risk category"),
        ],
        writes=False,
        paraphrase="Compare overall survival between CGS High risk and Standard risk cases in All cases.",
        editable=[SCOPE_EDIT] if editable is None else editable,
    )


def j1_steps() -> list[StepView]:
    return [
        queued("s1", 1, "fixture_fingerprint", "server", "sandbox status, versions, ranking", folded=True),
        queued("s2", 1, "cohort_count", "server", "CGS risk category = High risk"),
        queued("s3", 1, "cohort_count", "server", "CGS risk category = Standard risk"),
        queued("s4", 2, "survival_compare", "server", "2 groups, Kaplan–Meier"),
        queued("s5", 2, "derive_km_median", "exec", "2 curves"),
    ]


J1_P = 7.1e-60


def _curve(points: Sequence[tuple[float, float]]) -> list[KmPoint]:
    return [KmPoint(time=t, survival=v) for t, v in points]


# s4's recorded curves (illustrative): High risk first falls to 0.5 or below at 1,095 days,
# Standard risk at 2,190. derive_km_median (TOOLS.md 3: the first time with survival <= 0.5) runs
# over them, so the median facts are what the function writes (floats: base.KmPoint.time is one).
J1_CURVES = [
    KmCurve(label="High risk", points=_curve([(0, 1.0), (365, 0.85), (730, 0.7), (1095, 0.48), (1460, 0.37), (2190, 0.22)])),
    KmCurve(label="Standard risk", points=_curve([(0, 1.0), (365, 0.93), (730, 0.85), (1095, 0.76), (1460, 0.62), (2190, 0.47)])),
]
J1_MEDIANS = derive_km_median(J1_CURVES, unit="days")


def _median(index: int) -> float:
    value = J1_MEDIANS.groups[index].median
    assert value is not None, "both J1 curves reach their median"
    return value


J1_FACTS: dict[str, list[Fact]] = {
    "s1": [fact("f.s1.cases", "Cases in the sandbox", 1000, "cases", "s1", 1, "fixture_fingerprint", "/observed/document/sandbox/cases")],
    "s2": [fact("f.s2.count", "High risk cases", 339, "cases", "s2", 1, "cohort_count", "/observed/count")],
    "s3": [fact("f.s3.count", "Standard risk cases", 661, "cases", "s3", 1, "cohort_count", "/observed/count")],
    "s4": [
        fact("f.s4.events.high", "High risk events", 299, "events", "s4", 2, "survival_compare", "/observed/groups/0/events"),
        fact("f.s4.events.standard", "Standard risk events", 300, "events", "s4", 2, "survival_compare", "/observed/groups/1/events"),
        fact("f.s4.p", "Log-rank p", J1_P, "p", "s4", 2, "survival_compare", "/observed/pValue"),
    ],
    "s5": [
        fact("f.s5.median.high", "High risk median overall survival", _median(0), "days", "s5", 2, "derive_km_median", "/observed/groups/0/median"),
        fact("f.s5.median.standard", "Standard risk median overall survival", _median(1), "days", "s5", 2, "derive_km_median", "/observed/groups/1/median"),
    ],
}
J1_ALL = by_id([f for facts in J1_FACTS.values() for f in facts])


def j1_count_sentences() -> list[Sentence]:
    return [
        sentence("High risk: ", J1_ALL["f.s2.count"], " cases."),
        sentence("Standard risk: ", J1_ALL["f.s3.count"], " cases."),
    ]


def j1_observed(with_medians: bool) -> list[Sentence]:
    f = J1_ALL
    out = [
        sentence("High risk: ", f["f.s2.count"], " cases, ", f["f.s4.events.high"], " events."),
        sentence("Standard risk: ", f["f.s3.count"], " cases, ", f["f.s4.events.standard"], " events."),
        sentence("Log-rank p = ", f["f.s4.p"], " (Gen3 Analysis)."),
    ]
    if with_medians:
        out.append(
            sentence(
                "Median overall survival: High risk ", f["f.s5.median.high"], " days; Standard risk ",
                f["f.s5.median.standard"], " days (Kaplan–Meier median derived by ORION from the engine's curves).",
            )
        )
    return out


VIEWER_P_ROUNDED = Limitation(
    code="VIEWER_P_ROUNDED", text="The survival chart shows this p-value as p = 0; the value above comes from Gen3 Analysis.", unblock=None
)
J1_STAT_UNAVAILABLE = Limitation(
    code="STAT_UNAVAILABLE", text="The platform reports no hazard ratio for this comparison, so the answer gives none.", unblock=None
)
J1_CAPTION = "The chart's log-rank table shows p = 0; the answer's p-value comes from Gen3 Analysis."


def _gql(value: str) -> dict[str, JsonValue]:
    return {"and": [{"in": {CGS: [value]}}]}


# survival_compare's recorded request: the scope (null for all cases), the survival term, and the
# split field with its ProteinPaint twin, which derive_request_pinned turns into the pinned form.
J1_S4_REQUEST: dict[str, JsonValue] = {
    "scope": None, "term": "overall_survival", "split": {"field": CGS, "overlay": CGS_TWIN},
    "groups": [{"label": "High risk", "filter": _gql("High risk")}, {"label": "Standard risk", "filter": _gql("Standard risk")}],
}


def j1_frozen() -> FrozenTable:
    f = J1_ALL
    rows = [
        [seg(label), chip(f[count]), chip(f[f"f.s4.events.{key}"]), chip(f[f"f.s5.median.{key}"])]
        for label, key, count in (("High risk", "high", "f.s2.count"), ("Standard risk", "standard", "f.s3.count"))
    ]
    return FrozenTable(
        columns=["Group", "Cases", "Events", "Median (days)"], rows=rows,
        note=segments("Log-rank p = ", f["f.s4.p"], " (Gen3 Analysis)"),
    )


def j1_survival_viz(caption: str | None, frozen: FrozenTable | None) -> VizSpec:
    pinned = derive_request_pinned("survival", J1_S4_REQUEST)
    return VizSpec(
        viz_id="v1", app_id="Correlation", launcher="survival", cites_steps=["s4"], pinned=pinned, pinned_hash=pinned_hash(pinned),
        title="Overall survival by CGS risk category", caption=caption,
        request_pinned_hash=pinned_hash(derive_request_pinned("survival", J1_S4_REQUEST)), frozen=frozen, cohort_refs=["base"],
    )


J1_STEP_TIMES = [3.3, 3.8, 4.3, 4.8, 5.3]
J1_OUTPUTS = [
    ("s1", FP_SUMMARY, 412),
    ("s2", "339 cases", 188),
    ("s3", "661 cases", 171),
    ("s4", "2 curves, log-rank p reported", 1290),
    ("s5", "2 medians", 3),
]


def j1_step_runs(offset: float = -0.6) -> list[StepRun]:
    return [StepRun(sid, at + offset, out, ms, J1_FACTS[sid]) for (sid, out, ms), at in zip(J1_OUTPUTS, J1_STEP_TIMES)]


def j1_complete_parts(run: Run) -> None:
    """J1's code-written answer parts: Observed, Limitations and the survival card."""
    run.observed = j1_observed(with_medians=True)
    run.limitations = [VIEWER_P_ROUNDED, J1_STAT_UNAVAILABLE]
    run.viz_specs = [j1_survival_viz(caption=J1_CAPTION, frozen=j1_frozen())]


def j1_model_text(run: Run) -> None:
    f = J1_ALL
    run.interpretation = [
        model_sentence(
            ["In this synthetic cohort, the high-risk group's survival curve falls below the standard-risk group's, "
             "and the log-rank test gives p = ", f["f.s4.p"], "."],
            [f["f.s4.p"], f["f.s5.median.high"], f["f.s5.median.standard"]],
        ),
        model_sentence(["This compares the risk groups only and carries no clinical meaning."], [f["f.s2.count"], f["f.s3.count"]]),
    ]
    run.hypotheses = [model_sentence(["Does the gap remain within each ISS stage?"], [f["f.s4.p"]])]
    run.follow_ups = [
        FollowUp(label="Survival by ISS stage", question="Compare survival by ISS stage", kind="survival_compare"),
        FollowUp(label="Top mutated genes in high-risk cases", question=ONCO_QUESTION, kind="top_mutated_genes"),
    ]
    run.open_links = [NearestApp.of("CohortComparison")]


def j1_open(run: Run, stream: Stream, at: float = 2.6) -> None:
    stream.started()
    stream.snapshot(iso(0.1))
    run.status, run.plan, run.steps, run.cohorts = "Executing", j1_plan_view(), j1_steps(), [scope_cohort()]
    stream.snapshot(iso(at))


def stub_fingerprinted(run: Run, step: StepRun) -> None:
    """s1 finished: the record's environment names the fixture fingerprint (the scope stays unsized, as on Day 1)."""
    if step.step_id == "s1":
        run.fingerprint = FP_SHA


def build_j1_stub() -> str:
    """The Day 1 reference stream: the stub era's J1, ending ObservedOnly (no fingerprint fact yet).

    Its withheld reason, its Done steps and its end without a TEXT_MESSAGE are the Day 1 stub's, which
    no current configuration sends (`ORION_EXECUTOR=stub` now skips every step NOT_IMPLEMENTED and
    ends Limited; a live ObservedOnly end sends its TEXT_MESSAGE): the fork's replay tests pin those
    22 frames. The environment names the fingerprint once s1 is Done, as the agent's record does.

    Its plan is the J1 plan the live stub ask sends (tests/test_contract.py LiveAppStreamTests),
    the scope edit included: every plan offers it (TOOLS.md 6.4)."""
    run = Run(thread_id="4f0c1a9e2b7d4c3e8a6f5b1d0e9c2a7b", run_id="9b2e7c4a1f6d4e0b8c3a5d7f2e1b6c90", question=J1_QUESTION)
    stream = Stream(run)
    j1_open(run, stream, at=3.2)
    steps = [StepRun(sid, at, out, ms) for (sid, out, ms), at in zip(J1_OUTPUTS, J1_STEP_TIMES)]
    run_steps(stream, steps, facts_live=False, heartbeat=("s4", 5.3), on_finish=stub_fingerprinted)
    run.status = "Answering"
    run.facts = [f for sid in ("s2", "s3", "s4") for f in J1_FACTS[sid]]
    run.observed = j1_observed(with_medians=False)
    run.limitations = [VIEWER_P_ROUNDED]
    viz = j1_survival_viz(caption=None, frozen=None)
    run.viz_specs = [viz]
    stream.snapshot(iso(7.0))
    run.status, run.withheld_reason = "ObservedOnly", "Interpretation is not built yet (Day 1 stub)"
    stream.snapshot(iso(7.1))
    stream.finished("ObservedOnly")
    return stream.text()


def j1_complete_run() -> tuple[Run, Stream]:
    run = Run(thread_id=hexid("j1-complete/thread"), run_id=hexid("j1-complete/run"), question=J1_QUESTION)
    stream = Stream(run)
    j1_open(run, stream)
    run_steps(stream, j1_step_runs(), facts_live=True, heartbeat=("s4", 4.7), on_finish=fingerprinted)
    run.status = "Answering"
    j1_complete_parts(run)
    stream.snapshot(iso(6.4))
    j1_model_text(run)
    end_with_answer(stream, 9.8)
    return run, stream


# --- J1 partial, then retry_step (E11: a survival_compare timeout) -----------------------------------

TOOL_FAILED_S4 = Limitation(
    code="TOOL_FAILED",
    text="The survival comparison timed out on both attempts, so events, the log-rank p-value and the medians are missing.",
    unblock="Retry step reruns the survival comparison with the same request.",
)


def j1_retry_run() -> Run:
    return Run(thread_id=hexid("j1-retry/thread"), run_id=hexid("j1-retry/run"), question=J1_QUESTION)


def j1_partial(run: Run) -> Stream:
    """The ask turn: s4 survival_compare times out, retries once with the identical body, fails: Partial."""
    stream = Stream(run)
    j1_open(run, stream)
    run_steps(stream, j1_step_runs()[:3], facts_live=True, on_finish=fingerprinted)
    stream.add(StepStartedFrame(type="STEP_STARTED", step_name="s4"))
    run.set_step("s4", status="Running")
    stream.snapshot(iso(4.2))
    stream.heartbeat(iso(19.2))
    stream.heartbeat(iso(34.2))
    run.set_step("s4", status="Retrying")
    stream.snapshot(iso(59.2))
    stream.heartbeat(iso(74.2))
    stream.heartbeat(iso(89.2))
    stream.heartbeat(iso(104.2))
    stream.add(StepFinishedFrame(type="STEP_FINISHED", step_name="s4"))
    run.set_step("s4", status="Failed", output_summary="Timed out on both attempts", duration_ms=114_600, code="TOOL_FAILED")
    run.set_step("s5", status="Skipped", output_summary="Not run: s4 failed")
    run.status, run.code = "Partial", "TOOL_FAILED"
    run.observed = j1_count_sentences()
    run.limitations = [TOOL_FAILED_S4]
    run.withheld_reason = "a step failed"
    stream.snapshot(iso(118.9))
    stream.finished("Partial", "TOOL_FAILED")
    return stream


def j1_retry(run: Run) -> Stream:
    """The retry_step turn: s4 and the step skipped because of it rerun; the run ends Complete."""
    stream = Stream(run)
    stream.started()
    run.status, run.code, run.withheld_reason = "Executing", None, None
    for sid in ("s4", "s5"):
        run.set_step(sid, status="Queued", output_summary=None, duration_ms=None, code=None)
    stream.snapshot(iso(200.0))

    def failure_cleared(r: Run, step: StepRun) -> None:
        if step.step_id == "s4":
            r.limitations = [item for item in r.limitations if item.code != "TOOL_FAILED"]

    steps = [StepRun("s4", 200.1, "2 curves, log-rank p reported", 1310, J1_FACTS["s4"]), StepRun("s5", 201.5, "2 medians", 3, J1_FACTS["s5"])]
    run_steps(stream, steps, facts_live=True, on_finish=failure_cleared)
    run.status = "Answering"
    j1_complete_parts(run)
    stream.snapshot(iso(201.6))
    j1_model_text(run)
    end_with_answer(stream, 205.0)
    return stream


# --- J1 stopped during survival_compare (E24) ----------------------------------------------------------


def j1_stop() -> Stream:
    """The cancel turn, joining the live run at s4: Stopping at once, the call aborted, then Cancelled."""
    run = Run(thread_id=hexid("j1-stop/thread"), run_id=hexid("j1-stop/run"), question=J1_QUESTION)
    before = silent(run)
    j1_open(run, before)
    run_steps(before, j1_step_runs()[:3], facts_live=True, on_finish=fingerprinted)
    before.add(StepStartedFrame(type="STEP_STARTED", step_name="s4"))
    run.set_step("s4", status="Running")
    before.snapshot(iso(4.2))
    stream = Stream(run)
    stream.started()
    run.status = "Stopping"
    stream.snapshot(iso(9.0))
    # The executor aborted the in-flight survival request (its CallRecord: error "CANCELLED").
    run.set_step("s4", status="Skipped", output_summary=STOPPED, duration_ms=4_840)
    stream.snapshot(iso(9.1))
    run.set_step("s5", status="Skipped", output_summary=STOPPED)
    run.status = "Cancelled"
    run.observed = j1_count_sentences()
    run.withheld_reason = "you stopped the run"
    stream.snapshot(iso(9.2))
    stream.finished("Cancelled")
    return stream


# --- J3: differential expression by KRAS status ----------------------------------------------------------

J2_NONCE = hexid("j2/nonce-1")
J3_QUESTION = "Differential expression between patients with and without a KRAS mutation, top 10 genes"
J3_SIGNATURE = fake_sha("j3-plan-v1")
J3_NONCE = hexid("j3/nonce-1")
WT_NAME = "KRAS not detected (ORION)"
MUT_NAME = "KRAS mutated (ORION)"
WT_RENAMED = "KRAS wild-type (ORION)"


def case_id(n: int) -> str:
    return f"SYNTH-CASE-{n:04d}"


def kras_mutated_numbers() -> list[int]:
    ranked = sorted(range(1, 1001), key=lambda n: hashlib.sha256(f"kras:{n}".encode()).hexdigest())
    return sorted(ranked[:480])


MUT = kras_mutated_numbers()
WT = sorted(set(range(1, 1001)) - set(MUT))
MUT_DEF = CaseIdsDefinition(kind="case_ids", case_ids=[case_id(n) for n in MUT])
WT_DEF = CaseIdsDefinition(kind="case_ids", case_ids=[case_id(n) for n in WT])
J3_THRESHOLDS = DeThresholds(p_value_cutoff=0.05, p_value_type="adjusted", fold_change_cutoff=0.3)
WT_COHORT_ID = "5f2c9a7e-1b3d-4e6f-8a0c-2d4b6e8f0a13"
MUT_COHORT_ID = "9a1e3c5b-7d2f-4a6e-b8c0-4e6a8c0e2b57"


@dataclass(frozen=True)
class J3Version:
    """What a J3 plan version names: the version, its signature and the control cohort's name."""

    version: int = 1
    signature: str = J3_SIGNATURE
    wt_name: str = WT_NAME


def j3_plan_view(v: J3Version = J3Version()) -> PlanView:
    return PlanView(
        version=v.version,
        signature=v.signature,
        scope=Scope(source="active_cohort", name="All cases", case_count=1000),
        rows=[
            PlanRow(row=1, label="Find KRAS-mutated cases", tool="gene_cases", reason="Gen3 Analysis API lists case IDs", step_ids=["s1", "s2"]),
            PlanRow(row=2, label="Find mutation-tested cases", tool="cases_query", reason="Gen3 Analysis API lists case IDs", step_ids=["s3"]),
            PlanRow(row=3, label="Subtract to get KRAS not detected", tool="set_operation", reason="ORION computes set operations in code", step_ids=["s4", "s5"]),
            PlanRow(row=4, label="Check RNA-seq-eligible group sizes", tool="de_preanalysis", reason="Differential Expression checks eligible samples before any save", step_ids=["s6", "s7"]),
            PlanRow(row=5, label="Save 2 cohorts", tool="save_cohorts", reason="Cohort Builder saves cohorts after your click", step_ids=["s8"]),
            PlanRow(row=6, label="Run DE: Wilcoxon, rank by adjusted p", tool="de_run", reason="Differential Expression runs the comparison in its own viewer", step_ids=["s9", "s10"]),
        ],
        assumptions=[
            Assumption(code="DEFAULT_SCOPE_ACTIVE", text="No cohort named: using the active cohort, All cases"),
            Assumption(code="EXCLUDE_UNTESTED", text="Without a mutation means mutation-tested cases without it; cases without mutation data are left out and counted."),
            Assumption(code="DE_METHOD_WILCOXON", text="Method: Wilcoxon rank-sum"),
            Assumption(code="RANK_ADJ_P", text="Genes ranked by adjusted p-value"),
            Assumption(code="DE_THRESHOLDS_VIEWER", text="Thresholds: adjusted p < 0.05 and |log2FC| > 0.3, the viewer's defaults"),
            Assumption(code="CONTROL_FIRST", text=f"{v.wt_name} is the control group: fold change is log2({MUT_NAME} / {v.wt_name})"),
        ],
        writes=True,
        paraphrase="Compare gene expression between mutation-tested cases with and without a KRAS mutation in All cases, and list the top 10 genes by adjusted p-value.",
        editable=[
            EditableScope(field="scope", label="Scope", value=ScopeChoice(source="active_cohort")),
            EditableMethod(field="method", label="Method", value="wilcoxon"),
            EditableTopN(field="topN", label="Top N", value=10, max=MAX_TOP_N_DE),
            EditableThresholds(field="thresholds", label="Thresholds", value=J3_THRESHOLDS),
            EditableCohortName(field="cohortName", label="Control cohort name", value=v.wt_name, ref="kras_wt"),
            EditableCohortName(field="cohortName", label="Case cohort name", value=MUT_NAME, ref="kras_mut"),
        ],
    )


def j3_steps(v: J3Version = J3Version()) -> list[StepView]:
    return [
        queued("s1", 1, "fixture_fingerprint", "server", "sandbox status, versions, ranking", folded=True),
        queued("s2", 1, "gene_cases", "server", "KRAS-mutated cases"),
        queued("s3", 2, "cases_query", "server", "Mutation-tested cases (available variation data = ssm)"),
        queued("s4", 3, "check_subset", "exec", "KRAS-mutated cases inside the mutation-tested cases"),
        queued("s5", 3, "set_operation", "exec", "Mutation-tested minus KRAS-mutated"),
        queued("s6", 4, "sample_lists", "server", "Both groups as ProteinPaint samples"),
        queued("s7", 4, "de_preanalysis", "server", "RNA-seq-eligible samples per group"),
        queued("s8", 5, "save_cohorts", "client", f"{v.wt_name}, {MUT_NAME}"),
        queued("s9", 6, "de_run", "server", f"Wilcoxon, {v.wt_name} vs. {MUT_NAME}"),
        queued("s10", 6, "rank_top_n", "exec", "Top 10 by adjusted p"),
    ]


def j3_cohorts(v: J3Version = J3Version()) -> list[CohortView]:
    return [
        scope_cohort(),
        CohortView(
            ref="tested", role="planned", source="planned", name="Mutation-tested cases",
            definition_text="Cases with SSM data (available variation data = ssm)", def_sha256=def_sha256(SSM_DEF),
            case_count=None, save="none", cohort_id=None, saved_name=None,
        ),
        CohortView(
            ref="kras_mut", role="planned", source="planned", name=MUT_NAME,
            definition_text="Mutation-tested cases with a KRAS mutation, as a case-ID list", def_sha256=None,
            case_count=None, save="proposed", cohort_id=None, saved_name=None,
        ),
        CohortView(
            ref="kras_wt", role="planned", source="planned", name=v.wt_name,
            definition_text="Mutation-tested cases without a KRAS mutation, as a case-ID list", def_sha256=None,
            case_count=None, save="proposed", cohort_id=None, saved_name=None,
        ),
    ]


J3_READ_FACTS: dict[str, list[Fact]] = {
    "s1": [fact("f.s1.cases", "Cases in the sandbox", 1000, "cases", "s1", 1, "fixture_fingerprint", "/observed/document/sandbox/cases")],
    "s2": [fact("f.s2.count", "KRAS-mutated cases", 480, "cases", "s2", 1, "gene_cases", "/observed/count")],
    "s3": [fact("f.s3.count", "Mutation-tested cases", 1000, "cases", "s3", 2, "cases_query", "/observed/count")],
    "s4": [fact("f.s4.outside", "KRAS-mutated cases outside the tested cases", 0, "cases", "s4", 3, "check_subset", "/observed/outsideCount")],
    "s5": [fact("f.s5.count", "KRAS not detected cases", 520, "cases", "s5", 3, "set_operation", "/observed/count")],
    "s6": [
        fact("f.s6.samples.control", "KRAS not detected samples", 520, "samples", "s6", 4, "sample_lists", "/observed/groups/0/count"),
        fact("f.s6.samples.case", "KRAS mutated samples", 480, "samples", "s6", 4, "sample_lists", "/observed/groups/1/count"),
    ],
    "s7": [
        fact("f.s7.eligible.control", "KRAS not detected RNA-seq-eligible samples", 520, "samples", "s7", 4, "de_preanalysis", "/observed/groups/0/eligible"),
        fact("f.s7.eligible.case", "KRAS mutated RNA-seq-eligible samples", 480, "samples", "s7", 4, "de_preanalysis", "/observed/groups/1/eligible"),
    ],
}
J3_READ_OUTPUTS = [
    ("s1", 2.9, FP_SUMMARY, 405), ("s2", 3.4, "480 case IDs", 233), ("s3", 3.7, "1,000 case IDs, 1 page", 391),
    ("s4", 4.1, "All 480 inside the mutation-tested cases", 2), ("s5", 4.2, "520 case IDs", 2),
    ("s6", 4.3, "520 and 480 samples", 140), ("s7", 4.5, "520 and 480 RNA-seq eligible", 61),
]


def j3_read_steps() -> list[StepRun]:
    return [StepRun(sid, at, out, ms, J3_READ_FACTS[sid]) for sid, at, out, ms in J3_READ_OUTPUTS]


def j3_sized(run: Run, step: StepRun) -> None:
    """A step that defines or sizes a run cohort finished: its defSha256, caseCount and count fact."""
    fingerprinted(run, step)
    sized: dict[str, tuple[str, CaseIdsDefinition | FilterDefinition, int, str]] = {
        "s2": ("kras_mut", MUT_DEF, 480, "f.s2.count"),
        "s3": ("tested", SSM_DEF, 1000, "f.s3.count"),
        "s5": ("kras_wt", WT_DEF, 520, "f.s5.count"),
    }
    if step.step_id in sized:
        ref, definition, n, fact_id = sized[step.step_id]
        run.set_cohort(ref, def_sha256=def_sha256(definition), case_count=n, case_count_fact_id=fact_id)


def j3_pending(v: J3Version = J3Version(), nonce: str = J3_NONCE) -> SaveCohortsArgs:
    def pending(ref: str, name: str, text: str, definition: CaseIdsDefinition, eligible: int, count_id: str, eligible_id: str) -> PendingCohort:
        return PendingCohort(
            ref=ref, name=name, definition_text=text, definition=definition, def_sha256=def_sha256(definition),
            case_count=len(definition.case_ids), eligible_count=eligible, reuse=None,
            case_count_fact_id=count_id, eligible_count_fact_id=eligible_id,
        )

    return SaveCohortsArgs(
        kind="confirm", nonce=nonce, plan_signature=v.signature, plan_version=v.version, step_id="s8",
        cohorts=[
            pending("kras_wt", v.wt_name, "Mutation-tested cases without a KRAS mutation, as a case-ID list", WT_DEF, 520, "f.s5.count", "f.s7.eligible.control"),
            pending("kras_mut", MUT_NAME, "Mutation-tested cases with a KRAS mutation, as a case-ID list", MUT_DEF, 480, "f.s2.count", "f.s7.eligible.case"),
        ],
    )


def j3_run(seed: str = "j3") -> Run:
    return Run(thread_id=hexid(f"{seed}/thread"), run_id=hexid(f"{seed}/run"), question=J3_QUESTION)


def variant_nonce(seed: str) -> str:
    """The save pause nonce of a J3 variant run (each pause has its own)."""
    return hexid(f"{seed}/nonce-1")


def j3_variant(seed: str) -> Run:
    """A J3 run brought to its save pause silently, for a fixture that starts there."""
    run = j3_run(seed)
    j3_pause(run, variant_nonce(seed))
    return run


def j3_await(stream: Stream, at: float, v: J3Version = J3Version(), nonce: str = J3_NONCE) -> None:
    """The write step waits: AwaitingConfirmation, then the save_cohorts tool call and RUN_FINISHED."""
    run = stream.run
    args = j3_pending(v, nonce)
    run.status = "AwaitingConfirmation"
    run.set_step("s8", status="AwaitingUser")
    run.pending = ConfirmPending.model_validate(args.model_dump())
    stream.snapshot(iso(at))
    stream.save_cohorts_call(args)
    stream.finished("AwaitingConfirmation")


def j3_pause(run: Run, nonce: str = J3_NONCE) -> Stream:
    """The ask turn: plan, read steps, then the save pause with the save_cohorts tool call."""
    stream = Stream(run)
    stream.started()
    stream.snapshot(iso(0.1))
    run.status, run.plan, run.steps, run.cohorts = "Executing", j3_plan_view(), j3_steps(), j3_cohorts()
    stream.snapshot(iso(2.8))
    run_steps(stream, j3_read_steps(), facts_live=True, on_finish=j3_sized)
    j3_await(stream, 4.6, nonce=nonce)
    return stream


J3_GENES = [
    ("BRAF", 1.12, 2.1e-09), ("ETV4", 0.94, 6.8e-09), ("JUN", -0.87, 1.4e-08), ("DUSP6", 1.31, 3.9e-08),
    ("FOS", -1.02, 7.7e-08), ("TP53", -0.66, 2.6e-07), ("MAPK1", 2.17, 5.0e-07), ("DIS3", 1.88, 1.2e-06),
    ("ETV5", 1.54, 3.3e-06), ("KRAS", 0.71, 8.1e-06),
]
J3_PANEL = Limitation(
    code="PANEL_COVERAGE",
    text="KRAS status comes from the six-gene mutation panel (KRAS, NRAS, TP53, BRAF, DIS3, MYC); other mutations are not profiled.",
    unblock=None,
)
WRITE_SKIPPED = Limitation(
    code="WRITE_SKIPPED",
    text="You chose Don't save, so no cohort was saved: the chart uses the unsaved groups and Open with this cohort is off.",
    unblock="Ask again and choose Create to save the cohorts.",
)
DE_SORT_CAPTION = "Table sorted by fold change. Sort by adjusted p-value to match the answer."
DE_INPUT_CAPTION = "Choose Wilcoxon and click Run to draw the chart. Table sorted by fold change; sort by adjusted p-value to match the answer."


def top_facts(step_id: str, row: int, genes: Sequence[tuple[str, float, float]]) -> list[Fact]:
    out: list[Fact] = []
    for k, (gene, lfc, padj) in enumerate(genes, start=1):
        pointer = f"/observed/rows/{k - 1}"
        out += [
            fact(f"f.{step_id}.top.{k}.gene", f"Rank {k} gene", gene, "gene", step_id, row, "rank_top_n", f"{pointer}/key"),
            fact(f"f.{step_id}.top.{k}.log2fc", f"{gene} log2 fold change", lfc, "log2FC", step_id, row, "rank_top_n", f"{pointer}/row/fold_change"),
            fact(f"f.{step_id}.top.{k}.padj", f"{gene} adjusted p", padj, "p", step_id, row, "rank_top_n", f"{pointer}/value"),
        ]
    return out


def de_observed(run: Run, wt: str, mut: str, shown: int, ranked: Fact | None) -> list[Sentence]:
    f = by_id(run.facts)
    top: list[Part] = ["Top-ranked genes by adjusted p-value: "]
    for k in range(1, shown + 1):
        top += [f[f"f.s10.top.{k}.gene"], " (log2FC ", f[f"f.s10.top.{k}.log2fc"], ", adjusted p ", f[f"f.s10.top.{k}.padj"], ")",
                "; " if k < shown else "."]
    out = [
        sentence("Mutation-tested cases: ", f["f.s3.count"], "; KRAS-mutated: ", f["f.s2.count"], "; KRAS not detected: ", f["f.s5.count"], "."),
        sentence(f"Groups: {wt} with ", f["f.s7.eligible.control"], f" RNA-seq-eligible samples (control), and {mut} with ",
                 f["f.s7.eligible.case"], " (case)."),
        sentence(f"Method: Wilcoxon rank-sum; genes ranked by adjusted p-value; fold change is log2({mut} / {wt}), so a positive value is higher in {mut}."),
        sentence(f["f.s9.significant"], " of ", f["f.s9.tested"], " tested genes passed the viewer's thresholds."),
        sentence(*top),
    ]
    if ranked is not None:
        out.append(sentence("Only the first part of the ranking is shown; all ", ranked, " ranked genes are in the run record."))
    return out


def de_frozen(run: Run, shown: int) -> FrozenTable:
    f = by_id(run.facts)
    rows = [[chip(f[f"f.s10.top.{k}.gene"]), chip(f[f"f.s10.top.{k}.log2fc"]), chip(f[f"f.s10.top.{k}.padj"])]
            for k in range(1, min(shown, FROZEN_ROWS_CAP) + 1)]
    return FrozenTable(
        columns=["Gene", "log2FC", "Adjusted p"], rows=rows,
        note=segments("Ranked by adjusted p; ", f["f.s9.significant"], " genes passed the viewer's thresholds."),
    )


def case_filter(ids: Sequence[str]) -> dict[str, JsonValue]:
    return {"and": [{"in": {"case_id": list(ids)}}]}


def de_request(wt: str, mut: str, wt_id: str | None, mut_id: str | None) -> dict[str, JsonValue]:
    """de_run's recorded request: both groups with names, sample ids and (once saved) cohort ids and filters."""
    def group(name: str, numbers: Sequence[int], cohort: str | None) -> dict[str, JsonValue]:
        g: dict[str, JsonValue] = {"name": name, "sampleIds": list(numbers)}
        if cohort is not None:
            g |= {"cohortId": cohort, "filter0": case_filter([case_id(n) for n in numbers])}
        return g

    return {
        "groups": [group(wt, WT, wt_id), group(mut, MUT, mut_id)],
        "method": "wilcoxon",
        "thresholds": J3_THRESHOLDS.model_dump(mode="json"),
    }


def de_viz(run: Run, request: dict[str, JsonValue], launcher: PinnedKind, shown: int) -> VizSpec:
    pinned = derive_request_pinned(launcher, request)
    return VizSpec(
        viz_id="v1", app_id="DE", launcher=launcher, cites_steps=["s9", "s10"], pinned=pinned, pinned_hash=pinned_hash(pinned),
        title="Differential expression: KRAS mutated vs. KRAS not detected",
        caption=DE_SORT_CAPTION if launcher == "deDirect" else DE_INPUT_CAPTION,
        request_pinned_hash=pinned_hash(derive_request_pinned(launcher, request)),
        frozen=de_frozen(run, shown), cohort_refs=["kras_wt", "kras_mut"],
    )


def de_model_text(run: Run, wt: str, mut: str) -> None:
    f = by_id(run.facts)
    top = [f["f.s10.top.1.gene"], f["f.s10.top.1.log2fc"], f["f.s10.top.1.padj"]]
    run.interpretation = [
        model_sentence(["In this synthetic dataset, ", f["f.s10.top.1.gene"],
                        f" has the smallest adjusted p-value between the groups, with higher expression in {mut} than in {wt}."], top),
        model_sentence(["Differential expression alone does not establish a mechanism, and these results carry no clinical meaning."],
                       [f["f.s9.significant"]]),
    ]
    run.hypotheses = [model_sentence(["Is ", f["f.s10.top.1.gene"], " expression also associated with survival in these cohorts?"],
                                     [f["f.s10.top.1.gene"]])]
    run.follow_ups = [
        FollowUp(label="Show the top 20", question="Show the top 20 genes instead", kind="differential_expression"),
        FollowUp(label="Survival by KRAS status", question=f"Compare survival between the {mut} and {wt} cohorts", kind="survival_compare"),
    ]
    run.open_links = [NearestApp.of("GeneExpression")]


def j3_confirm(run: Run, *, decision: Literal["create", "dont_save"] = "create", launcher: PinnedKind = "deDirect", start: float = 31.0) -> Stream:
    """The confirm turn: the action applied (s8 Running for Create, Skipped for Don't save), DE, Complete."""
    stream = Stream(run)
    stream.started()
    run.status, run.pending = "Executing", None
    saved = decision == "create"
    if saved:
        run.set_step("s8", status="Running")
    else:
        run.set_step("s8", status="Skipped", output_summary="Not saved: you chose Don't save", code="WRITE_SKIPPED")
        run.set_cohort("kras_wt", save="skipped")
        run.set_cohort("kras_mut", save="skipped")
        run.limitations = [*run.limitations, WRITE_SKIPPED]
    stream.snapshot(iso(start))
    s9_facts = [
        fact("f.s9.tested", "Genes tested", 7684, "genes", "s9", 6, "de_run", "/observed/totals/tested"),
        fact("f.s9.significant", "Genes past the thresholds", 16, "genes", "s9", 6, "de_run", "/observed/totals/significant"),
    ]

    def on_saved(r: Run, step: StepRun) -> None:
        if step.step_id == "s8":
            r.set_cohort("kras_wt", save="saved", cohort_id=WT_COHORT_ID, saved_name=WT_NAME)
            r.set_cohort("kras_mut", save="saved", cohort_id=MUT_COHORT_ID, saved_name=MUT_NAME)

    if saved:
        stream.add(StepStartedFrame(type="STEP_STARTED", step_name="s8"))
        stream.add(StepFinishedFrame(type="STEP_FINISHED", step_name="s8"))
        run.set_step("s8", status="Done", output_summary="2 cohorts saved and read back", duration_ms=6)
        on_saved(run, StepRun("s8", start, "", 6))
    steps = [
        StepRun("s9", start + 0.2, "16 of 7,684 genes past the thresholds", 1640, s9_facts),
        StepRun("s10", start + 1.9, "Top 10 by adjusted p, no ties", 1, top_facts("s10", 6, J3_GENES)),
    ]
    run_steps(stream, steps, facts_live=True, heartbeat=("s9", start + 1.5))
    run.status = "Answering"
    run.observed = de_observed(run, WT_NAME, MUT_NAME, len(J3_GENES), None)
    run.limitations = [J3_PANEL, *(item for item in run.limitations if item.code == "WRITE_SKIPPED")]
    ids = (WT_COHORT_ID, MUT_COHORT_ID) if saved else (None, None)
    run.viz_specs = [de_viz(run, de_request(WT_NAME, MUT_NAME, *ids), launcher, len(J3_GENES))]
    stream.snapshot(iso(start + 2.0))
    de_model_text(run, WT_NAME, MUT_NAME)
    end_with_answer(stream, start + 5.4)
    return stream


def j3_edit_rename(run: Run) -> Stream:
    """edit_plan cohortName at the save pause: version 2 reuses s1–s7, voids the nonce and pauses again."""
    v2 = J3Version(version=2, signature=fake_sha("j3-plan-v2-renamed"), wt_name=WT_RENAMED)
    stream = Stream(run)
    stream.started()
    run.superseded = [SupersededSummary(plan_version=1, at=iso(40.0), status="AwaitingConfirmation", had_answer=False)]
    run.plan = j3_plan_view(v2)
    finished = {s.id: s for s in run.steps if s.status == "Done"}
    run.steps = [
        finished[s.id].model_copy(update={"status": "Reused", "reused_from": StepOrigin(plan_version=1, step_id=s.id)})
        if s.id in finished else s
        for s in j3_steps(v2)
    ]
    run.set_cohort("kras_wt", name=WT_RENAMED)
    run.status, run.pending = "Executing", None
    stream.snapshot(iso(40.0))
    j3_await(stream, 40.1, v2, hexid("j3-edit/nonce-2"))
    return stream


# --- Clarify and consent pauses ----------------------------------------------------------------------------

CLARIFY_QUESTION = "Compare survival of high-risk patients with everyone else"


def clarify_pending() -> ClarifyPending:
    return ClarifyPending(
        kind="clarify", slot="split_term", question="Which classification defines high risk?",
        options=[
            ClarifyOption(option_id=CGS, field=CGS, label="CGS risk category", preview="High risk, Standard risk"),
            ClarifyOption(option_id="diagnoses.iss_stage", field="diagnoses.iss_stage", label="ISS stage", preview="Stage I, Stage II, Stage III"),
        ],
    )


def clarify_turn(stream: Stream, at: float) -> None:
    run = stream.run
    stream.snapshot(iso(at))
    run.status, run.pending = "Clarifying", clarify_pending()
    stream.snapshot(iso(at + 2.3))
    stream.finished("Clarifying")


def build_clarify() -> str:
    run = Run(thread_id=hexid("clarify/thread"), run_id=hexid("clarify/run"), question=CLARIFY_QUESTION)
    stream = Stream(run)
    stream.started()
    clarify_turn(stream, 0.1)
    return stream.text()


def consent_state(run: Run) -> None:
    plan = j1_plan_view()  # the scope edit is listed; edit_plan is not accepted while AwaitingConsent
    rows = list(plan.rows)
    rows[1] = rows[1].model_copy(update={"reason": "Correlation Plot runs survival comparisons; OncoMatrix, which you named, ranks mutated genes"})
    run.plan = plan.model_copy(update={"rows": rows, "paraphrase": "Compare overall survival between CGS High risk and Standard risk cases in All cases, in Correlation Plot instead of OncoMatrix."})
    run.status, run.code, run.steps, run.cohorts = "AwaitingConsent", "NAMED_TOOL_MISMATCH", j1_steps(), [scope_cohort()]
    run.limitations = [
        Limitation(
            code="NAMED_TOOL_MISMATCH",
            text="OncoMatrix ranks mutated genes and cannot plot survival, so nothing has run yet.",
            unblock="Use Correlation Plot, which runs survival comparisons, or cancel.",
            nearest_apps=[NearestApp.of("Correlation")],
        )
    ]
    run.pending = ConsentPending(
        kind="consent", named=NearestApp.of("OncoMatrix"), proposed=NearestApp.of("Correlation"),
        reason="OncoMatrix ranks mutated genes; Correlation Plot runs the Kaplan–Meier survival comparison you asked for.",
    )


CONSENT_QUESTION = "Use OncoMatrix to plot survival by CGS risk category"


def build_consent() -> str:
    """E7: the named tool cannot do the job; the plan waits for consent and makes no data call."""
    run = Run(thread_id=hexid("consent/thread"), run_id=hexid("consent/run"), question=CONSENT_QUESTION)
    stream = Stream(run)
    stream.started()
    stream.snapshot(iso(0.1))
    consent_state(run)
    stream.snapshot(iso(2.7))
    stream.finished("AwaitingConsent", "NAMED_TOOL_MISMATCH")
    return stream.text()


# --- Several runs in one stream: an Interrupted report, and cancelWaiting -----------------------------------

VENN_QUESTION = "Show a Venn diagram of the two KRAS cohorts"
UNSUPPORTED_VENN = Limitation(
    code="UNSUPPORTED_ANALYSIS",
    text="ORION cannot run this analysis. It compares survival, ranks mutated genes, runs differential expression and counts cohorts.",
    unblock=None, nearest_apps=[NearestApp.of("SetOperations")],
)
RUN_INTERRUPTED = Limitation(
    code="RUN_INTERRUPTED", text="The run stopped responding before it finished; completed steps are shown as partial.", unblock="Ask again."
)


def build_interrupted() -> str:
    """An ask on a thread whose J1 run lost its controller at s4: the old run Interrupted, then the new run."""
    thread = hexid("interrupted/thread")
    old = Run(thread_id=thread, run_id=hexid("interrupted/old"), question=J1_QUESTION)
    before = silent(old)
    j1_open(old, before)
    run_steps(before, j1_step_runs()[:3], facts_live=True, on_finish=fingerprinted)
    old.set_step("s4", status="Running")
    before.snapshot(iso(4.2))  # its lease (renewed at 4.2) expired at 184.2
    new = Run(thread_id=thread, run_id=hexid("interrupted/new"), question=VENN_QUESTION)
    stream = Stream(new)
    stream.started()
    old.status, old.code = "Interrupted", "RUN_INTERRUPTED"
    old.set_step("s4", status="Failed", code="RUN_INTERRUPTED")
    old.set_step("s5", status="Skipped", output_summary="Not run: interrupted")
    old.limitations = [*old.limitations, RUN_INTERRUPTED]
    stream.snapshot(iso(184.2), old)  # updatedAt is the lease's expiry, as the view route shows it
    stream.snapshot(iso(400.1))
    new.status, new.code, new.limitations = "Unsupported", "UNSUPPORTED_ANALYSIS", [UNSUPPORTED_VENN]
    stream.snapshot(iso(402.0))
    stream.finished("Unsupported", "UNSUPPORTED_ANALYSIS")
    return stream.text()


def build_cancel_waiting() -> str:
    """An ask with cancelWaiting: the paused consent run ends Cancelled, then the new question runs."""
    thread = hexid("cancel-waiting/thread")
    old = Run(thread_id=thread, run_id=hexid("cancel-waiting/old"), question=CONSENT_QUESTION)
    before = silent(old)
    before.started()
    before.snapshot(iso(0.1))
    consent_state(old)
    before.snapshot(iso(2.7))
    new = Run(thread_id=thread, run_id=hexid("cancel-waiting/new"), question=CLARIFY_QUESTION)
    stream = Stream(new)
    stream.started()
    old.status, old.code, old.pending = "Cancelled", None, None
    old.steps = [s.model_copy(update={"status": "Skipped", "output_summary": STOPPED}) for s in old.steps]
    stream.snapshot(iso(60.0), old)
    clarify_turn(stream, 60.1)
    return stream.text()


# --- J2: top mutated genes in a planned cohort, saved after one confirmation ---------------------------------
# The question is J1's follow-up chip. A planned filter cohort is saved once, last, after the ranking
# (C-SAVE, TOOLS.md 5.2 and choice 1): j2-save-pause, j2-after-confirm and j2-dont-save. When the
# browser already holds an identical saved cohort, the compiler reuses it and plans no save
# (TOOLS.md choice 15): `oncomatrix`, the chip asked in J1's thread.

ONCO_QUESTION = "Top 3 most frequently mutated genes in CGS high-risk patients"
ONCO_GENES = [("KRAS", 163), ("NRAS", 121), ("DIS3", 97)]
HIGH_DEF = FilterDefinition(kind="filter", clauses=[FilterClause(field=CGS, op="in", values=["High risk"])])
HIGH_NAME = "CGS high-risk (ORION)"
HIGH_TEXT = "CGS risk category = High risk"
HIGH_COHORT_ID = "3c7a1e9b-5d2f-4b8a-9e6c-1f0d2a4b6c88"
J2_SIGNATURE = fake_sha("j2-plan-v1")
J2_PARAPHRASE = "List the 3 most frequently mutated genes among CGS high-risk cases in All cases."
PANEL_RANKING = Limitation(code="PANEL_COVERAGE", text="Mutation data covers the six-gene panel only.", unblock=None)
WRITE_SKIPPED_ONE = Limitation(
    code="WRITE_SKIPPED",
    text="You chose Don't save, so the cohort was not saved and Open with this cohort is off.",
    unblock="Ask again and choose Create to save the cohort.",
)


def high_risk_ids() -> list[str]:
    ranked = sorted(range(1, 1001), key=lambda n: hashlib.sha256(f"cgs-high:{n}".encode()).hexdigest())
    return [case_id(n) for n in sorted(ranked[:339])]


def high_saved_context() -> dict[str, JsonValue]:
    """The cohort context when CGS high-risk is already a saved cohort (read as HIGH_DEF)."""
    filters: dict[str, JsonValue] = {"mode": "and", "root": {CGS: {"operator": "includes", "field": CGS, "operands": ["High risk"]}}}
    context = all_cases_context()
    context["saved"] = [{"id": HIGH_COHORT_ID, "name": HIGH_NAME, "filters": filters, "caseCount": 339}]
    return context


def j2_plan_view(*, saves: bool, signature: str = J2_SIGNATURE) -> PlanView:
    rows = [
        PlanRow(row=1, label="Find CGS high-risk cases", tool="cases_query", reason="Gen3 Analysis API lists case IDs", step_ids=["s1", "s2"]),
        PlanRow(row=2, label="Rank mutated genes: top 3", tool="top_mutated_genes", reason="OncoMatrix ranks mutated genes", step_ids=["s3", "s4"]),
    ]
    editable: list[Editable] = [SCOPE_EDIT, EditableTopN(field="topN", label="Top N", value=3, max=50)]
    if saves:
        rows.append(PlanRow(row=3, label="Save 1 cohort", tool="save_cohorts", reason="Cohort Builder saves cohorts after your click", step_ids=["s5"]))
        editable.append(EditableCohortName(field="cohortName", label="Cohort name", value=HIGH_NAME, ref="high"))
    return PlanView(
        version=1, signature=signature, scope=Scope(source="active_cohort", name="All cases", case_count=1000), rows=rows,
        assumptions=[Assumption(code="DEFAULT_SCOPE_ACTIVE", text="No cohort named: using the active cohort, All cases")],
        writes=saves, paraphrase=J2_PARAPHRASE, editable=editable,
    )


def j2_steps(*, saves: bool) -> list[StepView]:
    steps = [
        queued("s1", 1, "fixture_fingerprint", "server", "sandbox status, versions, ranking", folded=True),
        queued("s2", 1, "cases_query", "server", HIGH_TEXT),
        queued("s3", 2, "top_mutated_genes", "server", "High risk cases, top 3"),
        queued("s4", 2, "rank_top_n", "exec", "Top 3 by mutated cases, ties disclosed"),
    ]
    return [*steps, queued("s5", 3, "save_cohorts", "client", HIGH_NAME)] if saves else steps


def high_cohort(save: Literal["proposed", "reused"]) -> CohortView:
    reused = save == "reused"
    return CohortView(
        ref="high", role="planned", source="planned", name=HIGH_NAME, definition_text=HIGH_TEXT, def_sha256=def_sha256(HIGH_DEF),
        case_count=None, save=save, cohort_id=HIGH_COHORT_ID if reused else None, saved_name=HIGH_NAME if reused else None,
    )


def j2_read_steps(at: float) -> list[StepRun]:
    tops: list[Fact] = []
    for k, (gene, cases) in enumerate(ONCO_GENES, start=1):
        tops += [fact(f"f.s4.top.{k}.gene", f"Rank {k} gene", gene, "gene", "s4", 2, "rank_top_n", f"/observed/rows/{k - 1}/key"),
                 fact(f"f.s4.top.{k}.cases", f"{gene} mutated cases", cases, "cases", "s4", 2, "rank_top_n", f"/observed/rows/{k - 1}/value")]
    s2 = fact("f.s2.count", "CGS high-risk cases", 339, "cases", "s2", 1, "cases_query", "/observed/count")
    return [
        StepRun("s1", at, FP_SUMMARY, 398, J1_FACTS["s1"]),
        StepRun("s2", at + 0.4, "339 case IDs, 1 page", 205, [s2]),
        StepRun("s3", at + 0.7, "3 genes", 612),
        StepRun("s4", at + 1.4, "Top 3, no ties", 1, tops),
    ]


def j2_sized(run: Run, step: StepRun) -> None:
    fingerprinted(run, step)
    if step.step_id == "s2":
        run.set_cohort("high", case_count=339, case_count_fact_id="f.s2.count")


def j2_open(stream: Stream, at: float, *, saves: bool, signature: str = J2_SIGNATURE) -> None:
    """RUN_STARTED, Planning, then the plan with its Queued steps and run cohorts."""
    run = stream.run
    stream.started()
    stream.snapshot(iso(at))
    run.plan = j2_plan_view(saves=saves, signature=signature)
    run.steps = j2_steps(saves=saves)
    run.cohorts = [scope_cohort(), high_cohort("proposed" if saves else "reused")]
    run.status = "Executing"
    stream.snapshot(iso(at + 2.3))


def j2_answer(run: Run, limitations: Sequence[Limitation]) -> None:
    """J2's code-written parts (Observed, Limitations, the OncoMatrix card), at Answering."""
    f = by_id(run.facts)
    run.status = "Answering"
    run.observed = [
        sentence("The cohort has ", f["f.s2.count"], " cases."),
        sentence("The most frequently mutated genes are ", f["f.s4.top.1.gene"], " (", f["f.s4.top.1.cases"], " of ", f["f.s2.count"],
                 " cases), ", f["f.s4.top.2.gene"], " (", f["f.s4.top.2.cases"], ") and ", f["f.s4.top.3.gene"], " (", f["f.s4.top.3.cases"], ")."),
    ]
    run.limitations = list(limitations)
    request: dict[str, JsonValue] = {"filter0": case_filter(high_risk_ids()), "geneCount": 3}
    observed: dict[str, JsonValue] = {"genes": [{"gene": g, "cases": c} for g, c in ONCO_GENES]}
    pinned = derive_request_pinned("oncomatrix", request, observed)
    run.viz_specs = [VizSpec(
        viz_id="v1", app_id="OncoMatrix", launcher="oncomatrix", cites_steps=["s3"], pinned=pinned, pinned_hash=pinned_hash(pinned),
        # viz.oncomatrix.title over the cohort's label: its name without " (ORION)" (TOOLS.md 8).
        title="Top mutated genes: CGS high-risk", caption=None,
        request_pinned_hash=pinned_hash(derive_request_pinned("oncomatrix", request, observed)),
        frozen=FrozenTable(columns=["Gene", "Mutated cases"], rows=[[chip(f[f"f.s4.top.{k}.gene"]), chip(f[f"f.s4.top.{k}.cases"])] for k in (1, 2, 3)], note=None),
        cohort_refs=["high"],
    )]


def j2_model_text(run: Run) -> None:
    f = by_id(run.facts)
    run.interpretation = [model_sentence(
        ["Among the profiled panel genes, ", f["f.s4.top.1.gene"], " is the most often mutated in this cohort; frequency alone does not show a driver role."],
        [f["f.s4.top.1.gene"], f["f.s4.top.1.cases"]],
    )]
    run.hypotheses = [model_sentence(["Does this ranking differ from the remaining cases?"], [f["f.s4.top.1.cases"]])]
    run.follow_ups = [FollowUp(label="Top mutated genes in all other cases",
                               question="Top 3 most frequently mutated genes in CGS standard-risk patients", kind="top_mutated_genes")]
    run.open_links = [NearestApp.of("MutationFrequency")]


def build_oncomatrix() -> str:
    """J1's follow-up chip in J1's thread, with CGS high-risk already saved: reused, no save step."""
    run = Run(thread_id=hexid("j1-complete/thread"), run_id=hexid("oncomatrix/run"), question=ONCO_QUESTION)
    stream = Stream(run)
    j2_open(stream, 20.1, saves=False, signature=fake_sha("oncomatrix-plan-v1"))
    run_steps(stream, j2_read_steps(22.5), facts_live=True, on_finish=j2_sized)
    j2_answer(run, [PANEL_RANKING])
    stream.snapshot(iso(24.0))
    j2_model_text(run)
    end_with_answer(stream, 27.2)
    return stream.text()


def j2_pending(nonce: str) -> SaveCohortsArgs:
    cohort = PendingCohort(
        ref="high", name=HIGH_NAME, definition_text=HIGH_TEXT, definition=HIGH_DEF, def_sha256=def_sha256(HIGH_DEF), case_count=339,
        eligible_count=None, reuse=None, case_count_fact_id="f.s2.count", eligible_count_fact_id=None,
    )
    return SaveCohortsArgs(kind="confirm", nonce=nonce, plan_signature=J2_SIGNATURE, plan_version=1, step_id="s5", cohorts=[cohort])


def j2_run(seed: str = "j2") -> Run:
    return Run(thread_id=hexid(f"{seed}/thread"), run_id=hexid(f"{seed}/run"), question=ONCO_QUESTION)


def j2_pause(run: Run, nonce: str) -> Stream:
    """The ask turn: plan, the four read steps, then the save pause (eligibleCount null) and its tool call."""
    stream = Stream(run)
    j2_open(stream, 0.1, saves=True)
    run_steps(stream, j2_read_steps(2.5), facts_live=True, on_finish=j2_sized)
    args = j2_pending(nonce)
    run.status = "AwaitingConfirmation"
    run.set_step("s5", status="AwaitingUser")
    run.pending = ConfirmPending.model_validate(args.model_dump())
    stream.snapshot(iso(4.0))
    stream.save_cohorts_call(args)
    stream.finished("AwaitingConfirmation")
    return stream


def j2_confirm(run: Run, *, decision: Literal["create", "dont_save"], start: float = 30.0) -> Stream:
    """The confirm turn: s5 Running (Create) or Skipped (Don't save), then Observed, the card, Complete."""
    stream = Stream(run)
    stream.started()
    run.status, run.pending = "Executing", None
    limitations = [PANEL_RANKING]
    if decision == "create":
        run.set_step("s5", status="Running")
        stream.snapshot(iso(start))
        stream.add(StepStartedFrame(type="STEP_STARTED", step_name="s5"))
        stream.add(StepFinishedFrame(type="STEP_FINISHED", step_name="s5"))
        run.set_step("s5", status="Done", output_summary="1 cohort saved and read back", duration_ms=5)
        run.set_cohort("high", save="saved", cohort_id=HIGH_COHORT_ID, saved_name=HIGH_NAME)
    else:
        run.set_step("s5", status="Skipped", output_summary="Not saved: you chose Don't save", code="WRITE_SKIPPED")
        run.set_cohort("high", save="skipped")
        run.limitations = [WRITE_SKIPPED_ONE]
        stream.snapshot(iso(start))
        limitations.append(WRITE_SKIPPED_ONE)
    j2_answer(run, limitations)
    stream.snapshot(iso(start + 0.1))
    j2_model_text(run)
    end_with_answer(stream, start + 3.2)
    return stream


def j2_variant(seed: str) -> Run:
    """A J2 run brought to its save pause silently, for a fixture that starts there."""
    run = j2_run(seed)
    j2_pause(run, hexid(f"{seed}/nonce-1"))
    return run


# --- The largest views the contract allows (contract "Size: 32 KB"; tests/test_contract.py) -------------------


def largest_complete_view() -> RunView:
    """J3 at its widest: top 100 asked (VIEW_RANK_CAP shown), 4,000 DE samples, the composer's maximum text."""
    run = j3_variant("largest")
    run.status, run.pending = "Executing", None
    run.set_step("s8", status="Done", output_summary="2 cohorts saved and read back", duration_ms=6)
    genes = [(f"GENE{k:05d}", 1.0 + k / 1000, k * 1e-9) for k in range(1, VIEW_RANK_CAP + 1)]
    run.facts += [
        fact("f.s9.tested", "Genes tested", 19_871, "genes", "s9", 6, "de_run", "/observed/totals/tested"),
        fact("f.s9.significant", "Genes past the thresholds", 1_204, "genes", "s9", 6, "de_run", "/observed/totals/significant"),
        *top_facts("s10", 6, genes),
        fact("f.s10.count", "Ranked genes", MAX_TOP_N_DE, "genes", "s10", 6, "rank_top_n", "/observed/count"),
    ]
    for sid in ("s9", "s10"):
        run.set_step(sid, status="Done", output_summary="x" * 240, duration_ms=1)
    run.status = "Complete"
    run.observed = de_observed(run, WT_NAME, MUT_NAME, VIEW_RANK_CAP, run.fact("f.s10.count"))
    run.limitations = [J3_PANEL, Limitation(code="TIES_AT_CUTOFF", text="y" * 300, unblock="z" * 200)]
    request: dict[str, JsonValue] = {
        "groups": [{"name": WT_NAME, "sampleIds": list(range(1, 2001))}, {"name": MUT_NAME, "sampleIds": list(range(2001, 4001))}],
        "method": "wilcoxon", "thresholds": J3_THRESHOLDS.model_dump(mode="json"),
    }
    run.viz_specs = [de_viz(run, request, "deDirect", VIEW_RANK_CAP)]
    f = by_id(run.facts)
    # The composer's caps: 4 interpretation and 3 hypothesis statements of up to 300 characters each.
    words = "the expression of this gene differs between the groups in this synthetic dataset and "
    gene = f["f.s10.top.1.gene"]
    run.interpretation = [model_sentence([gene, " " + (words * 6)[: 300 - len(str(gene.value)) - 1]], [gene]) for _ in range(4)]
    run.hypotheses = [model_sentence(["Is ", gene, " " + (words * 6)[: 300 - len(str(gene.value)) - 5] + "?"], [gene]) for _ in range(3)]
    run.follow_ups = [FollowUp(label="l" * 60, question="q" * 200, kind="differential_expression") for _ in range(3)]
    run.open_links = [NearestApp.of("GeneExpression"), NearestApp.of("DE"), NearestApp.of("CohortComparison")]
    return run.view(iso(99.0))


def largest_pause_view() -> RunView:
    """Six planned cohorts of 10,000 case ids each, waiting for Create."""
    run = j3_variant("largest-pause")
    ids = [f"SYNTH-CASE-{n:05d}" for n in range(1, 60_001)]
    cohorts: list[PendingCohort] = []
    for k in range(6):
        definition = CaseIdsDefinition(kind="case_ids", case_ids=ids[k * 10_000 : (k + 1) * 10_000])
        cohorts.append(PendingCohort(
            ref=f"group_{k + 1}", name=f"Group {k + 1} (ORION)", definition_text="A case-ID list", definition=definition,
            def_sha256=def_sha256(definition), case_count=10_000, eligible_count=None, reuse=None,
        ))
        run.cohorts.append(CohortView(
            ref=f"group_{k + 1}", role="planned", source="planned", name=f"Group {k + 1} (ORION)", definition_text="A case-ID list",
            def_sha256=def_sha256(definition), case_count=10_000, save="proposed", cohort_id=None, saved_name=None,
        ))
    run.pending = ConfirmPending(kind="confirm", nonce=J3_NONCE, plan_signature=J3_SIGNATURE, plan_version=1, step_id="s8", cohorts=cohorts)
    return run.view(iso(99.0))


# --- Vectors: pinned hashes, cohort definitions, request-pinned derivation, fingerprints, fact formats --------


def build_pinned_vectors() -> str:
    pins: list[tuple[str, dict[str, JsonValue]]] = [
        ("survival, all cases", {"kind": "survival", "filter0": None, "term": "overall_survival", "overlay": "cgs_risk_category"}),
        ("oncomatrix, case-ID scope, gene order kept", {"kind": "oncomatrix", "filter0": {"and": [{"in": {"case_id": ["SYNTH-CASE-0003", "SYNTH-CASE-0001"]}}]}, "genes": ["KRAS", "NRAS", "DIS3"]}),
        ("deDirect, unsorted sample ids, decimal pin, non-ASCII name", {"kind": "deDirect", "groups": [{"name": "KRAS no mutation", "sampleIds": [9, 2, 5]}, {"name": "KRAS mutated – café", "sampleIds": [7, 1, 3]}], "method": "wilcoxon", "volcano": {"foldChangeCutoff": 0.3, "pValueType": "adjusted"}}),
        ("deInput, saved cohorts", {"kind": "deInput", "groups": [
            {"name": "KRAS no mutation (ORION)", "cohortId": "c-0001", "filter0": {"and": [{"in": {"case_id": ["SYNTH-CASE-0002"]}}]}},
            {"name": "KRAS mutated (ORION)", "cohortId": "c-0002", "filter0": {"and": [{"in": {"case_id": ["SYNTH-CASE-0001"]}}]}},
        ]}),
    ]
    vectors: list[JsonValue] = []
    for name, raw in pins:
        pinned = PINNED.validate_python(raw)
        vectors.append({"name": name, "pinned": pinned.model_dump(mode="json"), "canonical": canonical_pinned(pinned), "sha256": pinned_hash(pinned)})
    return pretty({"schema": "orion.viz-pinned-vectors/v1", "vectors": vectors})


def build_request_pinned_vectors() -> str:
    """derive_request_pinned per launcher: the cited call's recorded request (and output) to the pinned form."""
    small: dict[str, JsonValue] = {"groups": [
        {"name": "KRAS not detected (ORION)", "sampleIds": [9, 2, 5], "cohortId": "c-0001", "filter0": case_filter(["SYNTH-CASE-0002", "SYNTH-CASE-0005", "SYNTH-CASE-0009"])},
        {"name": "KRAS mutated (ORION)", "sampleIds": [7, 1, 3], "cohortId": "c-0002", "filter0": case_filter(["SYNTH-CASE-0001", "SYNTH-CASE-0003", "SYNTH-CASE-0007"])},
    ], "method": "wilcoxon"}
    cases: list[tuple[str, PinnedKind, JsonValue, JsonValue]] = [
        ("survival_compare on all cases: scope null, the split field's twin as overlay", "survival", J1_S4_REQUEST, None),
        ("survival_compare on a filtered scope: the scope filter becomes filter0", "survival",
         {**J1_S4_REQUEST, "scope": {"and": [{"in": {"demographic.race": ["black or african american"]}}]}}, None),
        ("top_mutated_genes: filter0 from the request, genes in the output's order", "oncomatrix",
         {"filter0": case_filter(["SYNTH-CASE-0003", "SYNTH-CASE-0001"]), "geneCount": 3},
         {"genes": [{"gene": "KRAS", "cases": 2}, {"gene": "NRAS", "cases": 1}, {"gene": "DIS3", "cases": 1}]}),
        ("de_run, direct: names and sample ids; thresholds give the volcano pins (-log10 0.05 = 1.3)", "deDirect",
         {**small, "thresholds": J3_THRESHOLDS.model_dump(mode="json")}, None),
        ("de_run, direct, no thresholds: no volcano", "deDirect", small, None),
        ("de_run on saved cohorts: names, cohort ids and filters", "deInput", small, None),
    ]
    vectors: list[JsonValue] = []
    for name, launcher, request, observed in cases:
        assert isinstance(request, dict)
        pinned = derive_request_pinned(launcher, request, observed)
        vector: dict[str, JsonValue] = {"name": name, "launcher": launcher, "request": request}
        if observed is not None:
            vector["observed"] = observed
        vector |= {"pinned": pinned.model_dump(mode="json"), "requestPinnedHash": pinned_hash(pinned)}
        vectors.append(vector)
    return pretty({"schema": "orion.request-pinned-vectors/v1", "vectors": vectors})


def build_fingerprint_vectors() -> str:
    info = FP_HEALTH["versionInfo"]
    assert isinstance(info, dict)
    restarted: dict[str, JsonValue] = {**FP_HEALTH, "versionInfo": {**info, "launchdate": "Thu Nov 05 2026 08:00:00"}}
    reseeded: list[dict[str, JsonValue]] = [{**FP_RANKING[0], "cases": 479}, *FP_RANKING[1:]]
    cases: list[tuple[str, dict[str, JsonValue], dict[str, JsonValue], dict[str, JsonValue], list[dict[str, JsonValue]]]] = [
        ("the local stack", FP_STATUS, FP_VERSION, FP_HEALTH, FP_RANKING),
        ("a restart (new launchdate, same csrf) does not change the fingerprint", {**FP_STATUS, "csrf": "rotated"}, FP_VERSION, restarted, FP_RANKING),
        ("one count off in the ranking changes it", FP_STATUS, FP_VERSION, FP_HEALTH, reseeded),
    ]
    vectors: list[JsonValue] = []
    for name, status, version, health, ranking in cases:
        document = fingerprint_document(status, version, health, ranking)
        rows: list[JsonValue] = list(ranking)
        vectors.append({
            "name": name, "sources": {"sandboxStatus": status, "analysisVersion": version, "healthcheck": health, "ranking": rows},
            "document": document, "canonical": canonical_json(document), "sha256": fingerprint_sha256(document),
        })
    return pretty({"schema": "orion.fixture-fingerprint-vectors/v1", "vectors": vectors})


def build_fact_format_vectors() -> str:
    cases: list[tuple[FactValue, str]] = [
        (339, "cases"), (1095, "days"), (1_204_567, "genes"), (1095.5, "days"), (2.0, "cases"), (0.125, "days"),
        (7.1e-60, "p"), (2.1e-09, "p"), (8.149e-06, "p"), (9.96e-05, "p"), (0.0342, "p"), (0.05, "p"), (0.00105, "p"), (1.0, "p"), (0, "p"),
        (1.12, "log2FC"), (-0.87, "log2FC"), (0.125, "log2FC"), (-0.004, "log2FC"), (2, "log2FC"),
        ("DIS3", "gene"), ("KRAS", "gene"),
    ]
    vectors: list[JsonValue] = [{"value": value, "unit": unit, "text": format_fact(value, unit)} for value, unit in cases]
    about = "format_fact(value, unit): the one way a fact value is written in Observed, model text and TEXT_MESSAGE."
    return pretty({"schema": "orion.fact-format-vectors/v1", "about": about, "vectors": vectors})


def _fs(root: dict[str, JsonValue], mode: str = "and") -> dict[str, JsonValue]:
    return {"mode": mode, "root": root}


def build_cohort_def_vectors() -> str:
    ids: list[JsonValue] = ["SYNTH-CASE-0010", "SYNTH-CASE-0003", "SYNTH-CASE-0001", "SYNTH-CASE-0003"]
    race = "demographic.race"
    cases: list[tuple[str, str, JsonValue]] = [
        ("case IDs, unsorted, one duplicate", "definition", {"kind": "case_ids", "caseIds": ids}),
        ("the same case IDs as the platform's case-list FilterSet (case_id in)", "filterSet",
         _fs({"case_id": {"operator": "in", "field": "case_id", "operands": ids}})),
        ("the same case IDs as cases.case_id includes, with legacy {cases.case_id: id} operands", "filterSet",
         _fs({"cases.case_id": {"operator": "includes", "field": "cases.case_id", "operands": [{"cases.case_id": i} for i in ids]}})),
        ("a filter definition with one case_id clause becomes a case-ID list", "definition",
         {"kind": "filter", "clauses": [{"field": "case_id", "op": "in", "values": ids}]}),
        ("J2 race cohort as a definition", "definition", {"kind": "filter", "clauses": [{"field": race, "op": "in", "values": ["black or african american"]}]}),
        ("J2 race cohort as the facet FilterSet", "filterSet", _fs({race: {"operator": "includes", "field": race, "operands": ["black or african american"]}})),
        ("two fields in reverse order, values unsorted", "filterSet", _fs({
            "diagnoses.iss_stage": {"operator": "includes", "field": "diagnoses.iss_stage", "operands": ["Stage III", "Stage I"]},
            race: {"operator": "includes", "field": race, "operands": ["white", "asian"]},
        })),
        ("a numeric range is an and of two clauses; 65.0 prints as 65", "filterSet", _fs({
            "diagnoses.age_at_diagnosis": {"operator": "and", "operands": [
                {"operator": "<", "field": "diagnoses.age_at_diagnosis", "operand": 75.5},
                {"operator": ">=", "field": "diagnoses.age_at_diagnosis", "operand": 65.0},
            ]},
        })),
        ("= becomes in", "filterSet", _fs({"diagnoses.iss_stage": {"operator": "=", "field": "diagnoses.iss_stage", "operand": "Stage II"}})),
        ("= and includes of one value hash alike", "filterSet", _fs({"diagnoses.iss_stage": {"operator": "includes", "field": "diagnoses.iss_stage", "operands": ["Stage II"]}})),
        ("a case-ID clause beside another clause stays a filter", "filterSet", _fs({
            "case_id": {"operator": "in", "field": "case_id", "operands": ids[:2]},
            race: {"operator": "includes", "field": race, "operands": ["white"]},
        })),
        ("all cases: no filters", "filterSet", _fs({})),
        ("non-ASCII values stay unescaped", "definition", {"kind": "filter", "clauses": [{"field": "project.project_id", "op": "in", "values": ["MMRF – café", "MMRF-COMMPASS"]}]}),
        ("unreadable: OR across two fields never matches", "filterSet", _fs({
            race: {"operator": "includes", "field": race, "operands": ["white"]},
            "diagnoses.iss_stage": {"operator": "includes", "field": "diagnoses.iss_stage", "operands": ["Stage I"]},
        }, mode="or")),
        ("unreadable: an excludes filter never matches", "filterSet", _fs({race: {"operator": "excludes", "field": race, "operands": ["white"]}})),
        ("unreadable: a number outside the canonical range (0.00005) has no portable hash", "filterSet",
         _fs({"diagnoses.age_at_diagnosis": {"operator": ">=", "field": "diagnoses.age_at_diagnosis", "operand": 0.00005}})),
    ]
    vectors: list[JsonValue] = []
    for name, kind, value in cases:
        definition: CaseIdsDefinition | FilterDefinition | None
        if kind == "definition":
            definition = CANONICAL_DEFINITION.validate_python(value)
        else:
            definition = definition_from_filter_set(FilterSet.model_validate(value))
        canonical = None if definition is None else canonical_definition(definition)
        digest = None if definition is None else def_sha256(definition)
        vectors.append({"name": name, kind: value, "canonical": canonical, "sha256": digest})
    about = (
        "defSha256 test vectors. Each vector has a `definition` (the agent's canonical cohort definition, "
        "possibly unnormalized) or a `filterSet` (a saved cohort's stored filters, as the browser reads them). "
        "Both sides must reproduce `canonical` and `sha256`; null means the filters cannot be read and never match."
    )
    return pretty({"schema": "orion.cohort-def-vectors/v1", "about": about, "vectors": vectors})


# --- Request examples: one per action, each naming the run of the fixture it belongs to ---------------------


def all_cases_context() -> dict[str, JsonValue]:
    return {"active": {"id": None, "name": "All cases", "filters": {"mode": "and", "root": {}}, "caseCount": 1000}, "saved": []}


def build_request_examples() -> str:
    j1t, j1r = hexid("j1-complete/thread"), hexid("j1-complete/run")
    j3t, j3r = hexid("j3/thread"), hexid("j3/run")
    saved: list[JsonValue] = [
        {"ref": "kras_wt", "cohortId": WT_COHORT_ID, "name": WT_NAME, "defSha256": def_sha256(WT_DEF), "reused": False},
        {"ref": "kras_mut", "cohortId": MUT_COHORT_ID, "name": MUT_NAME, "defSha256": def_sha256(MUT_DEF), "reused": False},
    ]
    report: JsonValue = {"runId": j1r, "vizId": "v1", "mounted": True, "error": None, "pinnedHashVerified": True}

    def user(seed: str, text: str) -> list[JsonValue]:
        return [{"id": hexid(seed), "role": "user", "content": text}]

    examples: list[tuple[str, str, str, list[JsonValue], dict[str, JsonValue]]] = [
        ("warmup when the Assistant opens", j1t, hexid("warmup/run"), [], {"action": "warmup"}),
        ("ask, J3", j3t, j3r, user("j3/message", J3_QUESTION),
         {"action": "ask", "question": J3_QUESTION, "cohortContext": all_cases_context()}),
        ("ask, J2: top mutated genes in a planned cohort, saved after one confirmation", hexid("j2/thread"), hexid("j2/run"),
         user("j2/message", ONCO_QUESTION), {"action": "ask", "question": ONCO_QUESTION, "cohortContext": all_cases_context()}),
        ("ask, the follow-up chip after J1 with CGS high-risk already saved (reused, no save), carrying the J1 card's viz report",
         j1t, hexid("oncomatrix/run"), user("oncomatrix/message", ONCO_QUESTION),
         {"action": "ask", "question": ONCO_QUESTION, "cohortContext": high_saved_context(), "vizReports": [report]}),
        ("ask that first cancels the paused run (cancelWaiting)", hexid("cancel-waiting/thread"), hexid("cancel-waiting/new"),
         user("cancel-waiting/message", CLARIFY_QUESTION),
         {"action": "ask", "question": CLARIFY_QUESTION, "cohortContext": all_cases_context(), "cancelWaiting": hexid("cancel-waiting/old")}),
        ("cancel (Stop)", hexid("j1-stop/thread"), hexid("j1-stop/run"), [], {"action": "cancel"}),
        ("clarify_answer", hexid("clarify/thread"), hexid("clarify/run"), [], {"action": "clarify_answer", "optionId": CGS}),
        ("consent, accepted", hexid("consent/thread"), hexid("consent/run"), [], {"action": "consent", "accept": True}),
        ("consent, declined", hexid("consent/thread"), hexid("consent/run"), [], {"action": "consent", "accept": False}),
        ("confirm, Create with make-active", j3t, j3r, [], {"action": "confirm", "nonce": J3_NONCE, "decision": "create", "saved": saved, "makeActive": "kras_mut"}),
        ("confirm, Create of J2's one cohort", hexid("j2/thread"), hexid("j2/run"), [],
         {"action": "confirm", "nonce": J2_NONCE, "decision": "create", "makeActive": None, "saved": [
             {"ref": "high", "cohortId": HIGH_COHORT_ID, "name": HIGH_NAME, "defSha256": def_sha256(HIGH_DEF), "reused": False}]}),
        ("confirm, Don't save", hexid("j3-dont-save/thread"), hexid("j3-dont-save/run"), [],
         {"action": "confirm", "nonce": variant_nonce("j3-dont-save"), "decision": "dont_save", "saved": [], "makeActive": None}),
        ("edit_plan, scope chip (altScopeChoice or a scope picked on the plan card)", j3t, j3r, [],
         {"action": "edit_plan", "field": "scope", "value": {"source": "all_cases"}, "cohortContext": all_cases_context()}),
        ("edit_plan, method", j3t, j3r, [], {"action": "edit_plan", "field": "method", "value": "edgeR"}),
        ("edit_plan, top N", j3t, j3r, [], {"action": "edit_plan", "field": "topN", "value": 20}),
        ("edit_plan, thresholds", j3t, j3r, [], {"action": "edit_plan", "field": "thresholds", "value": {"pValueCutoff": 0.01, "pValueType": "adjusted", "foldChangeCutoff": 1.0}}),
        ("edit_plan, rename at the save pause", hexid("j3-edit/thread"), hexid("j3-edit/run"), [],
         {"action": "edit_plan", "field": "cohortName", "value": WT_RENAMED, "ref": "kras_wt"}),
        ("retry_step", hexid("j1-retry/thread"), hexid("j1-retry/run"), [], {"action": "retry_step", "stepId": "s4"}),
        ("viz_report, standalone (no turn in flight)", j1t, j1r, [], {"action": "viz_report", "vizId": "v1", "mounted": False, "error": "bindProteinPaint rejected: termdb/survival 503", "pinnedHashVerified": None}),
    ]
    out: list[JsonValue] = []
    for name, thread_id, run_id, messages, orion in examples:
        body: dict[str, JsonValue] = {
            "threadId": thread_id, "runId": run_id, "state": {}, "messages": messages, "tools": [], "context": [],
            "forwardedProps": {"orion": orion},
        }
        RunAgentInputModel.model_validate(body).orion()
        out.append({"name": name, "body": body})
    return pretty({"schema": "orion.request-examples/v1", "examples": out})


# --- J1 run records, as the record route returns them -----------------------------------------------------------


def _call(
    run_id: str, step_id: str, n: int, tool: str, transport: Transport, started: float, request: JsonValue, observed: JsonValue,
    elapsed: int, provenance: list[Provenance], http_status: int | None, *, attempt: int = 1, status: ToolStatus = "ok",
    error: str | None = None, limitation: Code | None = None,
) -> CallRecord:
    """One attempt, its request and Observed written as the tool writes them: through the catalog's
    Input and Observed models (tools/schemas.py), so requestSha256 and responseSha256 are what a
    tool's own output hashes to."""
    spec = catalog.tool_spec(tool)
    request = spec.input_model.model_validate(request).model_dump(mode="json")
    if observed is not None:
        assert spec.observed_model is not None, f"{tool} records its Observed"
        observed = spec.observed_model.model_validate(observed).model_dump(mode="json")
    return CallRecord(
        call_id=call_id(run_id, 1, step_id, n), plan_version=1, step_id=step_id, tool=tool, transport=transport, attempt=attempt,
        started_at=iso(started), request=request, request_sha256=sha256_hex(canonical_json(request, portable=False)),
        http_status=http_status, response_sha256=None if observed is None else sha256_hex(canonical_json(observed, portable=False)),
        elapsed_ms=elapsed, status=status, observed=observed, warnings=[], limitation=limitation, error=error, provenance=provenance,
    )


def _prov(method: str, path: str, seed: str, ms: int, status: int | None = 200) -> Provenance:
    return Provenance.model_validate(
        {"method": method, "path": path, "requestSha256": fake_sha(f"{seed}/req"), "httpStatus": status,
         "responseSha256": None if status is None else fake_sha(f"{seed}/res"), "elapsedMs": ms}
    )


def _points(curve: KmCurve) -> list[JsonValue]:
    return [point.model_dump(mode="json") for point in curve.points]


J1_S4_OBSERVED: dict[str, JsonValue] = {
    "groups": [
        {"label": "High risk", "cases": 339, "events": 299, "curve": _points(J1_CURVES[0])},
        {"label": "Standard risk", "cases": 661, "events": 300, "curve": _points(J1_CURVES[1])},
    ],
    "pValue": J1_P, "engine": catalog.ENGINE_GEN3_ANALYSIS,
}


def j1_reads(rid: str) -> list[CallRecord]:
    """s1–s3 of a J1 run: the fingerprint and both counts."""
    return [
        _call(rid, "s1", 1, "fixture_fingerprint", "in_process", 2.6, {}, {"document": FP_DOC, "fingerprintSha256": FP_SHA}, 412, [
            _prov("GET", "/sandbox/status", "s1a", 21), _prov("GET", "/analysis/v0/_version", "s1b", 18),
            _prov("GET", "/protein-paint/healthcheck", "s1c", 25), _prov("POST", "/protein-paint/termdb/topMutatedGenes", "s1d", 330),
        ], 200),
        _call(rid, "s2", 1, "cohort_count", "in_process", 3.2, {"filter": _gql("High risk")}, {"count": 339}, 188,
              [_prov("POST", "/guppy/graphql", "s2", 180)], 200),
        _call(rid, "s3", 1, "cohort_count", "in_process", 3.7, {"filter": _gql("Standard risk")}, {"count": 661}, 171,
              [_prov("POST", "/guppy/graphql", "s3", 165)], 200),
    ]


def j1_survival(rid: str, n: int, started: float) -> list[CallRecord]:
    return [
        _call(rid, "s4", n, "survival_compare", "in_process", started, J1_S4_REQUEST, J1_S4_OBSERVED, 1290,
              [_prov("POST", "/analysis/v0/survival/", f"s4{x}{n}", 400) for x in "abc"], 200),
        _call(rid, "s5", 1, "derive_km_median", "in_process", started + 1.3, {"curves": "s4"}, J1_MEDIANS.model_dump(mode="json"), 3, [], None),
    ]


def j1_compiled(plan: PlanView) -> CompiledPlan:
    scope = ResolvedScope(source="active_cohort", name="All cases", case_count=1000, clauses=[])
    specs = [
        StepSpec(id="s1", row=1, tool="fixture_fingerprint", kind="server", folded=True, input_summary="sandbox status, versions, ranking", args={}, depends_on=[]),
        StepSpec(id="s2", row=1, tool="cohort_count", kind="server", folded=False, input_summary="CGS risk category = High risk", args={"filter": _gql("High risk")}, depends_on=[]),
        StepSpec(id="s3", row=1, tool="cohort_count", kind="server", folded=False, input_summary="CGS risk category = Standard risk", args={"filter": _gql("Standard risk")}, depends_on=[]),
        StepSpec(id="s4", row=2, tool="survival_compare", kind="server", folded=False, input_summary="2 groups, Kaplan–Meier", args=J1_S4_REQUEST, depends_on=["s2", "s3"]),
        StepSpec(id="s5", row=2, tool="derive_km_median", kind="exec", folded=False, input_summary="2 curves", args={"curves": "s4"}, depends_on=["s4"]),
    ]
    return CompiledPlan(
        version=1, signature=plan.signature, scope=scope, rows=plan.rows, steps=specs, assumptions=plan.assumptions,
        limitations=[], writes=False, paraphrase=plan.paraphrase, editable=plan.editable,
    )


# FactRecord.engine is the step's tool's engine (ToolSpec.engine, TOOLS.md 1 "Engines"); null for ORION's functions.
J1_ENGINES: dict[str, str | None] = {spec.id: catalog.tool_spec(spec.tool).engine for spec in j1_compiled(j1_plan_view()).steps}


def j1_record(run: Run, final: RunView, calls: list[CallRecord], actions: list[ActionLogEntry], composer_at: float) -> str:
    rid = run.run_id
    used = {c.step_id: c for c in calls if c.status == "ok"}  # the attempt each fact cites: the last that succeeded
    facts = [
        FactRecord.model_validate(
            {**f.model_dump(mode="json"), "planVersion": 1, "callId": used[f.step_id].call_id,
             "requestSha256": used[f.step_id].request_sha256, "responseSha256": used[f.step_id].response_sha256,
             "engine": J1_ENGINES[f.step_id]}
        )
        for f in final.facts
    ]
    assert final.plan is not None
    plan_ir = ir.Plan(
        kind="plan",
        analyses=[ir.SurvivalCompare(kind="survival_compare", split_term=CGS, split_values=["high risk", "standard risk"])],
    )

    def model_call(role: ModelRole, effort: Literal["low", "medium"], start: float, ms: int, inp: int, out: int) -> ModelCallRecord:
        return ModelCallRecord(
            call_id=model_call_id(rid, role, 1, None if role == "planner" else 1), role=role, model=PLANNER_MODEL,
            fallback=False, effort=effort, attempt=1, started_at=iso(start), elapsed_ms=ms, outcome="ok", stop_reason="end_turn",
            input_tokens=inp, output_tokens=out, cache_read_tokens=7120, cache_write_tokens=0,
        )

    base = final.cohorts[0]
    record = RunRecord(
        thread_id=run.thread_id, run_id=rid, revision=final.revision, status=final.status, code=None, question=J1_QUESTION,
        created_at=iso(0.1), updated_at=final.updated_at,
        environment=Environment(mode="L", planner="bedrock", composer="bedrock", executor="engine", agent_version="0.2.0",
                                tool_transport="in_process", fingerprint_sha256=FP_SHA),
        lease=None,
        request=AskAction.model_validate({"action": "ask", "question": J1_QUESTION, "cohortContext": all_cases_context()}),
        planner_output=plan_ir,
        plan_versions=[j1_compiled(final.plan)],
        plan_history=[PlanHistoryEntry(version=1, cause="ask", at=iso(2.6), ir=plan_ir)],
        model_calls=[model_call("planner", "low", 0.2, 2310, 7840, 412), model_call("composer", "medium", composer_at, 3240, 8215, 655)],
        steps=final.steps, calls=calls, facts=facts,
        cohorts=[CohortRef.model_validate({**base.model_dump(mode="json"), "definition": ALL_CASES_DEF.model_dump(mode="json")})],
        actions=actions,
        answer=AnswerRecord(
            observed=final.observed, limitations=final.limitations, interpretation=final.interpretation, hypotheses=final.hypotheses,
            withheld_reason=final.withheld_reason, viz_specs=final.viz_specs, follow_ups=final.follow_ups, open_links=final.open_links,
        ),
        run_view=final,
    )
    if record.view() != final:
        raise ValueError("the record does not project to its own final view")
    return pretty(record.model_dump(mode="json"))


def j1_complete_record(run: Run, final: RunView) -> str:
    calls = [*j1_reads(run.run_id), *j1_survival(run.run_id, 1, 4.2)]
    return j1_record(run, final, calls, [ActionLogEntry(at=iso(0.0), action="ask", outcome="accepted", code=None)], 6.5)


def j1_retry_record(run: Run, final: RunView) -> str:
    """The retried run: s4's two timed-out attempts (c1, c2) and the retry_step rerun (c3) are three calls."""
    rid = run.run_id
    timeouts = [
        _call(rid, "s4", n, "survival_compare", "in_process", started, J1_S4_REQUEST, None, 55_000,
              [_prov("POST", "/analysis/v0/survival/", f"s4t{n}", 55_000, None)], None, attempt=n, status="error",
              error="TIMEOUT: no response within 55 s", limitation="TOOL_FAILED" if n == 2 else None)
        for n, started in ((1, 4.2), (2, 63.8))
    ]
    calls = [*j1_reads(rid), *timeouts, *j1_survival(rid, 3, 200.1)]
    actions = [
        ActionLogEntry(at=iso(0.0), action="ask", outcome="accepted", code=None),
        ActionLogEntry(at=iso(199.9), action="retry_step", outcome="accepted", code=None),
    ]
    return j1_record(run, final, calls, actions, 201.7)


# --- Assembly ------------------------------------------------------------------------------------------------------

STREAMS = (
    "j1-stub", "j1-complete", "j1-partial", "j1-retry", "j1-stop", "j2-save-pause", "j2-after-confirm", "j2-dont-save",
    "j3-save-pause", "j3-after-confirm", "j3-edit-at-pause", "j3-dont-save", "j3-deinput", "oncomatrix", "clarify", "consent",
    "interrupted", "cancel-waiting",
)


def build_all() -> dict[str, str]:
    j1_run, j1_stream = j1_complete_run()
    retry_run = j1_retry_run()
    partial = j1_partial(retry_run)
    retried = j1_retry(retry_run)
    j2 = j2_run()
    j2_paused = j2_pause(j2, J2_NONCE)
    j2_after = j2_confirm(j2, decision="create")
    j3 = j3_run()
    pause = j3_pause(j3)
    j3_after = j3_confirm(j3)
    files = {
        "j1-stub.events.jsonl": build_j1_stub(),
        "j1-complete.events.jsonl": j1_stream.text(),
        "j1-partial.events.jsonl": partial.text(),
        "j1-retry.events.jsonl": retried.text(),
        "j1-stop.events.jsonl": j1_stop().text(),
        "j2-save-pause.events.jsonl": j2_paused.text(),
        "j2-after-confirm.events.jsonl": j2_after.text(),
        "j2-dont-save.events.jsonl": j2_confirm(j2_variant("j2-dont-save"), decision="dont_save").text(),
        "j3-save-pause.events.jsonl": pause.text(),
        "j3-after-confirm.events.jsonl": j3_after.text(),
        "j3-edit-at-pause.events.jsonl": j3_edit_rename(j3_variant("j3-edit")).text(),
        "j3-dont-save.events.jsonl": j3_confirm(j3_variant("j3-dont-save"), decision="dont_save").text(),
        "j3-deinput.events.jsonl": j3_confirm(j3_variant("j3-deinput"), launcher="deInput").text(),
        "oncomatrix.events.jsonl": build_oncomatrix(),
        "clarify.events.jsonl": build_clarify(),
        "consent.events.jsonl": build_consent(),
        "interrupted.events.jsonl": build_interrupted(),
        "cancel-waiting.events.jsonl": build_cancel_waiting(),
        "viz-pinned-vectors.json": build_pinned_vectors(),
        "request-pinned-vectors.json": build_request_pinned_vectors(),
        "fingerprint-vectors.json": build_fingerprint_vectors(),
        "fact-format-vectors.json": build_fact_format_vectors(),
        "cohort-def-vectors.json": build_cohort_def_vectors(),
        "request-examples.json": build_request_examples(),
        "run-record.j1-complete.json": j1_complete_record(j1_run, j1_stream.last_view()),
        "run-record.j1-retry.json": j1_retry_record(retry_run, retried.last_view()),
    }
    assert set(STREAMS) == {name.removesuffix(".events.jsonl") for name in files if name.endswith(".events.jsonl")}
    return files


J1_STUB_TS_HEADER = (
    "// Bundled copy of j1-stub.events.jsonl for fixtureTransport; a unit test keeps\n"
    "// it equal to the .jsonl file, byte for byte.\n"
    "export const J1_STUB_STREAM =\n  "
)
VIZ_FIXTURES = frozenset({"viz-pinned-vectors.json", "request-pinned-vectors.json", "fingerprint-vectors.json"})


def mirror_files(files: dict[str, str]) -> dict[str, str]:
    """The fork's copies, relative to src/features/orion: viz vectors in viz/, cohort vectors in cohort/, the rest in agui/."""
    out: dict[str, str] = {}
    for name, text in files.items():
        if name in VIZ_FIXTURES:
            out[f"viz/__fixtures__/{name}"] = text
        elif name == "cohort-def-vectors.json":
            out[f"cohort/__fixtures__/{name}"] = text
        else:
            out[f"agui/__fixtures__/{name}"] = text
    out["agui/__fixtures__/j1Stub.ts"] = J1_STUB_TS_HEADER + json.dumps(files["j1-stub.events.jsonl"], ensure_ascii=False) + ";\n"
    return out


def _sync(root: Path, files: dict[str, str], check: bool) -> list[str]:
    drift: list[str] = []
    for name, text in files.items():
        path = root / name
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        drift.append(str(path))
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return drift


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file differs")
    parser.add_argument("--mirror", type=Path, help="the fork's src/features/orion directory")
    args = parser.parse_args(argv)
    files = build_all()
    drift = _sync(HERE, files, args.check)
    if args.mirror is not None:
        drift += _sync(args.mirror, mirror_files(files), args.check)
    for path in drift:
        print(("differs: " if args.check else "wrote: ") + path)
    if not drift:
        print("all fixtures up to date")
    return 1 if args.check and drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
