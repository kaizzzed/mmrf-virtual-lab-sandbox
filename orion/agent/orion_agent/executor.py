"""The step engine (RFA rev 25, A2 "Executor", A3 "Step lifecycle", A4 "Timeout budget"; TOOLS.md 1-5).

Runs a CompiledPlan's steps in order through a ToolClient (server tools), in-process (the four
pure functions) or as the client write `save_cohorts`, whose first visit pauses the run with a
pending confirm and whose resume checks the browser's read-back. Every counted call becomes one
CallRecord per attempt; every step that finishes reports its facts (extractor) and the run cohorts
it defined in the first snapshot after it.

Guards (TOOLS.md "Guards"): a pre-guard the executor checks skips the step with its code; a `stop`
post-guard keeps the step's facts and status and skips everything that depends on it; a `report`
post-guard only adds its line. A Failed step skips its dependents too ("Not run: s4 failed"); the
run then ends Partial, a stop ends it Limited, and otherwise the run is ready to answer.

Stop (contract "cancel"): the executor races every in-flight call with `StepSession.wait_stop`;
when it fires the call is cancelled at once, recorded with an error starting "CANCELLED", and the
step is Skipped "Not run: stopped". At a step boundary a stop request (a Stop, or an edit waiting
for the boundary) ends execution; the controller skips what is left.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, JsonValue, ValidationError

from . import compiler as compiler_module
from . import ir
from .compiler import CompiledPlan, SessionLedger, StepSpec
from .events import iso_utc, utc_now
from .extractor import ExtractionError, FactExtractor
from .ids import new_id
from .interfaces import ExecutionResult, ResumeState, StepSession
from .models import (
    VIEW_RANK_CAP,
    CallRecord,
    CanonicalDefinition,
    CaseIdsDefinition,
    Code,
    CohortContext,
    CohortRef,
    ConfirmAction,
    ConfirmPending,
    FactRecord,
    FilterClause,
    FilterDefinition,
    Limitation,
    SaveCohortsArgs,
    StepOrigin,
    StepStatus,
    StepView,
    call_id,
    canonical_json,
    def_sha256,
    format_fact,
    normalize_definition,
    sha256_hex,
)
from .observed import bridge_unsafe, call_budget, step_refs, survival_count_steps, write_verify_failed
from .tools import schemas as catalog
from .tools import functions as tool_functions
from .tools import guards
from .tools.base import (
    MAX_TOOL_CALLS_PER_RUN,
    MIN_DE_ELIGIBLE,
    STEP_CAP_S,
    STEP_TIMEOUT_S,
    ExecutorFunctionName,
    ServerToolName,
    RetryHook,
    ToolCallOutcome,
    ToolClient,
)
from .tools.functions import check_subset, derive_km_median, passes_thresholds, rank_top_n, set_operation
from .tools.records import ToolCallCancelled, call_records
from .vocab import Vocabulary

log = logging.getLogger("orion.executor")

STOPPED_SUMMARY: Final = "Not run: stopped"
FAILED_SUMMARY: Final = "Not run: the run failed"
INTERRUPTED_SUMMARY: Final = "Not run: interrupted"
DECLINED_SUMMARY: Final = "Not run: you declined the substitution"
DONT_SAVE_SUMMARY: Final = "Not saved: you chose Don't save"
BUDGET_SUMMARY: Final = "Not run: the run's call budget is used up"
FINISHED: Final[frozenset[StepStatus]] = frozenset({"Done", "Empty", "Reused"})
# What a stop guard's code says on a dependent's row.
_STOP_REASONS: Final[dict[str, str]] = {
    "EMPTY_COHORT": "found no cases",
    "TOO_MANY_IDS": "matched more than 10,000 cases",
    "INELIGIBLE_GROUP": "found a group that is too small",
    "REQUEST_TOO_LARGE": "would send too large a request",
    "UNSUPPORTED_FIELD": "cannot filter ProteinPaint",
    "KG_NOT_CONNECTED": "is not connected",
    "CALL_BUDGET": "hit the call budget",
}
_FAILURE_SUMMARIES: Final[dict[str, str]] = {
    "TIMEOUT": "Timed out on both attempts",
    "CACHE_BUSY": "Busy on both attempts",
    "HTTP_ERROR": "Returned an error",
    "UPSTREAM_SHAPE": "Returned a response ORION could not read",
    "INVALID_INPUT": "Could not be sent",
    "NOT_CONNECTED": "Not connected",
    "GUARD_FAILED": "Stopped by a check",
    "CANCELLED": STOPPED_SUMMARY,
    "INTERNAL_ERROR": "Failed",
}
# The DE fallback's launcher is a deployment setting (TOOLS.md 5.6); the renderer reads it.
DeLauncher = Literal["deDirect", "deInput"]


# --- Plan helpers shared with the controller --------------------------------------------------------


def input_hashes(plan: CompiledPlan) -> dict[str, str]:
    """Each step's input hash (TOOLS.md "Input hash and Reused"): the compiler's `step_input_hashes`,
    the one implementation (StepRefs, bare step ids and dependsOn replaced by the referenced steps'
    own hashes, so a write whose cohorts' defining steps changed is never Reused)."""
    return compiler_module.step_input_hashes(plan)


def reuse_map(new: CompiledPlan, old: CompiledPlan, old_steps: Sequence[StepView]) -> dict[str, StepOrigin]:
    """The steps of `new` whose input hash equals a finished step of `old`, with the version and step
    whose calls produced it (a Reused step's own origin, never another Reused step)."""
    new_hashes, old_hashes = input_hashes(new), input_hashes(old)
    finished = {s.id: s for s in old_steps if s.status in FINISHED}
    by_hash: dict[str, StepOrigin] = {}
    for step_id, view in finished.items():
        origin = view.reused_from or StepOrigin(plan_version=old.version, step_id=step_id)
        by_hash.setdefault(old_hashes[step_id], origin)
    return {sid: by_hash[h] for sid, h in new_hashes.items() if h in by_hash}


def verify_read_back(pending: ConfirmPending, confirm: ConfirmAction) -> list[str]:
    """The contract's `confirm create` checks: every pending ref read back once, a non-empty cohortId and
    name, and the browser's defSha256 equal to the pending cohort's. Returns what failed, by cohort."""
    problems: list[str] = []
    saved = {s.ref: s for s in confirm.saved}
    for cohort in pending.cohorts:
        back = saved.get(cohort.ref)
        if back is None:
            problems.append(f"{cohort.name} was not read back")
        elif not back.cohort_id.strip() or not back.name.strip():
            problems.append(f"{cohort.name} came back without an id or a name")
        elif back.def_sha256 != cohort.def_sha256:
            problems.append(f"{cohort.name} was saved with a different definition")
    extra = sorted(set(saved) - {c.ref for c in pending.cohorts})
    problems += [f"{ref} is not a pending cohort" for ref in extra]
    return problems


def _clauses_definition(clauses: Sequence[ir.Clause]) -> FilterDefinition:
    out: list[FilterClause] = []
    for clause in clauses:
        op = "in" if clause.op == "=" else clause.op
        out.append(FilterClause(field=clause.field, op=op, values=list(clause.values)))
    return FilterDefinition(kind="filter", clauses=out)


def gen3_filter(definition: CanonicalDefinition) -> dict[str, JsonValue]:
    """A canonical definition as a Gen3 filter, `and`-wrapped (case-ID lists as `case_id_filter`)."""
    if isinstance(definition, CaseIdsDefinition):
        return catalog.case_id_filter(definition.case_ids)
    leaves: list[JsonValue] = []
    for clause in definition.clauses:
        values: list[JsonValue] = list(clause.values)
        leaves.append({clause.op: {clause.field: values if clause.op == "in" else values[0]}})
    return {"and": leaves}


def _summary(clauses: Sequence[ir.Clause], vocab: Vocabulary | None) -> str:
    parts: list[str] = []
    for clause in clauses:
        spec = vocab.field(clause.field) if vocab is not None else None
        label = spec.label if spec is not None else clause.field
        shown = ", ".join(str(v) for v in clause.values[:3]) + (f" and {len(clause.values) - 3} more" if len(clause.values) > 3 else "")
        parts.append(f"{label} {'=' if clause.op in ('in', '=') and len(clause.values) == 1 else clause.op} {shown}")
    return "; ".join(parts)


def scope_step(plan: CompiledPlan) -> str | None:
    """The step that sizes the scope: the scope's cases_query for a filtered scope, else s1 (the fingerprint)."""
    if plan.scope.clauses:
        return next((s.id for s in plan.steps if s.tool == "cases_query" and not s.depends_on), None)
    return next((s.id for s in plan.steps if s.tool == "fixture_fingerprint"), None)


def base_cohort(plan: CompiledPlan, context: CohortContext, vocab: Vocabulary | None = None) -> CohortRef:
    """The run's scope as its `base` cohort (contract "RunView": cohorts)."""
    scope = plan.scope
    definition = normalize_definition(_clauses_definition(scope.clauses))
    cohort_id: str | None = None
    if scope.source == "active_cohort":
        cohort_id = context.active.id
    elif scope.source == "saved_cohort":
        wanted = scope.name.strip().casefold()
        cohort_id = next((s.id for s in context.saved if s.name.strip().casefold() == wanted), None)
    return CohortRef(
        ref="base",
        role="scope",
        source=scope.source,
        name=scope.name,
        definition_text=_summary(scope.clauses, vocab) or "All cases, no filters",
        def_sha256=def_sha256(definition),
        case_count=scope.case_count,
        save="none",
        cohort_id=cohort_id,
        saved_name=None,
        definition=definition,
    )


def _dicts(value: JsonValue) -> list[dict[str, JsonValue]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _ref_step(value: JsonValue) -> str | None:
    return next((ref.ref for ref in step_refs(value)), None)


def derive_cohort_steps(plan: CompiledPlan, refs: Sequence[str]) -> dict[str, str]:
    """Which step defines each planned cohort, from the step tables (TOOLS.md 5) when the plan does not say.

    DE groups through sample_lists' case-ID references; a ranked cohort through top_mutated_genes'
    filter0; the tested cohort through check_subset's outer list; any other cohort, in order, to the
    remaining cohort-defining steps.
    """
    specs = {s.id: s for s in plan.steps}
    found: dict[str, str] = {}
    for de in (s for s in plan.steps if s.tool == "de_run"):
        groups = de.args.get("groups")
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict) or not isinstance(group.get("cohort"), str):
                continue
            samples = catalog.StepRef.model_validate(group["sampleIds"]) if catalog.is_step_ref(group.get("sampleIds")) else None
            source = specs.get(samples.ref) if samples is not None else None
            lists = source.args.get("groups") if source is not None else None
            if samples is None or not isinstance(lists, list):
                continue
            index = samples.pointer.split("/")[3] if samples.pointer.count("/") >= 3 else "0"
            position = int(index) if index.isdigit() else 0
            entry = lists[position] if position < len(lists) else None
            if isinstance(entry, dict):
                defining = _ref_step(entry.get("caseIds"))
                if defining is not None:
                    found.setdefault(str(group["cohort"]), defining)
    saves = [s for s in plan.steps if s.tool == "save_cohorts"]
    save_refs = [str(c["ref"]) for s in saves for c in _dicts(s.args.get("cohorts")) if isinstance(c.get("ref"), str)]
    for genes in (s for s in plan.steps if s.tool == "top_mutated_genes"):
        ranked = _ref_step(genes.args.get("filter0"))
        unmapped = [r for r in (save_refs or refs) if r not in found and r in refs]
        if ranked is not None and unmapped:
            found.setdefault(unmapped[0], ranked)
    used = set(found.values())
    for subset in (s for s in plan.steps if s.tool == "check_subset"):
        outer = subset.args.get("outer")
        unmapped = [r for r in refs if r not in found]
        if isinstance(outer, str) and outer not in used and unmapped:
            found[unmapped[0]] = outer
            used.add(outer)
    defining_tools = {"cases_query", "gene_cases", "set_operation", "cohort_count"}
    scope = scope_step(plan)
    rest = [
        s.id for s in plan.steps
        if s.tool in defining_tools and s.id not in used and s.id != scope and s.args.get("op") != "size"
    ]
    for ref in refs:
        if ref not in found and rest:
            found[ref] = rest.pop(0)
    return found


@dataclass(frozen=True)
class PlanCohorts:
    """The run cohorts a plan shows (base first, then the planned ones in plan order) and how each is sized.

    `steps` names the step whose output sizes each cohort; `ids` the case-ID reference that defines a
    case-ID cohort (a StepRef, possibly intersected); `definitions` a definition known at compile time
    (the scope, a filter cohort, mutation-tested cases).
    """

    cohorts: list[CohortRef]
    steps: dict[str, str]
    ids: dict[str, catalog.StepRef] = field(default_factory=dict)
    definitions: dict[str, CanonicalDefinition] = field(default_factory=dict)


_COHORT_KEYS: Final = frozenset(
    {"ref", "role", "source", "name", "definitionText", "defSha256", "caseCount", "save", "cohortId", "savedName", "caseCountFactId", "definition"}
)


def _as_cohort(item: object, case_count: int | None) -> CohortRef | None:
    """A compiler plan cohort as the run cohort it starts as: its own `view(case_count)` when it has one
    (compiler.PlanCohort), else its CohortRef fields."""
    view = getattr(item, "view", None)
    if callable(view):
        with contextlib.suppress(TypeError, ValueError, ValidationError):
            result = view(case_count)
            if isinstance(result, CohortRef):
                return result
    data = item.model_dump(mode="json", by_alias=True) if isinstance(item, BaseModel) else item
    if not isinstance(data, dict):
        return None
    fields: dict[str, JsonValue] = {k: v for k, v in data.items() if k in _COHORT_KEYS}
    fields.setdefault("definition", None)
    fields.setdefault("defSha256", None)
    fields.setdefault("caseCount", None)
    try:
        return CohortRef.model_validate(fields)
    except ValidationError:
        return None


def _item_step(item: object) -> str | None:
    for name in ("step_id", "defined_by", "defining_step", "step"):
        value = getattr(item, name, None)
        if isinstance(value, str):
            return value
    return None


def plan_cohorts(plan: CompiledPlan, context: CohortContext, vocab: Vocabulary | None = None) -> PlanCohorts:
    """The run cohorts of a compiled plan.

    The compiler names planned cohorts (`CompiledPlan.cohorts`, each a CohortRef-shaped item, with
    the step that defines it as `step_id`); this reads them when the plan carries them. Missing
    defining steps come from the step tables (`derive_cohort_steps`). Without compiler cohorts the
    planned ones are the proposed saves (`save_cohorts` args), named as the compiler named them.
    """
    base = base_cohort(plan, context, vocab)
    steps: dict[str, str] = {}
    scope = scope_step(plan)
    if scope is not None:
        steps["base"] = scope
    planned: list[CohortRef] = []
    ids: dict[str, catalog.StepRef] = {}
    definitions: dict[str, CanonicalDefinition] = {}
    provided = getattr(plan, "cohorts", None)
    if isinstance(provided, list) and provided:
        for item in provided:
            cohort = _as_cohort(item, plan.scope.case_count)
            if cohort is None:
                log.warning("plan cohort %r is not a run cohort; ignored", getattr(item, "ref", item))
                continue
            known = getattr(item, "definition", None)
            if isinstance(known, (CaseIdsDefinition, FilterDefinition)):
                definitions[cohort.ref] = known
                if cohort.def_sha256 is None:
                    # A definition known at compile time shows its hash on the plan snapshot (the fixtures' form).
                    cohort = cohort.model_copy(update={"definition": known, "def_sha256": def_sha256(known)})
            reference = getattr(item, "ids", None)
            if isinstance(reference, catalog.StepRef):
                ids[cohort.ref] = reference
            step = _item_step(item)
            if cohort.ref == "base":
                base = cohort
            else:
                planned.append(cohort)
            if step is not None:
                steps[cohort.ref] = step
    else:
        for save in (s for s in plan.steps if s.tool == "save_cohorts"):
            args = catalog.SaveCohortsStepArgs.model_validate(save.args)
            for c in args.cohorts:
                planned.append(
                    CohortRef(
                        ref=c.ref, role="planned", source="planned", name=c.name,
                        definition_text=f"{catalog.cohort_label(c.name)}, as a case-ID list", def_sha256=None, case_count=None,
                        save="proposed", cohort_id=None, saved_name=None, definition=None,
                    )
                )
    derived = derive_cohort_steps(plan, [c.ref for c in planned])
    for ref, step in derived.items():
        steps.setdefault(ref, step)
    if base.definition is not None:
        definitions.setdefault("base", base.definition)
    return PlanCohorts(cohorts=[base, *planned], steps=steps, ids=ids, definitions=definitions)


# --- The pure functions (A2) are tools/functions.py's: set_operation, check_subset, rank_top_n,
# derive_km_median and passes_thresholds, imported above and re-exported for the controller and tests.


# --- The session the controller gives the executor ------------------------------------------------------


@dataclass(frozen=True)
class _RetryHook:
    """tools.base.RetryHook over a plain coroutine function."""

    hook: Callable[[int, int], Awaitable[None]]

    async def __call__(self, attempt: int, backoff_ms: int) -> None:
        await self.hook(attempt, backoff_ms)


@dataclass(frozen=True)
class RunContext:
    """What a run's execution reads beyond its plan: ids, the cohort snapshot, cohorts and their defining steps."""

    run_id: str
    cohort_context: CohortContext
    cohorts: Sequence[CohortRef]
    cohort_steps: Mapping[str, str]
    cohort_ids: Mapping[str, catalog.StepRef] = field(default_factory=dict)
    cohort_definitions: Mapping[str, CanonicalDefinition] = field(default_factory=dict)
    # The thread's earlier runs: the save pause offers a ledger cohort with the same definition for reuse.
    ledger: SessionLedger | None = None


@runtime_checkable
class EngineSession(StepSession, Protocol):
    @property
    def context(self) -> RunContext: ...


class StepFailure(Exception):
    """A step that cannot be sent (its input does not resolve or validate); it fails with TOOL_FAILED."""

    def __init__(self, message: str, code: str = "INVALID_INPUT") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _State:
    plan: CompiledPlan
    ctx: RunContext
    status: dict[str, StepStatus]
    codes: dict[str, Code | None]
    outputs: dict[str, CallRecord]
    facts: list[FactRecord]
    cohorts: dict[str, CohortRef]
    calls_made: int
    rank_shown: int
    blocked: dict[str, str] = field(default_factory=dict)  # step id -> why its dependents do not run
    stop_code: Code | None = None
    limitations: list[Limitation] = field(default_factory=list)
    new_calls: dict[str, int] = field(default_factory=dict)
    # Attempts each step already made in this plan version (a retry_step rerun continues their callIds).
    prior_calls: dict[str, int] = field(default_factory=dict)


Clock = Callable[[], float]


class Engine:
    """interfaces.Executor: the real step engine (ORION_EXECUTOR=engine)."""

    def __init__(
        self,
        tools: ToolClient,
        *,
        vocab: Vocabulary | None = None,
        extractor: FactExtractor | None = None,
        call_budget: int = MAX_TOOL_CALLS_PER_RUN,
        step_cap_s: float = STEP_CAP_S,
        # One attempt's budget, ToolClient.call's timeout_s (tools/base.py): the client gives the
        # tool's HTTP requests at most TOOL_HTTP_TIMEOUT_S of it (A4).
        step_timeout_s: float = STEP_TIMEOUT_S,
        clock: Clock = time.monotonic,
    ) -> None:
        self.tools = tools
        self.vocab = vocab
        self.extractor = extractor or FactExtractor()
        self.call_budget = call_budget
        self.step_cap_s = step_cap_s
        self.step_timeout_s = step_timeout_s
        self.clock = clock

    # -- entry ------------------------------------------------------------------------------------

    async def execute(self, plan: CompiledPlan, session: StepSession) -> ExecutionResult:
        if not isinstance(session, EngineSession):
            raise TypeError("the engine needs a session that carries its run context")
        state = self._state(plan, session.context, session.resume)
        resume = session.resume
        for step in plan.steps:
            current = state.status.get(step.id, "Queued")
            if current in FINISHED:
                continue
            if current == "Skipped" and state.codes.get(step.id) == "WRITE_SKIPPED":
                continue  # Don't save: later steps use the proposed names
            if current in ("Failed", "Skipped"):
                self._block(state, step.id, current)
                continue
            if session.stop_requested:
                return ExecutionResult(status="Cancelled", code=None, limitations=[])
            blocked = next((d for d in step.depends_on if d in state.blocked), None)
            if blocked is not None:
                reason = f"Not run: {blocked} {state.blocked[blocked]}"
                await session.skip_step(step.id, None, reason)
                state.status[step.id] = "Skipped"
                state.blocked[step.id] = "did not run"
                continue
            if step.kind == "client":
                outcome = await self._write_step(state, step, session, resume)
                if outcome is not None:
                    return outcome
                continue
            if state.calls_made >= self.call_budget:
                await session.skip_step(step.id, "CALL_BUDGET", BUDGET_SUMMARY)
                state.status[step.id], state.codes[step.id] = "Skipped", "CALL_BUDGET"
                state.blocked[step.id] = _STOP_REASONS["CALL_BUDGET"]
                state.stop_code = state.stop_code or "CALL_BUDGET"
                state.limitations.append(call_budget(self.call_budget))
                continue
            if step.kind == "exec":
                await self._exec_step(state, step, session)
            else:
                stopped = await self._server_step(state, step, session)
                if stopped:
                    return ExecutionResult(status="Cancelled", code=None, limitations=[])
        return self._result(state)

    def _state(self, plan: CompiledPlan, ctx: RunContext, resume: ResumeState | None) -> _State:
        views = list(resume.steps) if resume is not None else [s.queued() for s in plan.steps]
        status = {v.id: v.status for v in views}
        codes: dict[str, Code | None] = {v.id: v.code for v in views}
        calls = list(resume.calls) if resume is not None else []
        facts = list(resume.facts) if resume is not None else []
        cohorts = {c.ref: c for c in (resume.cohorts if resume is not None and resume.cohorts else ctx.cohorts)}
        outputs: dict[str, CallRecord] = {}
        reused = dict(resume.reused) if resume is not None else {}
        for view in views:
            origin = view.reused_from or reused.get(view.id)
            version, sid = (origin.plan_version, origin.step_id) if origin else (plan.version, view.id)
            used = [c for c in calls if c.plan_version == version and c.step_id == sid and c.status in ("ok", "empty")]
            if used and (view.status in FINISHED or view.status == "Running"):
                outputs[view.id] = used[-1]
        made = len({c.call_id for c in calls if c.plan_version == plan.version and c.attempt == 1})
        shown = sum(1 for f in facts if f.tool == "rank_top_n" and f.id.endswith(".gene") and ".top." in f.id)
        prior: dict[str, int] = {}
        for call in calls:
            if call.plan_version == plan.version:
                prior[call.step_id] = prior.get(call.step_id, 0) + 1
        state = _State(plan=plan, ctx=ctx, status=status, codes=codes, outputs=outputs, facts=facts, cohorts=cohorts,
                       calls_made=made, rank_shown=shown, prior_calls=prior)
        for view in views:
            if view.status in FINISHED and view.code is not None and self._stops(view.tool, view.code, view.status):
                state.blocked[view.id] = _STOP_REASONS.get(view.code, "stopped the run")
                state.stop_code = state.stop_code or view.code
        return state

    def _block(self, state: _State, step_id: str, status: StepStatus) -> None:
        code = state.codes.get(step_id)
        if status == "Failed":
            state.blocked[step_id] = "failed"
        elif code is not None and code in _STOP_REASONS:
            state.blocked[step_id] = _STOP_REASONS[code]
            state.stop_code = state.stop_code or code
        else:
            state.blocked[step_id] = "did not run"

    def _result(self, state: _State) -> ExecutionResult:
        if any(s == "Failed" for s in state.status.values()):
            return ExecutionResult(status="Partial", code="TOOL_FAILED", limitations=state.limitations)
        if state.stop_code is not None:
            return ExecutionResult(status="Limited", code=state.stop_code, limitations=state.limitations)
        return ExecutionResult(status="Answering", code=None, limitations=state.limitations)

    # -- reading earlier steps ------------------------------------------------------------------------

    def _read(self, state: _State) -> catalog.StepValueReader:
        def read(step_id: str, pointer: str) -> JsonValue:
            call = state.outputs.get(step_id)
            if call is None:
                raise StepFailure(f"{step_id} has no result to read")
            try:
                return catalog.resolve_pointer({"observed": call.observed}, pointer)
            except catalog.PointerError as error:
                raise StepFailure(str(error)) from error

        return read

    def _case_ids(self, state: _State, step_id: str) -> list[str]:
        value = self._read(state)(step_id, "/observed/caseIds")
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise StepFailure(f"{step_id} has no case-ID list")
        return [str(v) for v in value]

    def _first_call(self, state: _State, step_id: str) -> int:
        """n of the next attempt's callId `<runId>.v<n>.<stepId>.c<n>`: every attempt of the step in this version counts."""
        return 1 + state.prior_calls.get(step_id, 0) + state.new_calls.get(step_id, 0)

    # -- server tools ------------------------------------------------------------------------------------

    def _server_input(self, state: _State, step: StepSpec) -> dict[str, JsonValue]:
        read = self._read(state)
        try:
            if step.tool == "de_run":
                args = catalog.DeRunStepArgs.model_validate(step.args)
                saved: dict[str, catalog.SavedGroup] = {}
                for ref, cohort in state.cohorts.items():
                    if cohort.save in ("saved", "reused") and cohort.cohort_id and cohort.definition is not None:
                        saved[ref] = catalog.SavedGroup(
                            name=cohort.saved_name or cohort.name, cohort_id=cohort.cohort_id, filter0=gen3_filter(cohort.definition)
                        )
                request: JsonValue = catalog.de_run_input(args, read, saved).model_dump(mode="json")
            else:
                request = catalog.resolve_step_refs(cast(JsonValue, step.args), read)
        except (ValidationError, ValueError) as error:
            raise StepFailure(f"{step.id} {step.tool}: {error}") from error
        if not isinstance(request, dict):
            raise StepFailure(f"{step.id} {step.tool}: the request is not an object")
        return request

    def _pre_guard(self, state: _State, step: StepSpec, request: Mapping[str, JsonValue]) -> tuple[Code, str, Limitation | None] | None:
        """The pre-guards the executor checks (TOOLS.md GuardSpec checked_by executor: tools.guards.executor_pre_guard,
        with the vocabulary for the ProteinPaint bridge), plus the cheap DE sample-size check before any call."""
        group_counts: dict[str, int] | None = None
        if step.tool == "survival_compare":
            group_counts = {}
            for dep in self._count_deps(state, step):
                fact = next((f for f in state.facts if f.id == f"f.{dep}.count"), None)
                if fact is not None and isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool):
                    group_counts[dep] = int(fact.value)
        violation = guards.executor_pre_guard(step.tool, dict(request), fields=self.vocab, group_counts=group_counts)
        if violation is not None and violation.code == "INELIGIBLE_GROUP":
            return "INELIGIBLE_GROUP", "Not run: a group has fewer than 10 cases", None
        if violation is not None:
            spec = self.vocab.field(violation.field) if self.vocab is not None and violation.field else None
            label = spec.label if spec is not None else violation.field or "A filter field"
            return violation.code, f"Not run: {label} cannot filter ProteinPaint", bridge_unsafe(label)
        if step.tool in ("de_preanalysis", "de_run"):
            sizes = [len(ids) if isinstance(ids := group.get("sampleIds"), list) else 0 for group in _dicts(request.get("groups"))]
            if guards.de_samples_within_limit(sizes) is not None:
                return "REQUEST_TOO_LARGE", "Not run: more than 4,000 samples", None
        return None

    def _count_deps(self, state: _State, step: StepSpec) -> list[str]:
        return survival_count_steps(state.plan, step)

    async def _server_step(self, state: _State, step: StepSpec, session: StepSession) -> bool:
        """Run one server tool step. Returns True when a stop aborted it."""
        await session.start_step(step.id)
        started = self.clock()
        try:
            request = self._server_input(state, step)
        except StepFailure as error:
            await self._fail(state, step, session, str(error), started)
            return False
        guard = self._pre_guard(state, step, request)
        if guard is not None:
            code, summary, line = guard
            await session.skip_step(step.id, code, summary)
            state.status[step.id], state.codes[step.id] = "Skipped", code
            state.blocked[step.id] = _STOP_REASONS.get(code, "did not run")
            state.stop_code = state.stop_code or code
            if line is not None:
                state.limitations.append(line)
            return False
        first = self._first_call(state, step.id)
        attempt = [1]

        async def on_retry(attempt_n: int, backoff_ms: int) -> None:
            attempt[0] = attempt_n
            await session.retrying(step.id, attempt=attempt_n, backoff_ms=backoff_ms)

        retry_hook: RetryHook = _RetryHook(on_retry)

        at = iso_utc(utc_now())

        async def guarded() -> ToolCallOutcome | ToolCallCancelled:
            # A cancelled ToolClient raises ToolCallCancelled carrying every attempt so far (the aborted one
            # with its in-flight request in provenance); keep it as the call's value for the record.
            try:
                return await self.tools.call(
                    cast(ServerToolName, step.tool), request, timeout_s=self.step_timeout_s, run_id=state.ctx.run_id,
                    plan_version=state.plan.version, step_id=step.id, first_call=first, on_retry=retry_hook,
                )
            except ToolCallCancelled as cancelled:
                return cancelled

        call = asyncio.ensure_future(guarded())
        stop = asyncio.ensure_future(session.wait_stop())
        try:
            done, _ = await asyncio.wait({call, stop}, timeout=self.step_cap_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if call not in done:
            call.cancel()
            aborted: ToolCallOutcome | ToolCallCancelled | None = None
            with contextlib.suppress(asyncio.CancelledError, Exception):
                aborted = await call
            stopped = stop in done
            reason = "CANCELLED: stopped" if stopped else f"TIMEOUT: the step cap of {self.step_cap_s:g} s passed"
            records = self._aborted_records(state, step, request, first, attempt[0], at, started, reason, aborted)
            state.new_calls[step.id] = state.new_calls.get(step.id, 0) + len(records)
            state.calls_made += 1
            if stopped:
                await session.skip_step(step.id, None, STOPPED_SUMMARY, calls=records, duration_ms=self._elapsed(started))
                state.status[step.id] = "Skipped"
                return True
            await self._finish_failed(state, step, session, "Timed out on both attempts" if len(records) > 1 else "Timed out", started, records)
            return False
        try:
            outcome = call.result()
            if isinstance(outcome, ToolCallCancelled):  # cancelled from inside the client, not by this step
                raise RuntimeError("the tool call was cancelled without a Stop")
        except Exception as error:  # a ToolClient bug; tool failures come back as results
            log.exception("tool client raised for %s %s", state.ctx.run_id, step.id)
            n = first + attempt[0] - 1
            record = self._aborted(state, step, request, n, attempt[0], at, started, f"INTERNAL_ERROR: {type(error).__name__}")
            state.new_calls[step.id] = state.new_calls.get(step.id, 0) + attempt[0]
            state.calls_made += 1
            await self._finish_failed(state, step, session, "Failed", started, [record])
            return False
        state.new_calls[step.id] = state.new_calls.get(step.id, 0) + len(outcome.attempts)
        state.calls_made += 1
        await self._settle(state, step, session, outcome, started)
        return False

    def _aborted(
        self, state: _State, step: StepSpec, request: JsonValue, n: int, attempt: int, at: str, started: float, error: str
    ) -> CallRecord:
        return CallRecord(
            call_id=call_id(state.ctx.run_id, state.plan.version, step.id, n), plan_version=state.plan.version, step_id=step.id,
            tool=step.tool, transport=self.tools.transport, attempt=min(max(attempt, 1), 2), started_at=at, request=request,
            request_sha256=sha256_hex(canonical_json(request, portable=False)), http_status=None, response_sha256=None,
            elapsed_ms=self._elapsed(started), status="error", observed=None, warnings=[], limitation=None, error=error, provenance=[],
        )

    def _aborted_records(
        self, state: _State, step: StepSpec, request: JsonValue, first: int, attempts: int, at: str, started: float, reason: str,
        aborted: ToolCallOutcome | ToolCallCancelled | None,
    ) -> list[CallRecord]:
        """The records of a call this step aborted (Stop, or the step cap): every attempt the client reported,
        its aborted one carrying the in-flight request in its provenance and `reason` as its error; or one
        synthesized record when the client reported nothing."""
        outcome = aborted.outcome if isinstance(aborted, ToolCallCancelled) else None
        if outcome is None or not outcome.attempts:
            return [self._aborted(state, step, request, first + attempts - 1, attempts, at, started, reason)]
        records = call_records(outcome, plan_version=state.plan.version, step_id=step.id)
        last = records[-1]
        records[-1] = last.model_copy(update={"error": reason, "elapsed_ms": max(last.elapsed_ms, 0)})
        return records

    def _elapsed(self, started: float) -> int:
        return max(int((self.clock() - started) * 1000), 0)

    def _stops(self, tool: str, code: str, status: StepStatus) -> bool:
        """Whether a finished step's code is a stop guard (its dependents do not run)."""
        try:
            guards = catalog.tool_spec(tool).guards
        except KeyError:
            return False
        return any(g.guard.on_fail == code and g.effect == "stop" for g in guards)

    async def _settle(self, state: _State, step: StepSpec, session: StepSession, outcome: ToolCallOutcome, started: float) -> None:
        records = call_records(outcome, plan_version=state.plan.version, step_id=step.id)
        result = outcome.result
        duration = outcome.elapsed_ms or self._elapsed(started)
        if result.status in ("error", "unavailable"):
            error = result.error
            if error is not None and error.code == "GUARD_FAILED" and result.limitation is not None:
                guard_code: Code = result.limitation
                await session.skip_step(step.id, guard_code, f"Not run: {_STOP_REASONS.get(guard_code, 'stopped by a check')}", calls=records)
                state.status[step.id], state.codes[step.id] = "Skipped", guard_code
                state.blocked[step.id] = _STOP_REASONS.get(guard_code, "did not run")
                state.stop_code = state.stop_code or guard_code
                if guard_code == "CALL_BUDGET" and not any(item.code == "CALL_BUDGET" for item in state.limitations):
                    # The tool client's own cap stopped the step: say why under Limitations, as the executor's check does.
                    state.limitations.append(call_budget(self.call_budget))
                return
            if result.status == "unavailable" or (error is not None and error.code == "NOT_CONNECTED"):
                missing: Code = result.limitation or "KG_NOT_CONNECTED"
                await session.skip_step(step.id, missing, "Not run: not connected", calls=records)
                state.status[step.id], state.codes[step.id] = "Skipped", missing
                state.blocked[step.id] = _STOP_REASONS.get(missing, "did not run")
                state.stop_code = state.stop_code or missing
                return
            summary = _FAILURE_SUMMARIES.get(error.code if error is not None else "INTERNAL_ERROR", "Failed")
            await self._finish_failed(state, step, session, summary, started, records, duration=duration)
            return
        record = records[-1]
        status: StepStatus = "Empty" if result.status == "empty" else "Done"
        code = self._post_guard(step, result.status, result.limitation, result.observed)
        await self._finish_ok(state, step, session, record, records, status, code, duration)

    def _post_guard(self, step: StepSpec, status: str, limitation: Code | None, observed: JsonValue) -> Code | None:
        """The post-guard code a finished read carries: the tool's own, or what the executor sees itself."""
        if limitation is not None:
            return limitation
        data = observed if isinstance(observed, dict) else {}
        if step.tool == "cases_query" and data.get("complete") is False:
            return "TOO_MANY_IDS"
        if step.tool == "de_preanalysis":
            groups = data.get("groups")
            if isinstance(groups, list) and any(
                isinstance(g, dict) and isinstance(g.get("eligible"), int) and int(g["eligible"]) < MIN_DE_ELIGIBLE  # type: ignore[arg-type]
                for g in groups
            ):
                return "INELIGIBLE_GROUP"
        if status == "empty":
            for guard in catalog.tool_spec(step.tool).guards if step.tool in catalog.TOOL_REGISTRY else ():
                if guard.guard.phase == "post" and guard.step_status == "Empty":
                    return guard.guard.on_fail
        return None

    async def _finish_ok(
        self, state: _State, step: StepSpec, session: StepSession, record: CallRecord, records: list[CallRecord],
        status: StepStatus, code: Code | None, duration: int,
    ) -> None:
        state.outputs[step.id] = record
        try:
            facts = self.extractor.from_call(step, record, context=self._fact_context(state, step))
        except ExtractionError as error:
            log.warning("run %s %s: %s", state.ctx.run_id, step.id, error)
            await self._finish_failed(state, step, session, "Returned a response ORION could not read", 0.0, records,
                                      duration=duration, keep_output=True)
            return
        state.facts.extend(facts)
        state.rank_shown += sum(1 for f in facts if f.tool == "rank_top_n" and ".top." in f.id and f.id.endswith(".gene"))
        cohorts = self._sized(state, step, record, facts)
        summary = output_summary(step, record.observed, state)
        await session.finish_step(step.id, status, summary, duration, code, facts=facts, calls=records, cohorts=cohorts)
        state.status[step.id], state.codes[step.id] = status, code
        if code is not None and self._stops(step.tool, code, status):
            state.blocked[step.id] = _STOP_REASONS.get(code, "stopped the run")
            state.stop_code = state.stop_code or code

    async def _fail(self, state: _State, step: StepSpec, session: StepSession, message: str, started: float) -> None:
        log.warning("run %s %s cannot be sent: %s", state.ctx.run_id, step.id, message)
        await self._finish_failed(state, step, session, "Could not be sent", started, [])

    async def _finish_failed(
        self, state: _State, step: StepSpec, session: StepSession, summary: str, started: float, records: list[CallRecord],
        *, duration: int | None = None, keep_output: bool = False,
    ) -> None:
        await session.finish_step(
            step.id, "Failed", summary, duration if duration is not None else self._elapsed(started), "TOOL_FAILED", calls=records
        )
        state.status[step.id], state.codes[step.id] = "Failed", "TOOL_FAILED"
        state.blocked[step.id] = "failed"
        if not keep_output:
            state.outputs.pop(step.id, None)

    # -- executor functions ------------------------------------------------------------------------------

    async def _exec_step(self, state: _State, step: StepSpec, session: StepSession) -> None:
        """One executor function (tools/functions.py): in-process, one attempt, its post-guard as the step's code."""
        await session.start_step(step.id)
        started = self.clock()
        outcome = tool_functions.call_function(
            cast(ExecutorFunctionName, step.tool), cast(JsonValue, dict(step.args)), self._function_reader(state),
            run_id=state.ctx.run_id, plan_version=state.plan.version, step_id=step.id, call_n=self._first_call(state, step.id),
            monotonic=self.clock,
        )
        records = call_records(outcome, plan_version=state.plan.version, step_id=step.id)
        state.new_calls[step.id] = state.new_calls.get(step.id, 0) + 1
        state.calls_made += 1
        result = outcome.result
        if result.status not in ("ok", "empty"):
            log.warning("run %s %s %s failed: %s", state.ctx.run_id, step.id, step.tool, result.error.message if result.error else "")
            await self._finish_failed(state, step, session, "Failed", started, records)
            return
        status: StepStatus = "Empty" if result.status == "empty" else "Done"
        await self._finish_ok(state, step, session, records[-1], records, status, result.limitation, records[-1].elapsed_ms)

    def _function_reader(self, state: _State) -> catalog.StepValueReader:
        """The step reader for executor functions, which expect LookupError for a value that does not resolve."""
        read = self._read(state)

        def function_read(step_id: str, pointer: str) -> JsonValue:
            try:
                return read(step_id, pointer)
            except StepFailure as error:
                raise LookupError(str(error)) from error

        return function_read

    # -- the client write --------------------------------------------------------------------------------

    async def _write_step(self, state: _State, step: StepSpec, session: StepSession, resume: ResumeState | None) -> ExecutionResult | None:
        confirm = resume.confirmation if resume is not None else None
        pending = resume.pending if resume is not None else None
        if confirm is None or pending is None or pending.step_id != step.id:
            return ExecutionResult(status="AwaitingConfirmation", code=None, limitations=[], pending=self._pending(state, step))
        if confirm.decision == "dont_save":
            return None  # the controller applied it: the step is Skipped WRITE_SKIPPED
        await session.start_step(step.id)
        started = self.clock()
        problems = verify_read_back(pending, confirm)
        if problems:
            summary = "Read-back did not match: " + "; ".join(problems)
            await session.finish_step(step.id, "Failed", summary, self._elapsed(started), "WRITE_VERIFY_FAILED")
            state.status[step.id], state.codes[step.id] = "Failed", "WRITE_VERIFY_FAILED"
            return ExecutionResult(status="Failed", code="WRITE_VERIFY_FAILED", limitations=[write_verify_failed("; ".join(problems))])
        args = SaveCohortsArgs.model_validate(pending.model_dump(mode="json"))
        request: JsonValue = args.model_dump(mode="json")
        observed: JsonValue = catalog.SaveCohortsObserved(
            decision="create", saved=list(confirm.saved), make_active=confirm.make_active
        ).model_dump(mode="json")
        n = self._first_call(state, step.id)
        record = CallRecord(
            call_id=call_id(state.ctx.run_id, state.plan.version, step.id, n), plan_version=state.plan.version, step_id=step.id,
            tool=step.tool, transport="client", attempt=1, started_at=iso_utc(utc_now()), request=request,
            request_sha256=sha256_hex(canonical_json(request, portable=False)), http_status=None,
            response_sha256=sha256_hex(canonical_json(observed, portable=False)), elapsed_ms=self._elapsed(started), status="ok",
            observed=observed, warnings=[], limitation=None, error=None, provenance=[],
        )
        state.new_calls[step.id] = state.new_calls.get(step.id, 0) + 1
        state.calls_made += 1
        updated: list[CohortRef] = []
        for back in confirm.saved:
            cohort = state.cohorts.get(back.ref)
            if cohort is None:
                continue
            cohort = cohort.model_copy(
                update={"save": "reused" if back.reused else "saved", "cohort_id": back.cohort_id, "saved_name": back.name}
            )
            state.cohorts[back.ref] = cohort
            updated.append(cohort)
        reused = sum(1 for b in confirm.saved if b.reused)
        saved = len(confirm.saved) - reused
        summary = _save_summary(saved, reused)
        state.outputs[step.id] = record
        await session.finish_step(step.id, "Done", summary, self._elapsed(started), None, calls=[record], cohorts=updated)
        state.status[step.id] = "Done"
        return None

    def _pending(self, state: _State, step: StepSpec) -> ConfirmPending:
        """The save pause's payload (TOOLS.md 4): the compiler's `build_confirm_pending` over the finished reads,
        with reuse found in the cohort context or the session ledger."""
        built = compiler_module.build_confirm_pending(
            state.plan, nonce=new_id(), read=self._read(state), context=state.ctx.cohort_context, ledger=state.ctx.ledger
        )
        return ConfirmPending.model_validate(built.model_dump(mode="json"))

    # -- facts and cohorts ---------------------------------------------------------------------------------

    def _fact_context(self, state: _State, step: StepSpec) -> catalog.FactContext:
        """The compiler's labels for a step's facts (`compiler.fact_context`), with the rank cap the earlier
        rankings of this view leave (VIEW_RANK_CAP rows in all)."""
        return replace(compiler_module.fact_context(state.plan, step.id), rank_cap=max(VIEW_RANK_CAP - state.rank_shown, 0))

    def _sized(self, state: _State, step: StepSpec, record: CallRecord, facts: Sequence[FactRecord]) -> list[CohortRef]:
        """The run cohorts this step defines or sizes (contract "cohorts"): caseCount and its fact when the sizing
        step finishes; defSha256 from the compile-time definition, or, for a case-ID cohort, from its case-ID
        reference once every step it reads finished (intersected where the plan intersects it)."""
        out: list[CohortRef] = []
        count = next((f for f in facts if f.id in (f"f.{step.id}.count", f"f.{step.id}.cases")), None)
        ctx = state.ctx
        for ref, cohort in list(state.cohorts.items()):
            update: dict[str, object] = {}
            defining = ctx.cohort_steps.get(ref)
            known = ctx.cohort_definitions.get(ref)
            if defining == step.id:
                if count is not None and isinstance(count.value, (int, float)) and not isinstance(count.value, bool):
                    update["case_count"] = int(count.value)
                    update["case_count_fact_id"] = count.id
                if known is not None and cohort.def_sha256 is None:
                    update["definition"] = known
                    update["def_sha256"] = def_sha256(known)
            if cohort.role == "planned" and known is None:
                definition = self._case_id_definition(state, ref, defining, step.id)
                if definition is not None and def_sha256(definition) != cohort.def_sha256:
                    update["definition"] = definition
                    update["def_sha256"] = def_sha256(definition)
            if update:
                cohort = cohort.model_copy(update=update)
                state.cohorts[ref] = cohort
                out.append(cohort)
        return out

    def _case_id_definition(self, state: _State, ref: str, defining: str | None, finished: str) -> CaseIdsDefinition | None:
        source = state.ctx.cohort_ids.get(ref) or (
            catalog.StepRef(ref=defining, pointer="/observed/caseIds") if defining is not None else None
        )
        if source is None or finished not in (source.ref, source.intersect) or source.ref not in state.outputs:
            return None
        full = source.intersect is not None and source.intersect in state.outputs
        reference = source if full else catalog.StepRef(ref=source.ref, pointer=source.pointer)
        try:
            value = catalog.resolve_step_refs(reference.model_dump(mode="json", by_alias=True), self._read(state))
        except (StepFailure, ValueError):
            return None
        if not isinstance(value, list):
            return None
        return CaseIdsDefinition(kind="case_ids", case_ids=sorted({str(v) for v in value}))


def _save_summary(saved: int, reused: int) -> str:
    if reused == 0:
        return f"{saved} cohort{'s' if saved != 1 else ''} saved and read back"
    if saved == 0:
        return f"{reused} cohort{'s' if reused != 1 else ''} reused"
    return f"{saved} saved, {reused} reused"


def _n(value: JsonValue) -> str:
    return format_fact(value, "cases") if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def output_summary(step: StepSpec, observed: JsonValue, state: _State | None = None) -> str:
    """The step row's output text (TOOLS.md 5.7); a step row is itself the source its numbers cite."""
    data = observed if isinstance(observed, dict) else {}
    tool = step.tool
    if tool == "fixture_fingerprint":
        sha = data.get("fingerprintSha256")
        return f"fingerprint {str(sha)[:4]}…" if isinstance(sha, str) else "fingerprint recorded"
    if tool == "cohort_count":
        return f"{_n(data.get('count'))} cases"
    if tool == "cases_query":
        pages = data.get("pages")
        if data.get("complete") is False:
            return f"{_n(data.get('count'))} cases, too many to list"
        return f"{_n(data.get('count'))} case IDs, {pages} page{'' if pages == 1 else 's'}"
    if tool == "gene_cases":
        return f"{_n(data.get('count'))} case IDs"
    if tool == "set_operation":
        return f"{_n(data.get('count'))} cases" if data.get("op") == "size" else f"{_n(data.get('count'))} case IDs"
    if tool == "check_subset":
        outer = "the outer list"
        if state is not None:
            label = next((f.label for f in state.facts if f.id == f"f.{step.args.get('outer')}.count"), None)
            outer = f"the {label[:1].lower() + label[1:]}" if label else outer
        if data.get("isSubset"):
            return f"All {_n(data.get('innerCount'))} inside {outer}"
        return f"{_n(data.get('outsideCount'))} of {_n(data.get('innerCount'))} outside {outer}"
    if tool in ("sample_lists", "de_preanalysis"):
        groups = data.get("groups")
        name = "count" if tool == "sample_lists" else "eligible"
        values = [g.get(name) for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []
        words = " and ".join(_n(v) for v in values)
        return f"{words} samples" if tool == "sample_lists" else f"{words} RNA-seq eligible"
    if tool == "de_run":
        totals = data.get("totals")
        if isinstance(totals, dict):
            return f"{_n(totals.get('significant'))} of {_n(totals.get('tested'))} genes past the thresholds"
        return "differential expression run"
    if tool == "rank_top_n":
        n = data.get("nRequested")
        count = data.get("count")
        ties = data.get("tiesAtCutoff")
        tied = len(ties) if isinstance(ties, list) else 0
        if count == 0:
            return "No rows to rank"
        rank_by = data.get("rankBy")
        by = "" if rank_by == "cases" else f" by {catalog.RANK_SHORT.get(cast(catalog.RankBy, rank_by), str(rank_by))}"
        shown = min(n, count) if isinstance(n, int) and isinstance(count, int) else n
        return f"Top {shown}{by}, {'no ties' if not tied else f'{tied} tied'}"
    if tool == "top_mutated_genes":
        genes = data.get("genes")
        return f"{len(genes) if isinstance(genes, list) else 0} genes"
    if tool == "survival_compare":
        groups = data.get("groups")
        n = len(groups) if isinstance(groups, list) else 0
        return f"{n} curves, log-rank p reported" if data.get("pValue") is not None else f"{n} curves, no log-rank p"
    if tool == "derive_km_median":
        groups = data.get("groups")
        return f"{len(groups) if isinstance(groups, list) else 0} medians"
    return "Done"


__all__ = [
    "BUDGET_SUMMARY",
    "DECLINED_SUMMARY",
    "DONT_SAVE_SUMMARY",
    "FAILED_SUMMARY",
    "INTERRUPTED_SUMMARY",
    "STOPPED_SUMMARY",
    "Engine",
    "EngineSession",
    "PlanCohorts",
    "RunContext",
    "base_cohort",
    "check_subset",
    "derive_cohort_steps",
    "derive_km_median",
    "gen3_filter",
    "input_hashes",
    "output_summary",
    "passes_thresholds",
    "plan_cohorts",
    "rank_top_n",
    "reuse_map",
    "set_operation",
    "verify_read_back",
]

