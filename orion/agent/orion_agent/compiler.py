"""Compiler: planner IR to a signed plan (RFA rev 25, A2 "Planning: from question to signed plan").

Rules: C-SCOPE, C-VOCAB, C-PANEL, C-UNTESTED, C-CASEIDS, C-SPLIT, C-SAVE, C-FIT, C-GUARD and
C-DEFAULTS, plus the per-run call budget and a compile-time estimate of the largest snapshot.
Step tables, args, step ids, row labels and reasons follow orion/contracts/TOOLS.md sections 5 and
6; where it and the run-event contract disagree, the contract wins.

- A compile-time stop makes no data call, so a CompileStop carries zero steps (C-34).
- The planner names no cohort and writes no display text: names, definition texts, row labels,
  reasons, assumptions and limitation lines all come from code templates here (C-37).
- Every editable value is an argument of the steps it affects, so every real edit changes the
  signature; an edit is compiled from the patched IR into plan version n+1 with no model call.
- A filtered scope is resolved once to a case-ID list (one `cases_query`) and every later step
  names it by StepRef (C-23); ProteinPaint only ever sees case-ID lists (C-CASEIDS).
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from pydantic.alias_generators import to_camel

from . import events, ir
from .catalog import (
    ANALYSIS_APPS,
    ANALYSIS_ASKS,
    APPS,
    CAPABILITIES,
    TOOL_REASONS,
    UNSUPPORTED_TEXTS,
    AnalysisKind,
    AppId,
    Capability,
    app_title,
)
from .models import (
    MAX_TOP_N_DE,
    MAX_TOP_N_GENES,
    SNAPSHOT_LIMIT_BYTES,
    VIEW_RANK_CAP,
    ActiveCohort,
    Assumption,
    CanonicalDefinition,
    CaseIdsDefinition,
    ClarifyOption,
    ClarifyPending,
    CohortContext,
    CohortRef,
    CohortReuse,
    CohortRole,
    CohortSave,
    CohortSource,
    Code,
    ConfirmPending,
    ConsentPending,
    DeMethod,
    DeThresholds,
    Editable,
    EditableCohortName,
    EditableMethod,
    EditableScope,
    EditableThresholds,
    EditableTopN,
    EditCohortNameAction,
    EditMethodAction,
    EditPlanAction,
    EditScopeAction,
    EditThresholdsAction,
    EditTopNAction,
    Fact,
    FilterClause,
    FilterDefinition,
    FilterSet,
    FollowUp,
    FrozenTable,
    HexId,
    LedgerRefUse,
    LedgerUse,
    Limitation,
    NearestApp,
    PendingCohort,
    PlanRow,
    PlanView,
    RunStatus,
    RunView,
    SaveCohortsArgs,
    SavedCohort,
    Scope,
    ScopeChoice,
    ScopeSource,
    Segment,
    Sentence,
    StepKind,
    StepView,
    VizSpec,
    canonical_json,
    def_sha256,
    definition_from_filter_set,
    format_fact,
    normalize_definition,
    sha256_hex,
)
from .settings import DefaultScope
from .tools.base import MAX_SAMPLE_LIST_BODY_BYTES, MAX_TOOL_CALLS_PER_RUN
from .tools.schemas import (
    DE_METHOD_SHORT,
    DE_RANK_BY,
    ORION_SUFFIX,
    RANK_LABELS,
    RANK_SHORT,
    SURVIVAL_TERM,
    VIZ_TEMPLATES,
    FactContext,
    RankBy,
    RankSourceTool,
    SaveCohortsStepArgs,
    StepRef,
    StepValueReader,
    cases_label,
    cohort_label,
    fact_specs,
    is_step_ref,
    resolve_step_refs,
    tool_spec,
    ViewerKind,
)
from .vocab import FieldSpec, VocabProblem, Vocabulary, clauses_from_filter_set

PANEL: Final = ("KRAS", "NRAS", "TP53", "BRAF", "DIS3", "MYC")
GENE_FIELDS: Final = frozenset({"gene.symbol", "gene.gene_id"})
MUTATION_TESTED: Final = ir.Clause(field="available_variation_data", op="in", values=["ssm"])
ALL_CASES: Final = "All cases"
# Day 1's pseudo step id for the unfiltered scope; no step names it any more (kept for importers).
SCOPE_REF: Final = "scope"
CASES_PAGE_SIZE: Final = 1000
MAX_CASE_IDS: Final = 10_000
CASE_IDS: Final = "/observed/caseIds"
# C-DEFAULTS: what the question may leave out (TOOLS.md 6.2, choice 11 for top N).
DEFAULT_DE_METHOD: Final[DeMethod] = "wilcoxon"
DEFAULT_RANK_BY: Final = "adjusted_p"
VIEWER_THRESHOLDS: Final = DeThresholds(p_value_cutoff=0.05, p_value_type="adjusted", fold_change_cutoff=0.3)
TOP_N_DEFAULT_DE: Final = 10
TOP_N_DEFAULT_GENES: Final = 3
MAX_SURVIVAL_GROUPS: Final = 6
# The longest question an ask carries: the size estimate assumes it.
MAX_QUESTION_CHARS: Final = 4000

_CASE_ID_FIELDS: Final = frozenset({"case_id", "cases.case_id"})
_DE_KEYS: Final = ("control", "case")
_UNBLOCK_VOCAB: Final = "Rephrase using a Virtual Lab field and value."
_UNBLOCK_BROWSER: Final = "Remove that filter from the cohort, or ask about all cases."
_UNBLOCK_LEDGER: Final = "Ask again and name the cohort."
# Executor inputs that name earlier steps by bare step id (TOOLS.md 1, "Args, request and Observed").
_BARE_STEP_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "set_operation": ("a", "b"),
    "check_subset": ("inner", "outer"),
    "rank_top_n": ("rows",),
    "derive_km_median": ("curves",),
}


def _absent(value: object) -> bool:
    return value is None


def _empty(value: object) -> bool:
    return not value


class CompiledModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )


class ResolvedScope(CompiledModel):
    source: ScopeSource
    name: str
    case_count: int | None
    clauses: list[ir.Clause]
    # v1.2 additions, omitted when unset: the platform cohort the scope is (CohortView.cohortId of
    # `base`), and the session-ledger cohort ref a thread scope was taken from.
    cohort_id: str | None = Field(default=None, exclude_if=_absent)
    ledger_ref: str | None = Field(default=None, exclude_if=_absent)

    def view(self) -> Scope:
        return Scope(source=self.source, name=self.name, case_count=self.case_count)

    def choice(self) -> ScopeChoice:
        """The scope edit value that resolves to this scope (the plan card's scope chip)."""
        if self.source == "saved_cohort":
            return ScopeChoice(source=self.source, cohort_name=self.name)
        if self.source == "thread_cohort" and self.ledger_ref is not None:
            return ScopeChoice(source=self.source, ledger_ref=self.ledger_ref)
        return ScopeChoice(source=self.source)

    def definition(self) -> CanonicalDefinition | None:
        """The scope's canonical definition (all cases is the empty filter); None when it has none."""
        return definition_of(self.clauses)


class StepSpec(CompiledModel):
    id: str
    row: int
    tool: str
    kind: StepKind
    folded: bool
    input_summary: str
    args: dict[str, JsonValue]
    depends_on: list[str]

    def queued(self) -> StepView:
        return StepView(
            id=self.id,
            row=self.row,
            tool=self.tool,
            kind=self.kind,
            folded=self.folded,
            input_summary=self.input_summary,
            status="Queued",
            output_summary=None,
            duration_ms=None,
            code=None,
        )


class PlanCohort(CompiledModel):
    """One run cohort as the compiler plans it: the scope (`base`) or a cohort the plan builds.

    `definition` is the canonical definition when it is known at compile time (the scope, a filter
    cohort, mutation-tested cases: what a save writes and reuse matches); a case-ID cohort's
    definition is the ids `ids` names, known once its step ran. `step_id` is the step that sizes the cohort and `count_fact_id` that step's count
    fact (the run cohort's caseCountFactId). `save` is `proposed` for a cohort the save step writes,
    `reused` for one an identical saved cohort already holds (cohortId and savedName set), else `none`.
    """

    ref: str
    role: CohortRole
    source: CohortSource
    name: str
    definition_text: str
    definition: CanonicalDefinition | None
    ids: StepRef | None
    step_id: str | None
    count_fact_id: str | None
    save: CohortSave
    cohort_id: str | None
    saved_name: str | None

    def view(self, case_count: int | None = None) -> CohortRef:
        """The run cohort the record starts with (contract "RunView", `cohorts`): a definition known at
        compile time (the scope, a filter cohort, mutation-tested cases) carries its defSha256 at once,
        as the fixtures show; a case-ID cohort's fills in when its step finishes. Only the scope's
        count is known (the browser's); every count fact comes from a step."""
        return CohortRef(
            ref=self.ref,
            role=self.role,
            source=self.source,
            name=self.name,
            definition_text=self.definition_text,
            def_sha256=None if self.definition is None else def_sha256(self.definition),
            case_count=case_count if self.role == "scope" else None,
            save=self.save,
            cohort_id=self.cohort_id,
            saved_name=self.saved_name,
            case_count_fact_id=None,
            definition=self.definition,
        )


class CompiledPlan(CompiledModel):
    version: int
    signature: str
    scope: ResolvedScope
    rows: list[PlanRow]
    steps: list[StepSpec]
    assumptions: list[Assumption]
    limitations: list[Limitation]
    writes: bool
    paraphrase: str
    # v1.2 (contract): the second scope a follow-up names after a cohort switch, and the editable settings.
    alt_scope: ResolvedScope | None = None
    # The scope edit value that switches to alt_scope (the plan card's switch chip); set with it.
    alt_scope_choice: ScopeChoice | None = None
    editable: list[Editable] = Field(default_factory=list)
    # Compiler additions, omitted when empty so a plan without them keeps the contract's shape:
    # the run cohorts in plan order (base first), the consent a named-tool substitution waits for
    # (C-FIT, E7), the apps a named tool or the analysis hands off to (Open <app>), and what the
    # plan took from the session ledger (the record's `ledger`).
    cohorts: list[PlanCohort] = Field(default_factory=list, exclude_if=_empty)
    consent: ConsentPending | None = Field(default=None, exclude_if=_absent)
    open_apps: list[NearestApp] = Field(default_factory=list, exclude_if=_empty)
    ledger: LedgerUse | None = Field(default=None, exclude_if=_absent)

    def view(self) -> PlanView:
        return PlanView(
            version=self.version,
            signature=self.signature,
            scope=self.scope.view(),
            rows=self.rows,
            assumptions=self.assumptions,
            writes=self.writes,
            paraphrase=self.paraphrase,
            alt_scope=None if self.alt_scope is None else self.alt_scope.view(),
            alt_scope_choice=self.alt_scope_choice,
            editable=self.editable,
        )

    def step(self, step_id: str) -> StepSpec:
        for spec in self.steps:
            if spec.id == step_id:
                return spec
        raise KeyError(f"{step_id} is not a step of plan version {self.version}")

    def cohort(self, ref: str) -> PlanCohort:
        for planned in self.cohorts:
            if planned.ref == ref:
                return planned
        raise KeyError(f"{ref} is not a cohort of plan version {self.version}")

    def write_step(self) -> StepSpec | None:
        """The plan's save_cohorts step (at most one: one confirmation card per plan)."""
        return next((s for s in self.steps if s.tool == "save_cohorts"), None)

    def run_cohorts(self) -> list[CohortRef]:
        """The record's cohorts at plan time: `base` with its definition and the browser's count,
        every planned cohort with its save state, definitions and counts filled when sized."""
        return [c.view(self.scope.case_count) for c in self.cohorts]


class CompileStop(CompiledModel):
    code: Code
    limitations: list[Limitation]
    assumptions: list[Assumption]
    scope: ResolvedScope | None
    # Limited for a compile-time guard (no data call); Unsupported for a question no analysis kind fits.
    status: Literal["Limited", "Unsupported"] = "Limited"


class LedgerCohort(CompiledModel):
    ref: str
    name: str
    clauses: list[ir.Clause]
    # v1.2 (contract types for the ledger builder): the saved cohort, when the run saved or reused one.
    cohort_id: str | None = None
    def_sha256: str | None = None
    case_count: int | None = None
    # The run cohort's canonical definition (CohortRef.definition): a case-ID list or a filter. A
    # thread cohort is rebuilt from it; without it, from `clauses`.
    definition: CanonicalDefinition | None = None
    definition_text: str | None = None


class LedgerRun(CompiledModel):
    run_id: HexId
    scope: ResolvedScope
    cohorts: list[LedgerCohort]
    # v1.2: what the planner sees of an earlier run (A2 SessionLedger: questions, signatures, fact labels).
    question: str = ""
    signature: str | None = None
    status: RunStatus | None = None
    analyses: list[AnalysisKind] = Field(default_factory=list)
    fact_labels: list[str] = Field(default_factory=list)
    # The IR the run's current plan version compiled (planHistory), so a follow-up such as "Show the
    # top 5 instead" can reuse its analysis; and the active cohort when it was asked (its ask's
    # cohortContext.active), so a follow-up can tell the active cohort changed since (E15).
    plan_ir: ir.Plan | None = None
    active: ActiveCohort | None = None


class SessionLedger(CompiledModel):
    """The thread's earlier runs, oldest first, derived from their run records."""

    runs: list[LedgerRun]


class EditRejected(Exception):
    """An edit_plan the current plan cannot take (contract "Checks, in order", 7): INVALID_INPUT."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PlanEdit:
    """An accepted edit: the patched IR (planHistory's `ir`) and version n+1, or a no-op.

    A no-op (the edited value equals the current editable value) compiles nothing: `result` is None
    and the controller logs it `repeat` and answers it like a repeated confirm.
    """

    ir: ir.Plan
    result: "CompiledPlan | CompileStop | None"

    @property
    def no_op(self) -> bool:
        return self.result is None


class _Stop(Exception):
    def __init__(self, code: Code, limitations: list[Limitation]) -> None:
        super().__init__(code)
        self.code = code
        self.limitations = limitations


def _stop(code: Code, text: str, unblock: str | None, apps: Sequence[AppId] = ()) -> _Stop:
    return _Stop(code, [Limitation(code=code, text=text, unblock=unblock, nearest_apps=[NearestApp.of(a) for a in apps])])


def _stop_for(problem: VocabProblem, where: str, unblock: str) -> _Stop:
    return _Stop(problem.code, [Limitation(code=problem.code, text=f"{where}{problem.text}", unblock=unblock)])


# --- Filters, definitions and references ------------------------------------------------------------


def gql_filter(clauses: list[ir.Clause]) -> dict[str, JsonValue]:
    """Gen3 filter grammar, always `and`-wrapped (an unwrapped filter can be silently ignored)."""
    return {"and": _leaves(clauses)}


def _leaves(clauses: Sequence[ir.Clause]) -> list[JsonValue]:
    leaves: list[JsonValue] = []
    for clause in clauses:
        values: list[JsonValue] = list(clause.values)
        op = "in" if clause.op == "=" else clause.op
        leaves.append({op: {clause.field: values if op == "in" else values[0]}})
    return leaves


def step_ref(step_id: str, pointer: str = CASE_IDS, intersect: str | None = None) -> dict[str, JsonValue]:
    """A StepRef as StepSpec.args carries it (TOOLS.md 1): the value at `pointer` in that step's call."""
    ref: dict[str, JsonValue] = {"ref": step_id, "pointer": pointer}
    if intersect is not None:
        ref["intersect"] = intersect
    return ref


def _ref_dict(ref: StepRef) -> dict[str, JsonValue]:
    return step_ref(ref.ref, ref.pointer, ref.intersect)


def case_id_leaf(ids: JsonValue) -> JsonValue:
    """The Gen3 filter leaf of a case-ID list (or a StepRef to one): `{"in": {"case_id": ids}}`."""
    return {"in": {"case_id": ids}}


def definition_of(clauses: Sequence[ir.Clause]) -> CanonicalDefinition | None:
    """The canonical definition of a filter (contract "Cohort definitions and defSha256"), or None
    when it cannot have one (over 20 clauses, mixed value types, a number outside the canonical range)."""
    try:
        parts = [FilterClause(field=c.field, op="in" if c.op == "=" else c.op, values=list(c.values)) for c in clauses]
        normal = normalize_definition(FilterDefinition(kind="filter", clauses=parts))
        def_sha256(normal)
    except (ValidationError, ValueError):
        return None
    return normal


def clauses_of(definition: CanonicalDefinition) -> list[ir.Clause]:
    """IR clauses of a canonical definition: a case-ID list is one `in` clause on case_id."""
    if isinstance(definition, CaseIdsDefinition):
        values: list[ir.Value] = list(definition.case_ids)
        return [ir.Clause(field="case_id", op="in", values=values)] if values else []
    return [ir.Clause(field=c.field, op=c.op, values=list(c.values)) for c in definition.clauses]


def summarize(clauses: list[ir.Clause], vocab: Vocabulary) -> str:
    parts: list[str] = []
    for clause in clauses:
        spec = vocab.field(clause.field)
        label = spec.label if spec else clause.field
        shown = ", ".join(str(v) for v in clause.values[:3]) + (f" and {len(clause.values) - 3} more" if len(clause.values) > 3 else "")
        parts.append(f"{label} {'=' if clause.op in ('in', '=') and len(clause.values) == 1 else clause.op} {shown}")
    return "; ".join(parts)


def join_words(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def signature(scope: ResolvedScope, steps: list[StepSpec], assumptions: list[Assumption], writes: bool) -> str:
    """sha256 of the compiled plan with its resolved scope; named tools, labels and assumption text are left out.

    It covers the scope (source and clauses), every step's tool, kind, row, args and dependencies,
    the assumption codes and whether the plan writes. Every editable value is a step argument, so
    an edit always changes it; the scope's name and the browser's counts never do.
    """
    canonical = {
        "scope": {"source": scope.source, "clauses": [c.model_dump(mode="json") for c in scope.clauses]},
        "steps": [
            {"id": s.id, "row": s.row, "tool": s.tool, "kind": s.kind, "args": s.args, "dependsOn": s.depends_on}
            for s in steps
        ],
        "assumptions": sorted(a.code for a in assumptions),
        "writes": writes,
    }
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def step_input_hashes(plan: CompiledPlan) -> dict[str, str]:
    """Each step's input hash (TOOLS.md 1, "Input hash and Reused"): sha256 of the canonical JSON of
    {"tool", "version", "args", "dependsOn"} with every StepRef and every bare step id an executor
    Input names replaced by the referenced step's own input hash, and dependsOn listing the input
    hashes of the steps it names (so a save whose cohorts' defining steps changed changes too).
    Equal hashes across plan versions mean Reused."""
    hashes: dict[str, str] = {}

    def swap(value: JsonValue) -> JsonValue:
        if is_step_ref(value) and isinstance(value, dict):
            out: dict[str, JsonValue] = {"ref": hashes[str(value["ref"])], "pointer": value["pointer"]}
            if value.get("intersect") is not None:
                out["intersect"] = hashes[str(value["intersect"])]
            return out
        if isinstance(value, dict):
            return {k: swap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [swap(v) for v in value]
        return value

    for step in plan.steps:
        args = swap(step.args)
        assert isinstance(args, dict)
        for name in _BARE_STEP_FIELDS.get(step.tool, ()):
            if isinstance(args.get(name), str):
                args[name] = hashes[str(args[name])]
        depends: list[JsonValue] = [hashes[d] for d in step.depends_on]
        document: dict[str, JsonValue] = {"tool": step.tool, "version": tool_spec(step.tool).version, "args": args, "dependsOn": depends}
        hashes[step.id] = sha256_hex(canonical_json(document, portable=False))
    return hashes


def step_chip(plan: CompiledPlan, step_id: str) -> str:
    """How chips and the Evidence Inspector name a step (C-15): "Row 6 · s9 de_run"."""
    step = plan.step(step_id)
    return f"Row {step.row} · {step.id} {step.tool}"


def apply_untested_rule(cohorts: list[ir.CohortDef]) -> tuple[list[ir.CohortDef], list[str]]:
    """C-UNTESTED: "without mutation X" means mutation-tested minus X-mutated.

    A difference whose `b` is gene-mutated and whose `a` is not mutation-tested gets a new
    `a` = mutation_tested(within the old a). Returns the cohorts and the refs of every
    mutation-tested cohort whose excluded (untested) cases are stated (template de.untested, from
    the scope and tested counts: no extra step, TOOLS.md choice 5).
    """
    by_ref = {c.ref: c for c in cohorts}
    result: list[ir.CohortDef] = []
    tested_refs: list[str] = []
    for cohort in cohorts:
        definition = cohort.definition
        if isinstance(definition, ir.DifferenceDef) and isinstance(
            getattr(by_ref.get(definition.b), "definition", None), ir.GeneMutatedDef
        ):
            a_def = getattr(by_ref.get(definition.a), "definition", None)
            if isinstance(a_def, ir.MutationTestedDef):
                tested = definition.a
            else:
                tested = _fresh_ref(f"{definition.a}_tested", by_ref)
                added = ir.CohortDef(ref=tested, definition=ir.MutationTestedDef(kind="mutation_tested", within=definition.a))
                by_ref[tested] = added
                result.append(added)
                cohort = cohort.model_copy(update={"definition": definition.model_copy(update={"a": tested})})
            if tested not in tested_refs:
                tested_refs.append(tested)
        result.append(cohort)
    return result, tested_refs


def _fresh_ref(base: str, taken: Mapping[str, object]) -> str:
    candidate, n = base[:32], 2
    while candidate in taken or candidate == ir.BASE_REF:
        candidate, n = f"{base[:29]}_{n}", n + 1
    return candidate


def _apply_untested(sources: list["_Source"]) -> tuple[list["_Source"], list[str]]:
    """C-UNTESTED over resolved cohorts: the rewritten list, a new tested cohort before its user."""
    by_ref = {s.ref: s for s in sources}
    cohorts, tested = apply_untested_rule([ir.CohortDef(ref=s.ref, definition=s.definition, name=s.name) for s in sources])
    out: list[_Source] = []
    for cohort in cohorts:
        definition = cohort.definition
        assert not isinstance(definition, (ir.SavedDef, ir.ThreadDef))
        known = by_ref.get(cohort.ref)
        out.append(_Source(cohort.ref, definition) if known is None else replace(known, definition=definition))
    return out, tested


def _sorted_steps(step_ids: set[str]) -> list[str]:
    return sorted(step_ids, key=lambda s: int(s[1:]))


def _refs_in(value: JsonValue) -> set[str]:
    if is_step_ref(value) and isinstance(value, dict):
        found = {str(value["ref"])}
        if value.get("intersect") is not None:
            found.add(str(value["intersect"]))
        return found
    if isinstance(value, dict):
        return set().union(*(_refs_in(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_refs_in(v) for v in value)) if value else set()
    return set()


def _proposed_name(label: str) -> str:
    """A name ORION proposes for a saved cohort: the label plus " (ORION)", within 100 characters."""
    return label[: 100 - len(ORION_SUFFIX)].rstrip() + ORION_SUFFIX


def _norm_text(text: str) -> str:
    return " ".join(re.sub(r"[-–—_/]", " ", text.casefold()).split())


# --- Saved cohorts: reuse by defSha256 (C-SAVE, FR-08) -------------------------------------------------


def _saved_definitions(context: CohortContext) -> list[tuple[SavedCohort, str]]:
    out: list[tuple[SavedCohort, str]] = []
    for saved in context.saved:
        definition = definition_from_filter_set(saved.filters)
        if definition is not None:
            out.append((saved, def_sha256(definition)))
    return out


def find_reuse(sha: str, context: CohortContext, ledger: "SessionLedger | None" = None) -> CohortReuse | None:
    """An identical saved cohort for a definition hash: one in the browser's cohort list whose stored
    filters hash the same, else a cohort an earlier run of the thread saved with that hash that the
    cohort list still holds (the browser re-checks at Create, since the store may change)."""
    for saved, saved_sha in _saved_definitions(context):
        if saved_sha == sha:
            return CohortReuse(cohort_id=saved.id, name=saved.name)
    present = {saved.id: saved for saved in context.saved}
    for run in reversed(ledger.runs if ledger else []):
        for cohort in run.cohorts:
            if cohort.def_sha256 == sha and cohort.cohort_id is not None and cohort.cohort_id in present:
                return CohortReuse(cohort_id=cohort.cohort_id, name=present[cohort.cohort_id].name)
    return None


def build_confirm_pending(
    plan: CompiledPlan,
    *,
    nonce: str,
    read: StepValueReader,
    context: CohortContext,
    ledger: SessionLedger | None = None,
) -> SaveCohortsArgs:
    """The save pause's pending payload (contract "save_cohorts tool call"), every definition present.

    Built from the write step's args once the steps before it ran: each cohort's canonical definition
    (its filter, or the case IDs its StepRef reads, intersected where the plan intersects them) and
    defSha256, the count and its fact, the eligible count and its fact (DE), and `reuse`, an
    identical saved cohort in the cohort context or the session ledger. `read(step_id, pointer)`
    reads a finished step's last successful call, as resolve_step_refs does. A cohort whose case IDs
    lost cases to an intersection has no count fact (no step produced that number).
    """
    step = plan.write_step()
    if step is None:
        raise ValueError(f"plan version {plan.version} has no write step")
    args = SaveCohortsStepArgs.model_validate(step.args)
    cohorts: list[PendingCohort] = []
    for entry in args.cohorts:
        planned = plan.cohort(entry.ref)
        count_fact = planned.count_fact_id
        definition: CanonicalDefinition
        if planned.definition is not None:
            definition = planned.definition
            if planned.step_id is None:
                raise ValueError(f"cohort {entry.ref} has no sizing step")
            count = read(planned.step_id, "/observed/count")
            if not isinstance(count, int):
                raise ValueError(f"{planned.step_id} has no count")
            case_count = count
        else:
            if planned.ids is None:
                raise ValueError(f"cohort {entry.ref} has neither a definition nor case IDs")
            resolved = resolve_step_refs(_ref_dict(planned.ids), read)
            if not isinstance(resolved, list):
                raise ValueError(f"cohort {entry.ref}'s case IDs are not a list")
            case_ids = sorted({str(i) for i in resolved})
            definition = CaseIdsDefinition(kind="case_ids", case_ids=case_ids)
            case_count = len(case_ids)
            if planned.ids.intersect is not None:
                raw = read(planned.ids.ref, "/observed/count")
                count_fact = count_fact if raw == case_count else None
        eligible: int | None = None
        eligible_fact: str | None = None
        if entry.eligible is not None:
            value = read(entry.eligible.ref, entry.eligible.pointer)
            if not isinstance(value, int):
                raise ValueError(f"{entry.eligible.ref}{entry.eligible.pointer} is not a count")
            eligible = value
            index = int(entry.eligible.pointer.split("/")[-2])
            eligible_fact = f"f.{entry.eligible.ref}.eligible.{_DE_KEYS[index]}"
        sha = def_sha256(definition)
        cohorts.append(
            PendingCohort(
                ref=entry.ref, name=entry.name, definition_text=planned.definition_text, definition=definition,
                def_sha256=sha, case_count=case_count, eligible_count=eligible, reuse=find_reuse(sha, context, ledger),
                case_count_fact_id=count_fact, eligible_count_fact_id=eligible_fact,
            )
        )
    return SaveCohortsArgs(
        kind="confirm", nonce=nonce, plan_signature=plan.signature, plan_version=plan.version, step_id=step.id, cohorts=cohorts
    )


def fact_context(plan: CompiledPlan, step_id: str) -> FactContext:
    """What the extractor needs to label a step's facts (schemas.FactContext): the cohort a count
    counts, check_subset's inner label, the DE groups' labels and a ranking's source and rankBy.
    `rank_cap` is left at VIEW_RANK_CAP: the extractor lowers it by the rows earlier rankings show."""
    step = plan.step(step_id)
    if step.tool in ("cohort_count", "cases_query", "set_operation"):
        return FactContext(cohort=_counted(plan, step))
    if step.tool == "check_subset":
        inner = plan.step(str(step.args["inner"]))
        if inner.tool == "gene_cases":
            return FactContext(inner=f"{inner.args['gene']}-mutated cases")
        return FactContext(inner=cases_label(_counted(plan, inner)))
    if step.tool in ("sample_lists", "de_preanalysis"):
        sample_step = step.id if step.tool == "sample_lists" else _first_ref(step.args)
        for other in plan.steps:
            if other.tool == "de_run" and _first_ref(other.args) == sample_step:
                groups = other.args["groups"]
                assert isinstance(groups, list)
                names = tuple(cohort_label(str(g["name"])) for g in groups if isinstance(g, dict))
                return FactContext(group_labels=names)
        return FactContext()
    if step.tool == "rank_top_n":
        rank = _rank_of(plan, step)
        if rank is not None:
            return FactContext(rank_source=rank[0], rank_by=rank[1])
    return FactContext()


def _first_ref(value: JsonValue) -> str | None:
    refs = _refs_in(value)
    return _sorted_steps(refs)[0] if refs else None


def _counted(plan: CompiledPlan, step: StepSpec) -> str:
    """The name of the cohort (or survival group) a count step counts."""
    target = str(step.args["a"]) if step.tool == "set_operation" and step.args.get("op") == "size" else step.id
    for planned in plan.cohorts:
        if planned.step_id == target or (planned.ids is not None and planned.ids.ref == target and planned.ids.intersect is None):
            return planned.name
    for other in plan.steps:
        if other.tool == "survival_compare":
            groups = other.args["groups"]
            assert isinstance(groups, list)
            for group in groups:
                if isinstance(group, dict) and group.get("filter") == step.args.get("filter"):
                    return str(group["label"])
    return plan.scope.name


# --- The builder ----------------------------------------------------------------------------------------

CohortKind = Literal["base", "filter", "tested", "gene", "difference", "intersection"]
Origin = Literal["planned", "saved", "thread"]
ResolvedDef = ir.FilterDef | ir.GeneMutatedDef | ir.MutationTestedDef | ir.DifferenceDef | ir.IntersectionDef


@dataclass(frozen=True)
class _Source:
    """An IR cohort after C-VOCAB: saved and thread cohorts are filters (a case-ID list is a case_id clause)."""

    ref: str
    definition: ResolvedDef
    origin: Origin = "planned"
    name: str | None = None  # the IR's name, or the saved or ledger cohort's
    known_id: str | None = None  # the saved cohort's id, or the ledger cohort's
    known_sha: str | None = None  # the ledger cohort's defSha256
    text: str | None = None  # the ledger cohort's definition text
    ledger_run: str | None = None
    ledger_ref: str | None = None
    case_ids: bool = False  # a thread cohort rebuilt from its case-ID list


@dataclass(frozen=True)
class _Info:
    """What a cohort is without its steps: kind, label, definition, inline Gen3 filter."""

    ref: str
    kind: CohortKind
    label: str  # name without the ORION suffix: "KRAS mutated", "CGS high-risk"
    text: str  # definition text
    phrase: str  # in check_subset summaries: "KRAS-mutated cases", "the mutation-tested cases"
    short: str  # in set_operation summaries: "KRAS-mutated", "Mutation-tested"
    root: str  # the population the cohort lies in: base, or the filter cohort a chain starts from
    definition: CanonicalDefinition | None = None  # known at compile time
    inline: list[JsonValue] | None = None  # Gen3 filter leaves (scope included) when no step is needed
    clauses: list[ir.Clause] = field(default_factory=list)  # scope and own clauses (filter, tested)
    own: list[ir.Clause] = field(default_factory=list)  # the cohort's own clauses (filter cohorts)
    gene: str | None = None
    within: str | None = None
    source: _Source | None = None


@dataclass
class _DeParts:
    analysis: ir.DifferentialExpression
    groups: tuple[str, str]
    samples: str
    pre: str
    method: DeMethod
    rank_by: RankBy
    ascending: bool
    thresholds: DeThresholds
    top_n: int
    index: int


@dataclass
class _Builder:
    vocab: Vocabulary
    scope: ResolvedScope
    sources: dict[str, _Source]
    order: list[str]  # IR cohort order (after C-UNTESTED)
    saved_shas: dict[str, SavedCohort]
    context: CohortContext
    steps: list[StepSpec] = field(default_factory=list)
    rows: list[PlanRow] = field(default_factory=list)
    infos: dict[str, _Info] = field(default_factory=dict)
    ids: dict[str, dict[str, JsonValue]] = field(default_factory=dict)
    bare: dict[str, str] = field(default_factory=dict)
    sizing: dict[str, str] = field(default_factory=dict)  # ref -> the step whose count sizes it
    counts_row: int | None = None
    scope_step: str | None = None
    all_cases_step: str | None = None  # the unfiltered scope's case IDs, when a step needs them
    save_state: dict[str, CohortSave] = field(default_factory=dict)
    reused: dict[str, SavedCohort | CohortReuse] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    open_apps: list[AppId] = field(default_factory=list)
    main_rows: list[int] = field(default_factory=list)  # per analysis: the row its tool reason sits on

    # -- rows and steps ---------------------------------------------------------------------------------

    def add_row(self, label: str, tool: str) -> int:
        self.rows.append(PlanRow(row=len(self.rows) + 1, label=label, tool=tool, reason=TOOL_REASONS.get(tool, ""), step_ids=[]))
        if len(self.rows) == 1:
            # s1 records the data and service versions, folded under row 1, before any other call.
            self.add_step(1, "fixture_fingerprint", "server", "sandbox status, versions, ranking", {}, folded=True)
        return len(self.rows)

    def add_step(
        self, row: int, tool: str, kind: StepKind, summary: str, args: dict[str, JsonValue],
        depends_on: Sequence[str] = (), folded: bool = False,
    ) -> str:
        step_id = f"s{len(self.steps) + 1}"
        needs = _refs_in(args) | set(depends_on)
        for name in _BARE_STEP_FIELDS.get(tool, ()):
            if isinstance(args.get(name), str):
                needs.add(str(args[name]))
        self.steps.append(
            StepSpec(
                id=step_id, row=row, tool=tool, kind=kind, folded=folded, input_summary=summary, args=args,
                depends_on=_sorted_steps(needs),
            )
        )
        self.rows[row - 1].step_ids.append(step_id)
        return step_id

    # -- the scope ----------------------------------------------------------------------------------------

    def scope_leaves(self) -> list[JsonValue]:
        """C-23: a filtered scope enters every later filter as its case-ID list, by StepRef."""
        return [] if self.scope_step is None else [case_id_leaf(step_ref(self.scope_step))]

    def scope_filter(self) -> dict[str, JsonValue] | None:
        """The scope as a Gen3 filter for filter0 and survival's scope: null for an unfiltered scope."""
        return None if self.scope_step is None else {"and": self.scope_leaves()}

    def add_scope_step(self, label: str) -> None:
        """C-SCOPE: a filtered scope compiles to exactly one cases_query step; an empty filter needs none."""
        if not self.scope.clauses:
            return
        row = self.add_row(label, "cases_query")
        self.scope_step = self.add_step(
            row, "cases_query", "server", summarize(self.scope.clauses, self.vocab),
            {"filter": gql_filter(self.scope.clauses), "pageSize": CASES_PAGE_SIZE, "maxIds": MAX_CASE_IDS},
        )

    def base_count_fact(self) -> str:
        return "f.s1.cases" if self.scope_step is None else f"f.{self.scope_step}.count"

    # -- cohort information (no steps) --------------------------------------------------------------------

    def info(self, ref: str) -> _Info:
        if ref in self.infos:
            return self.infos[ref]
        if ref == ir.BASE_REF:
            text = "All cases, no filters" if not self.scope.clauses else summarize(self.scope.clauses, self.vocab)
            made = _Info(
                ref=ref, kind="base", label=self.scope.name, text=text, phrase=f"cases in {self.scope.name}", short=self.scope.name,
                root=ir.BASE_REF, definition=self.scope.definition(), inline=self.scope_leaves(), clauses=list(self.scope.clauses),
            )
        else:
            made = self._planned_info(self.sources[ref])
        self.infos[ref] = made
        return made

    def _planned_info(self, source: _Source) -> _Info:
        definition = source.definition
        scope = list(self.scope.clauses)
        if isinstance(definition, ir.FilterDef):
            own = list(definition.clauses)
            named = source.name is not None and source.origin != "planned"
            label = cohort_label(source.name) if named and source.name is not None else self._filter_label(own)
            if source.text is not None:
                text = source.text
            elif source.case_ids:
                text = f"{source.name or label}, the case-ID list from an earlier answer"
            else:
                text = summarize(scope + own, self.vocab)
            return _Info(
                ref=source.ref, kind="filter", label=label, text=text, phrase=cases_label(label), short=label, root=source.ref,
                definition=definition_of(scope + own), inline=self.scope_leaves() + _leaves(own), clauses=scope + own, own=own,
                source=source,
            )
        if isinstance(definition, ir.MutationTestedDef):
            within = self.info(definition.within)
            plain = within.kind == "base"
            label = "Mutation-tested" if plain else f"Mutation-tested {within.label}"
            where = "Cases" if plain and not scope else f"Cases in {self._name_of(within)}"
            static = within.kind in ("base", "filter") and within.inline is not None
            clauses = within.clauses + [MUTATION_TESTED] if static else []
            return _Info(
                ref=source.ref, kind="tested", label=label, text=f"{where} with SSM data (available variation data = ssm)",
                phrase="the mutation-tested cases", short="Mutation-tested", root=within.root,
                definition=definition_of(clauses) if static else None,
                inline=(within.inline or []) + _leaves([MUTATION_TESTED]) if static else None, clauses=clauses,
                within=definition.within, source=source,
            )
        if isinstance(definition, ir.GeneMutatedDef):
            within = self.info(definition.within)
            gene = definition.gene
            prefix, where = self._population(within.root)
            who = "Mutation-tested cases" if within.kind == "tested" else "Cases"
            return _Info(
                ref=source.ref, kind="gene", label=f"{prefix}{gene} mutated", text=f"{who} with a {gene} mutation{where}, as a case-ID list",
                phrase=f"{gene}-mutated cases", short=f"{gene}-mutated", root=within.root, gene=gene, within=definition.within,
                source=source,
            )
        a, b = self.info(definition.a), self.info(definition.b)
        if isinstance(definition, ir.DifferenceDef):
            if a.kind == "tested" and b.kind == "gene" and b.gene is not None:
                prefix, where = self._population(a.root)
                label = f"{prefix}{b.gene} not detected"
                text = f"Mutation-tested cases without a {b.gene} mutation{where}, as a case-ID list"
            else:
                label = f"{a.label} without {b.label}"
                text = f"{self._name_of(a)} minus {self._name_of(b)}, as a case-ID list"
            return _Info(
                ref=source.ref, kind="difference", label=label, text=text, phrase=cases_label(label), short=label, root=a.root,
                source=source,
            )
        label = f"{a.label} and {b.label}"
        return _Info(
            ref=source.ref, kind="intersection", label=label, text=f"Cases in both {self._name_of(a)} and {self._name_of(b)}, as a case-ID list",
            phrase=cases_label(label), short=label, root=a.root, source=source,
        )

    def _population(self, root: str) -> tuple[str, str]:
        """A gene-defined cohort's label prefix and text suffix when it lies in a filter cohort."""
        if root == ir.BASE_REF:
            return "", ""
        population = self.info(root)
        return f"{population.label}, ", f" in {self._name_of(population)}"

    def _filter_label(self, clauses: Sequence[ir.Clause]) -> str:
        parts: list[str] = []
        for clause in clauses:
            spec = self.vocab.field(clause.field)
            if spec is None:
                parts.append(f"{clause.field} {clause.op} {', '.join(str(v) for v in clause.values)}")
            elif clause.op in ("in", "=") and clause.field not in _CASE_ID_FIELDS:
                parts.append(" or ".join(spec.display(v) for v in clause.values))
            else:
                parts.append(f"{spec.label} {clause.op} {', '.join(str(v) for v in clause.values[:3])}")
        return " and ".join(parts)

    def _name_of(self, info: _Info) -> str:
        """A cohort's name in another cohort's definition text."""
        if info.kind == "base":
            return info.label
        return self.names.get(info.ref) or cases_label(info.label)

    # -- cohort steps ----------------------------------------------------------------------------------

    def gen3(self, ref: str) -> list[JsonValue]:
        """The cohort as Gen3 filter leaves: inline for the scope and filter cohorts, else its case IDs."""
        info = self.info(ref)
        if info.inline is not None:
            return list(info.inline)
        return [case_id_leaf(self.case_ids(ref))]

    def case_ids(self, ref: str) -> dict[str, JsonValue]:
        """C-CASEIDS: a StepRef to the cohort's case IDs, adding the steps that produce them."""
        if ref in self.ids:
            return self.ids[ref]
        info = self.info(ref)
        made: dict[str, JsonValue]
        if info.kind == "base":
            if self.scope_step is not None:
                made = step_ref(self.scope_step)
            else:
                if self.all_cases_step is None:
                    row = self.add_row(f"Find cases in {self.scope.name}", "cases_query")
                    self.all_cases_step = self.add_step(
                        row, "cases_query", "server", ALL_CASES,
                        {"filter": {"and": []}, "pageSize": CASES_PAGE_SIZE, "maxIds": MAX_CASE_IDS},
                    )
                made = step_ref(self.all_cases_step)
        elif info.kind in ("filter", "tested"):
            within_leaves: list[JsonValue]
            if info.inline is not None:
                within_leaves = list(info.inline)
            else:
                assert info.within is not None
                within_leaves = [case_id_leaf(self.case_ids(info.within))] + _leaves([MUTATION_TESTED])
            if info.kind == "tested":
                label = "Find mutation-tested cases" if info.label == "Mutation-tested" else f"Find {info.label.lower()[:1]}{info.label[1:]} cases"
                summary = "Mutation-tested cases (available variation data = ssm)"
            else:
                label = f"Find {cases_label(info.label)}"
                summary = info.text if info.source is not None and info.source.case_ids else summarize(info.own, self.vocab)
            row = self.add_row(label, "cases_query")
            step = self.add_step(
                row, "cases_query", "server", summary,
                {"filter": {"and": within_leaves}, "pageSize": CASES_PAGE_SIZE, "maxIds": MAX_CASE_IDS},
            )
            self.sizing.setdefault(ref, step)
            made = step_ref(step)
        elif info.kind == "gene":
            assert info.within is not None and info.gene is not None
            within = self.info(info.within)
            # The gene runs in the population: the scope, a filter cohort, or a computed cohort's case IDs.
            population = self.info(within.within) if within.kind == "tested" and within.within is not None else within
            cohort_filter: dict[str, JsonValue] = {"and": self.gen3(population.ref)}
            row = self.add_row(f"Find {info.gene}-mutated cases", "gene_cases")
            step = self.add_step(row, "gene_cases", "server", f"{info.gene}-mutated cases", {"gene": info.gene, "cohortFilter": cohort_filter})
            self.sizing.setdefault(ref, step)
            if within.kind == "tested":
                # C-UNTESTED: the gene runs in the scope, so check_subset means something; where its
                # ids are used they are intersected with the tested cases (TOOLS.md choice 4).
                tested = self.case_ids(within.ref)
                made = step_ref(step, intersect=str(tested["ref"]))
            else:
                made = step_ref(step)
        else:
            source = info.source
            assert source is not None
            definition = source.definition
            assert isinstance(definition, (ir.DifferenceDef, ir.IntersectionDef))
            if isinstance(definition, ir.DifferenceDef):
                # The subtrahend first: J3 finds the mutated cases, then the tested ones (TOOLS.md 5.3).
                b_raw = str(self.case_ids(definition.b)["ref"])
                a = self.bare_step(definition.a)
                row = self.add_row(f"Subtract to get {info.label}", "set_operation")
                inner, outer = self.info(definition.b), self.info(definition.a)
                # C-GUARD: a subset check before every difference.
                self.add_step(row, "check_subset", "exec", f"{inner.phrase} inside {outer.phrase}", {"inner": b_raw, "outer": a})
                step = self.add_step(row, "set_operation", "exec", f"{outer.short} minus {inner.short}", {"op": "difference", "a": a, "b": b_raw})
            else:
                a = self.bare_step(definition.a)
                b = self.bare_step(definition.b)
                row = self.add_row(f"Intersect to get {info.label}", "set_operation")
                step = self.add_step(
                    row, "set_operation", "exec", f"{self.info(definition.a).short} and {self.info(definition.b).short}",
                    {"op": "intersection", "a": a, "b": b},
                )
            self.sizing.setdefault(ref, step)
            made = step_ref(step)
        self.ids[ref] = made
        return made

    def bare_step(self, ref: str) -> str:
        """A step whose /observed/caseIds is exactly the cohort, for executor inputs that name steps."""
        if ref in self.bare:
            return self.bare[ref]
        ids = self.case_ids(ref)
        if ids.get("intersect") is None:
            self.bare[ref] = str(ids["ref"])
            return self.bare[ref]
        info = self.info(ref)
        row = self.steps[int(str(ids["ref"])[1:]) - 1].row
        step = self.add_step(
            row, "set_operation", "exec", f"{info.phrase} inside the mutation-tested cases",
            {"op": "intersection", "a": str(ids["ref"]), "b": str(ids["intersect"])},
        )
        self.bare[ref] = step
        self.sizing[ref] = step
        return step

    def count_fact(self, ref: str, label: str) -> str:
        """The fact that sizes a cohort used by a Gen3 count (survival groups, cohort size)."""
        info = self.info(ref)
        if info.kind == "base":
            return self.base_count_fact()
        if info.kind == "filter" and ref not in self.ids:
            step = self.sizing.get(ref)
            if step is None:
                if self.counts_row is None:
                    self.counts_row = self.add_row(label, "cohort_count")
                step = self.add_step(
                    self.counts_row, "cohort_count", "server", summarize(info.own, self.vocab) or info.label,
                    {"filter": {"and": self.gen3(ref)}},
                )
                self.sizing[ref] = step
            return f"f.{step}.count"
        return f"f.{self.bare_step(ref)}.count"

    # -- names and the save --------------------------------------------------------------------------

    def name(self, ref: str) -> str:
        """The run cohort's name: the IR's, a proposed "<label> (ORION)", a reused cohort's saved name,
        or "<label> cases"."""
        if ref in self.names:
            return self.names[ref]
        info = self.info(ref)
        source = info.source
        state = self.save_state.get(ref, "none")
        if info.kind == "base":
            made = self.scope.name
        elif source is not None and source.name is not None and source.origin == "planned":
            made = source.name
        elif state == "reused":
            made = self.reused[ref].name
        elif source is not None and source.name is not None:
            made = source.name
        elif state == "proposed":
            made = _proposed_name(info.label)
        else:
            made = cases_label(info.label)
        self.names[ref] = made
        return made

    def saved_match(self, ref: str) -> SavedCohort | CohortReuse | None:
        """C-SAVE: an identical saved cohort, known at compile time from the cohort's definition."""
        info = self.info(ref)
        if info.definition is None:
            return None
        sha = def_sha256(info.definition)
        hit = self.saved_shas.get(sha)
        if hit is not None:
            return hit
        source = info.source
        if source is not None and source.known_id is not None and source.known_sha == sha:
            present = {s.id: s for s in self.context.saved}
            if source.known_id in present:
                return present[source.known_id]
        return None


class Compiler:
    def __init__(
        self,
        vocab: Vocabulary,
        default_scope: DefaultScope,
        *,
        offer_edits: bool = True,
        snapshot_limit_bytes: int = SNAPSHOT_LIMIT_BYTES,
    ) -> None:
        """`offer_edits` fills `plan.editable` (contract "edit_plan fields"); false keeps the Day 1
        layout of `j1-stub`, whose plan offers none. `snapshot_limit_bytes` is the frame budget the
        compile-time size estimate checks (the contract's 32 KB; lower only in tests)."""
        self.vocab = vocab
        self.default_scope = default_scope
        self.offer_edits = offer_edits
        self.snapshot_limit_bytes = snapshot_limit_bytes

    # -- public API -------------------------------------------------------------------------------------

    def compile(
        self, plan: ir.Plan, context: CohortContext, ledger: SessionLedger | None = None, version: int = 1
    ) -> CompiledPlan | CompileStop:
        assumptions: list[Assumption] = []
        limitations: list[Limitation] = []
        scope: ResolvedScope | None = None
        try:
            self._check_panel(plan)
            scope = self._resolve_scope(plan, context, ledger, assumptions, limitations)
            alt = self._alt_scope(plan, scope, context, ledger)
            sources, tested_refs = _apply_untested(self._resolve_sources(plan, context, ledger))
            if tested_refs:
                assumptions.append(
                    Assumption(
                        code="EXCLUDE_UNTESTED",
                        text="Without a mutation means mutation-tested cases without it; cases without mutation data are left out and counted.",
                    )
                )
            builder = _Builder(
                vocab=self.vocab, scope=scope, sources={s.ref: s for s in sources}, order=[s.ref for s in sources],
                saved_shas={sha: saved for saved, sha in _saved_definitions(context)}, context=context,
            )
            first = plan.analyses[0]
            sized_scope = isinstance(first, ir.CohortSize) and first.cohort == ir.BASE_REF
            builder.add_scope_step(f"Count {scope.name}" if sized_scope else f"Find cases in {scope.name}")
            self._plan_saves(builder, plan.analyses)
            paraphrases: list[str] = []
            de_parts: list[_DeParts] = []
            for index, analysis in enumerate(plan.analyses):
                paraphrases.append(self._compile_analysis(builder, analysis, assumptions, de_parts, index))
            save_step = self._save_step(builder, de_parts)
            for parts in de_parts:
                self._finish_de(builder, parts, save_step)
            writes = save_step is not None
            consent = self._fit(builder, plan, paraphrases, limitations)
            compiled = CompiledPlan(
                version=version,
                signature=signature(scope, builder.steps, assumptions, writes),
                scope=scope,
                rows=builder.rows,
                steps=builder.steps,
                assumptions=assumptions,
                limitations=limitations,
                writes=writes,
                paraphrase=" ".join(paraphrases),
                alt_scope=None if alt is None else alt[0],
                alt_scope_choice=None if alt is None else alt[1],
                editable=self._editables(builder, plan, scope, de_parts) if self.offer_edits else [],
                cohorts=self._plan_cohorts(builder),
                consent=consent,
                open_apps=[NearestApp.of(a) for a in dict.fromkeys(builder.open_apps)],
                ledger=self._ledger_use(plan, scope, sources, ledger),
            )
            self._check_budget(compiled)
        except _Stop as stop:
            return CompileStop(code=stop.code, limitations=limitations + stop.limitations, assumptions=assumptions, scope=scope)
        return compiled

    def edit(
        self, current: CompiledPlan, current_ir: ir.Plan, action: EditPlanAction, context: CohortContext,
        ledger: SessionLedger | None = None,
    ) -> PlanEdit:
        """Compile an edit_plan into plan version n+1 with no model call (contract "The edit sequence").

        `current_ir` is the IR the current version compiled (planHistory) and `context` the cohort
        context it resolved against: the scope edit's own, else the ask's (or the last scope edit's).
        Raises EditRejected (INVALID_INPUT) for a field the plan does not offer, a cohortName for a ref
        that is not a proposed cohort, or a topN above the editable's max. Whether a proposed cohort
        is still `proposed` (STALE_ACTION) is the run's state, which the controller checks.
        """
        entry = self._editable_for(current, action)
        if isinstance(entry, EditableTopN) and isinstance(action, EditTopNAction) and action.value > entry.max:
            raise EditRejected("INVALID_INPUT", f"topN {action.value} is above this analysis's maximum {entry.max}")
        if entry.value == action.value:
            return PlanEdit(ir=current_ir, result=None)
        patched = _patch(current_ir, action)
        result = self.compile(patched, context, ledger, version=current.version + 1)
        if isinstance(result, CompiledPlan) and isinstance(action, EditScopeAction) and result.scope.choice() != current.scope.choice():
            # The new version names the scope it left, so the chip can switch back.
            result = result.model_copy(update={"alt_scope": current.scope, "alt_scope_choice": current.scope.choice()})
        return PlanEdit(ir=patched, result=result)

    def clarify(self, output: ir.Clarify, question: str, context: CohortContext | None = None) -> ClarifyPending | CompileStop:
        """The Clarifying card (FR-04, E10): one question from a code template and 2-5 options, each
        naming a vocabulary field with a value preview. The planner returns only the slot and option
        ids; options the vocabulary does not know are dropped, and fewer than two left stops the run
        (AMBIGUOUS_REQUEST) with no data call."""
        options: list[ClarifyOption] = []
        if output.slot in ("split_term", "cohort_field"):
            for option_id in output.option_ids:
                spec = self.vocab.categorical(option_id)
                if spec is not None:
                    options.append(ClarifyOption(option_id=spec.id, field=spec.id, label=spec.label, preview=spec.preview()))
        elif output.slot == "gene":
            for option_id in output.option_ids:
                gene = option_id.upper()
                if gene in PANEL:
                    options.append(ClarifyOption(option_id=gene, field=None, label=gene, preview=f"Cases with a {gene} mutation"))
        else:
            options = [o for o in (_scope_option(i, context, self.vocab) for i in output.option_ids) if o is not None]
        unique = list({o.option_id: o for o in options}.values())[:5]
        if len(unique) < 2:
            return CompileStop(
                code="AMBIGUOUS_REQUEST",
                limitations=[Limitation(
                    code="AMBIGUOUS_REQUEST", text="ORION could not tell which setting the question means, so nothing was run.",
                    unblock="Ask again and name the field, such as CGS risk category.",
                )],
                assumptions=[], scope=None,
            )
        term = _clarify_term(unique, question, self.vocab) if output.slot in ("split_term", "cohort_field") else None
        return ClarifyPending(kind="clarify", slot=output.slot, question=_question_for(output.slot, term), options=unique)

    def unsupported(self, output: ir.Unsupported) -> CompileStop:
        """The Unsupported card (E17, E18): code-template text and the nearest app, if any.

        A clinical-advice question names no app and carries no clinical content (G-5); an app that
        reads no cohort (Projects) cannot be opened on one, so it is never named.
        """
        apps: list[NearestApp] = []
        if output.reason != "CLINICAL_ADVICE":
            apps = [NearestApp.of(a) for a in dict.fromkeys(output.nearest_app_ids) if APPS[a].role != "out_of_scope"]
        limitation = Limitation(code=output.reason, text=UNSUPPORTED_TEXTS[output.reason], unblock=None, nearest_apps=apps)
        return CompileStop(code=output.reason, limitations=[limitation], assumptions=[], scope=None, status="Unsupported")

    # -- C-PANEL ----------------------------------------------------------------------------------------

    def _check_panel(self, plan: ir.Plan) -> None:
        outside: list[str] = []
        for cohort in plan.cohorts:
            definition = cohort.definition
            if isinstance(definition, ir.GeneMutatedDef) and definition.gene == "OTHER":
                outside.append("The gene asked about")
            if isinstance(definition, ir.FilterDef):
                for clause in definition.clauses:
                    if clause.field in GENE_FIELDS:
                        panel_values = set(self._panel_values(clause.field))
                        outside += [str(v) for v in clause.values if str(v).upper() not in panel_values]
        if outside:
            raise _stop(
                "OUT_OF_PANEL",
                f"{join_words(sorted(set(outside)))} is outside the six-gene mutation panel ({', '.join(PANEL)}), so nothing was run.",
                f"Ask about {join_words(list(PANEL)).replace(' and ', ' or ')}.",
            )

    def _panel_values(self, field_id: str) -> tuple[str, ...]:
        spec = self.vocab.field(field_id)
        return tuple(str(v).upper() for v in (spec.values or ())) if spec else PANEL

    # -- C-SCOPE ----------------------------------------------------------------------------------------

    def _resolve_scope(
        self, plan: ir.Plan, context: CohortContext, ledger: SessionLedger | None, assumptions: list[Assumption],
        limitations: list[Limitation],
    ) -> ResolvedScope:
        """A scope the question names, then the thread's scope for an explicit follow-up, then the default."""
        base = plan.base_cohort
        if base is not None and base.source == "saved_cohort":
            return self._saved_scope(base.cohort_name or "", context)
        if base is not None and base.source == "all_cases":
            return self._all_cases(context)
        if base is not None and base.source == "active_cohort":
            return self._active_scope(context)
        named_thread = base is not None and base.source == "thread_cohort"
        if named_thread or plan.follows is not None:
            resolved = self._thread_scope(plan, ledger, context)
            if resolved is not None:
                return resolved
            if named_thread:
                raise _stop(
                    "UNKNOWN_VALUE", "The earlier cohort this question names is not in the session ledger, so nothing was run.",
                    _UNBLOCK_LEDGER,
                )
            scope = self._default_scope(context, assumptions)
            limitations.append(
                Limitation(
                    code="UNKNOWN_VALUE",
                    text=f"This follows an earlier answer, but that answer's scope is not in the session ledger, so the default scope, {scope.name}, is used.",
                    unblock=_UNBLOCK_LEDGER,
                )
            )
            return scope
        return self._default_scope(context, assumptions)

    def _default_scope(self, context: CohortContext, assumptions: list[Assumption]) -> ResolvedScope:
        if self.default_scope == "all_cases":
            assumptions.append(Assumption(code="DEFAULT_SCOPE_ALL", text="No cohort named: using all cases"))
            return self._all_cases(context)
        scope = self._active_scope(context)
        assumptions.append(Assumption(code="DEFAULT_SCOPE_ACTIVE", text=f"No cohort named: using the active cohort, {scope.name}"))
        return scope

    def _all_cases(self, context: CohortContext) -> ResolvedScope:
        unfiltered = not context.active.filters.root
        count = context.active.case_count if unfiltered else None
        return ResolvedScope(source="all_cases", name=ALL_CASES, case_count=count, clauses=[])

    def _active_scope(self, context: CohortContext) -> ResolvedScope:
        """The browser's count is copied as is; null means the count is still pending."""
        active = context.active
        clauses = self._browser_clauses(active.filters, f"Your active cohort, {active.name}: ")
        # The contract names an unfiltered active cohort "All cases", whatever the browser calls it.
        name = active.name if active.filters.root else ALL_CASES
        return ResolvedScope(source="active_cohort", name=name, case_count=active.case_count, clauses=clauses, cohort_id=active.id)

    def _saved_scope(self, name: str, context: CohortContext) -> ResolvedScope:
        saved = self._find_saved(name, context)
        clauses = self._browser_clauses(saved.filters, f"Saved cohort {saved.name}: ")
        return ResolvedScope(source="saved_cohort", name=saved.name, case_count=saved.case_count, clauses=clauses, cohort_id=saved.id)

    def _find_saved(self, name: str, context: CohortContext) -> SavedCohort:
        wanted = name.strip().casefold()
        for saved in context.saved:
            if saved.name.strip().casefold() == wanted:
                return saved
        raise _stop(
            "UNKNOWN_VALUE", f"No saved cohort named {name or 'that'} is in this browser's cohort list, so nothing was run.",
            "Pick a saved cohort by its exact name, or ask about the active cohort.",
        )

    def _ledger_runs(self, ledger: SessionLedger | None, follows: str | None) -> list[LedgerRun]:
        """The runs a follow-up may take from: the one it follows, else every run, latest first."""
        runs = list(reversed(ledger.runs)) if ledger else []
        if follows is None:
            return runs
        return [r for r in runs if r.run_id == follows]

    def _thread_scope(self, plan: ir.Plan, ledger: SessionLedger | None, context: CohortContext) -> ResolvedScope | None:
        ref = plan.base_cohort.ledger_ref if plan.base_cohort is not None else None
        for run in self._ledger_runs(ledger, plan.follows):
            if ref is None:
                where = f"Earlier scope {run.scope.name}: "
                clauses = [self._vocab_clause(c, where, _UNBLOCK_LEDGER) for c in run.scope.clauses]
                definition = definition_of(clauses)
                return ResolvedScope(
                    source="thread_cohort", name=run.scope.name, case_count=self._browser_count(definition, context),
                    clauses=clauses, cohort_id=run.scope.cohort_id,
                )
            for cohort in run.cohorts:
                if cohort.ref == ref:
                    raw = clauses_of(cohort.definition) if cohort.definition is not None else cohort.clauses
                    clauses = [self._vocab_clause(c, f"Earlier cohort {cohort.name}: ", _UNBLOCK_LEDGER) for c in raw]
                    return ResolvedScope(
                        source="thread_cohort", name=cohort.name, case_count=self._browser_count(definition_of(clauses), context),
                        clauses=clauses, cohort_id=cohort.cohort_id, ledger_ref=ref,
                    )
        return None

    def _browser_count(self, definition: CanonicalDefinition | None, context: CohortContext) -> int | None:
        """The browser's count of an identical cohort (the active or a saved one), else pending."""
        if definition is None:
            return None
        sha = def_sha256(definition)
        active = definition_from_filter_set(context.active.filters)
        if active is not None and def_sha256(active) == sha:
            return context.active.case_count
        for saved, saved_sha in _saved_definitions(context):
            if saved_sha == sha:
                return saved.case_count
        return None

    def _alt_scope(
        self, plan: ir.Plan, scope: ResolvedScope, context: CohortContext, ledger: SessionLedger | None
    ) -> tuple[ResolvedScope, ScopeChoice] | None:
        """C-08, FR-19, E15: a follow-up after the active cohort changed names both scopes.

        The plan runs on the thread scope and offers the new active cohort (or, once switched, runs on
        the active cohort and offers the thread scope back). No alt scope when the two are the same
        cases, when the active cohort is unchanged since the earlier run, or when it cannot be read.
        """
        if plan.follows is None and (plan.base_cohort is None or plan.base_cohort.source != "thread_cohort"):
            return None
        if scope.source not in ("thread_cohort", "active_cohort"):
            return None
        try:
            thread_plan = plan.model_copy(update={"base_cohort": None}) if scope.source == "active_cohort" else plan
            thread = self._thread_scope(thread_plan, ledger, context)
            active = self._active_scope(context)
        except _Stop:
            return None
        if thread is None:
            return None
        if _same_cases(thread, active):
            return None
        if scope.source == "thread_cohort":
            if not self._active_changed(plan, ledger, context, thread):
                return None
            return active, active.choice()
        return thread, thread.choice()

    def _active_changed(self, plan: ir.Plan, ledger: SessionLedger | None, context: CohortContext, thread: ResolvedScope) -> bool:
        runs = self._ledger_runs(ledger, plan.follows)
        earlier = runs[0].active if runs else None
        if earlier is None:
            return True  # unknown: the scopes differ, so name both
        before = definition_from_filter_set(earlier.filters)
        now = definition_from_filter_set(context.active.filters)
        same = earlier.id == context.active.id and before is not None and now is not None and def_sha256(before) == def_sha256(now)
        return not same

    def _browser_clauses(self, filters: FilterSet, where: str) -> list[ir.Clause]:
        """A browser FilterSet is validated exactly like planner output; a problem stops the plan as a limitation."""
        clauses, problems = clauses_from_filter_set(filters)
        if problems:
            raise _Stop(
                problems[0].code,
                [Limitation(code=p.code, text=f"{where}{p.text} ORION cannot reproduce this cohort, so nothing was run.", unblock=_UNBLOCK_BROWSER) for p in problems],
            )
        return [self._vocab_clause(c, where, _UNBLOCK_BROWSER) for c in clauses]

    # -- C-VOCAB ----------------------------------------------------------------------------------------

    def _vocab_clause(self, clause: ir.Clause, where: str, unblock: str = _UNBLOCK_VOCAB) -> ir.Clause:
        checked = self.vocab.check_clause(clause)
        if isinstance(checked, VocabProblem):
            raise _stop_for(checked, where, unblock)
        return checked

    def _resolve_sources(self, plan: ir.Plan, context: CohortContext, ledger: SessionLedger | None) -> list[_Source]:
        """C-VOCAB on every cohort: filter clauses normalized, saved and thread cohorts read into filters."""
        out: list[_Source] = []
        for cohort in plan.cohorts:
            definition = cohort.definition
            if isinstance(definition, ir.FilterDef):
                clauses = [self._vocab_clause(c, "") for c in definition.clauses]
                out.append(_Source(cohort.ref, definition.model_copy(update={"clauses": clauses}), name=cohort.name))
            elif isinstance(definition, ir.SavedDef):
                saved = self._find_saved(definition.cohort_name, context)
                clauses = self._browser_clauses(saved.filters, f"Saved cohort {saved.name}: ")
                if not clauses:
                    raise _stop("UNKNOWN_VALUE", f"Saved cohort {saved.name} has no filter to reuse.", None)
                out.append(
                    _Source(cohort.ref, ir.FilterDef(kind="filter", clauses=clauses), "saved", cohort.name or saved.name, saved.id)
                )
            elif isinstance(definition, ir.ThreadDef):
                out.append(self._thread_source(cohort, definition, plan.follows, ledger))
            else:
                out.append(_Source(cohort.ref, definition, name=cohort.name))
        return out

    def _thread_source(self, cohort: ir.CohortDef, definition: ir.ThreadDef, follows: str | None, ledger: SessionLedger | None) -> _Source:
        """A thread cohort (session ledger) is rebuilt as a planned cohort from its definition: a filter's
        clauses, or a case-ID list as one case_id clause (TOOLS.md 5.8), checked like planner output."""
        runs = self._ledger_runs(ledger, follows) + [r for r in self._ledger_runs(ledger, None) if r.run_id != follows]
        for run in runs:
            for found in run.cohorts:
                if found.ref != definition.ledger_ref:
                    continue
                raw = clauses_of(found.definition) if found.definition is not None else found.clauses
                if not raw:
                    continue
                clauses = [self._vocab_clause(c, f"Earlier cohort {found.name}: ", _UNBLOCK_LEDGER) for c in raw]
                return _Source(
                    cohort.ref, ir.FilterDef(kind="filter", clauses=clauses), "thread", cohort.name or found.name, found.cohort_id,
                    found.def_sha256, found.definition_text, run.run_id, found.ref, isinstance(found.definition, CaseIdsDefinition),
                )
        raise _stop("UNKNOWN_VALUE", "An earlier cohort this question reuses is not in the session ledger.", _UNBLOCK_LEDGER)

    def _ledger_use(self, plan: ir.Plan, scope: ResolvedScope, sources: Sequence[_Source], ledger: SessionLedger | None) -> LedgerUse | None:
        """What the plan took from the session ledger (the record's `ledger`): the run it follows and
        each thread cohort it reuses, the thread scope's cohort first."""
        refs: list[LedgerRefUse] = []
        if scope.source == "thread_cohort" and scope.ledger_ref is not None:
            run = next((r for r in self._ledger_runs(ledger, plan.follows) if any(c.ref == scope.ledger_ref for c in r.cohorts)), None)
            if run is not None:
                refs.append(LedgerRefUse(run_id=run.run_id, ref=scope.ledger_ref, name=scope.name))
        for source in sources:
            if source.origin == "thread" and source.ledger_run is not None and source.ledger_ref is not None:
                refs.append(LedgerRefUse(run_id=source.ledger_run, ref=source.ledger_ref, name=source.name or source.ref))
        follows = plan.follows
        if follows is None and scope.source == "thread_cohort":
            follows = next((r.run_id for r in self._ledger_runs(ledger, None)), None)
        if follows is None and not refs:
            return None
        return LedgerUse(follows=follows, refs=refs)

    # -- analyses ------------------------------------------------------------------------------------

    def _plan_saves(self, builder: _Builder, analyses: Sequence[ir.Analysis]) -> None:
        """C-SAVE: the cohorts a full tool needs are proposed for saving, in both phrasings: a ranked
        planned cohort (J2, OncoMatrix opens on it) and both DE groups (J3). One identical to a saved
        cohort at compile time is reused instead (FR-08); `base` is the scope, never saved by a plan."""
        wanted: list[str] = []
        for analysis in analyses:
            if isinstance(analysis, ir.DifferentialExpression):
                wanted += [analysis.group1, analysis.group2]
            elif isinstance(analysis, ir.TopMutatedGenes):
                wanted.append(analysis.cohort)
        for ref in dict.fromkeys(r for r in wanted if r != ir.BASE_REF):
            hit = builder.saved_match(ref)
            if hit is None:
                builder.save_state[ref] = "proposed"
            else:
                builder.save_state[ref] = "reused"
                builder.reused[ref] = hit

    def _compile_analysis(
        self, builder: _Builder, analysis: ir.Analysis, assumptions: list[Assumption], de_parts: list[_DeParts], index: int
    ) -> str:
        if isinstance(analysis, ir.SurvivalCompare):
            if analysis.groups is not None:
                return self._survival_groups(builder, analysis.groups)
            return self._survival_split(builder, analysis, assumptions)
        if isinstance(analysis, ir.TopMutatedGenes):
            return self._top_genes(builder, analysis, assumptions)
        if isinstance(analysis, ir.DifferentialExpression):
            return self._de_reads(builder, analysis, assumptions, de_parts, index)
        return self._cohort_size(builder, analysis)

    def _split_field(self, analysis: ir.SurvivalCompare) -> tuple[FieldSpec, list[str]]:
        if analysis.split_term is None:
            raise _stop(
                "AMBIGUOUS_REQUEST", "A survival comparison needs a field to split by, or two cohorts to compare, so nothing was run.",
                "Name the field to compare, such as CGS risk category.",
            )
        spec = self.vocab.field(analysis.split_term)
        if spec is None:
            problem = VocabProblem("UNKNOWN_FIELD", analysis.split_term, f"{analysis.split_term} is not a field in the Virtual Lab vocabulary.")
            raise _stop_for(problem, "", "Name a Virtual Lab field.")
        if spec.values is None or spec.type not in ("enum", "multiselect"):
            raise _stop("UNSUPPORTED_FIELD", f"Survival can be split only by a categorical field, and {spec.label} is not one.", None)
        if analysis.split_values is None:
            values = [str(v) for v in spec.values]
        else:
            checked = self._vocab_clause(ir.Clause(field=spec.id, op="in", values=analysis.split_values), "")
            values = [str(v) for v in checked.values]
        if not 2 <= len(values) <= MAX_SURVIVAL_GROUPS:
            raise _stop(
                "UNSUPPORTED_FIELD",
                f"Survival compares two to six groups, and {spec.label} has {len(values)} values here, so nothing was run.",
                "Name two to six values to compare.",
            )
        return spec, values

    def _survival_split(self, builder: _Builder, analysis: ir.SurvivalCompare, assumptions: list[Assumption]) -> str:
        spec, values = self._split_field(analysis)
        code = spec.split_assumption or f"SPLIT_{spec.id.rsplit('.', 1)[-1].upper()}"
        assumptions.append(Assumption(code=code, text=f"{' vs. '.join(values)} from {spec.label}"))
        count_row = builder.add_row(
            f"Count both {spec.group} groups" if len(values) == 2 else f"Count the {len(values)} {spec.group} groups", "cohort_count"
        )
        groups: list[JsonValue] = []
        counts: list[str] = []
        for value in values:
            value_filter: dict[str, JsonValue] = {"and": builder.scope_leaves() + [{"in": {spec.id: [value]}}]}
            counts.append(builder.add_step(count_row, "cohort_count", "server", f"{spec.label} = {value}", {"filter": value_filter}))
            groups.append({"label": value, "filter": value_filter})
        # C-SPLIT: the split field's ProteinPaint twin is the chart's overlay term; no twin, no card.
        split: JsonValue = {"field": spec.id, "overlay": spec.pp_twin} if spec.pp_twin is not None else None
        survival_row = builder.add_row(f"Compare survival: Kaplan–Meier, split by {spec.label}", "survival_compare")
        builder.main_rows.append(survival_row)
        args: dict[str, JsonValue] = {"scope": builder.scope_filter(), "term": SURVIVAL_TERM, "split": split, "groups": groups}
        # The survival pre-guard reads each group's count fact, so the counts are dependencies.
        survival = builder.add_step(survival_row, "survival_compare", "server", f"{len(values)} groups, Kaplan–Meier", args, counts)
        builder.add_step(survival_row, "derive_km_median", "exec", f"{len(values)} curves", {"curves": survival})
        if split is None:
            builder.open_apps.append("CohortComparison")
        prefix = f"{spec.value_prefix} " if spec.value_prefix else ""
        return f"Compare overall survival between {prefix}{join_words(values)} cases in {builder.scope.name}."

    def _survival_groups(self, builder: _Builder, refs: Sequence[str]) -> str:
        """Survival between cohorts (TOOLS.md 5.1): each group sized by its own count fact, split null,
        no survival card (no overlay term), Cohort Comparison offered as the hand-off."""
        labels = [builder.info(ref).label for ref in refs]
        if len(set(labels)) != len(labels):
            raise _stop("AMBIGUOUS_REQUEST", "Two of the groups to compare are the same cohort, so nothing was run.", "Name different cohorts.")
        row_label = "Count both groups" if len(refs) == 2 else f"Count the {len(refs)} groups"
        groups: list[JsonValue] = []
        counts: list[str] = []
        for ref, label in zip(refs, labels):
            fact = builder.count_fact(ref, row_label)
            counts.append(fact.split(".")[1])
            groups.append({"label": label, "filter": {"and": builder.gen3(ref)}})
        survival_row = builder.add_row(f"Compare survival: Kaplan–Meier, {' vs. '.join(labels)}", "survival_compare")
        builder.main_rows.append(survival_row)
        args: dict[str, JsonValue] = {"scope": builder.scope_filter(), "term": SURVIVAL_TERM, "split": None, "groups": groups}
        survival = builder.add_step(survival_row, "survival_compare", "server", f"{len(refs)} groups, Kaplan–Meier", args, counts)
        builder.add_step(survival_row, "derive_km_median", "exec", f"{len(refs)} curves", {"curves": survival})
        builder.open_apps.append("CohortComparison")
        return f"Compare overall survival between {join_words(labels)} in {builder.scope.name}."

    def _top_genes(self, builder: _Builder, analysis: ir.TopMutatedGenes, assumptions: list[Assumption]) -> str:
        n = analysis.n if analysis.n is not None else TOP_N_DEFAULT_GENES
        if analysis.n is None:
            assumptions.append(Assumption(code="TOP_N_DEFAULT", text=f"Top {n} genes: the question names no number"))
        if analysis.cohort == ir.BASE_REF:
            filter0 = builder.scope_filter()
            subject = builder.scope.name
            among = f"in {builder.scope.name}"
        else:
            filter0 = {"and": [case_id_leaf(builder.case_ids(analysis.cohort))]}
            info = builder.info(analysis.cohort)
            own = ", ".join(str(v) for c in info.own for v in c.values)
            subject = f"{own} cases" if info.kind == "filter" and info.own and (info.source is None or not info.source.case_ids) else cases_label(info.label)
            among = f"among {cases_label(info.label)} in {builder.scope.name}"
        row = builder.add_row(f"Rank mutated genes: top {n}", "top_mutated_genes")
        builder.main_rows.append(row)
        genes = builder.add_step(row, "top_mutated_genes", "server", f"{subject}, top {n}", {"filter0": filter0, "geneCount": n})
        builder.add_step(
            row, "rank_top_n", "exec", f"Top {n} by mutated cases, ties disclosed",
            {"rows": genes, "key": "gene", "rankBy": "cases", "n": n, "ascending": False, "tieBreak": "source"},
        )
        lead = "List the most frequently mutated gene" if n == 1 else f"List the {n} most frequently mutated genes"
        return f"{lead} {among}."

    def _de_reads(
        self, builder: _Builder, analysis: ir.DifferentialExpression, assumptions: list[Assumption], de_parts: list[_DeParts], index: int
    ) -> str:
        """J3 up to the save (TOOLS.md 5.3): both groups' case IDs, their samples and RNA-seq eligibility."""
        if analysis.group1 == analysis.group2:
            raise _stop("AMBIGUOUS_REQUEST", "Differential expression needs two different groups, so nothing was run.", "Name two cohorts to compare.")
        method = analysis.method or DEFAULT_DE_METHOD
        rank_key = analysis.rank_by or DEFAULT_RANK_BY
        rank_by, ascending = DE_RANK_BY[rank_key]
        thresholds = (
            DeThresholds(
                p_value_cutoff=analysis.thresholds.p_value_cutoff, p_value_type=analysis.thresholds.p_value_type,
                fold_change_cutoff=analysis.thresholds.fold_change_cutoff,
            )
            if analysis.thresholds is not None else VIEWER_THRESHOLDS
        )
        top_n = analysis.top_n if analysis.top_n is not None else TOP_N_DEFAULT_DE
        control, case = builder.case_ids(analysis.group1), builder.case_ids(analysis.group2)
        if analysis.method is None:
            assumptions.append(Assumption(code="DE_METHOD_WILCOXON", text="Method: Wilcoxon rank-sum"))
        if analysis.rank_by is None:
            assumptions.append(Assumption(code="RANK_ADJ_P", text="Genes ranked by adjusted p-value"))
        if analysis.thresholds is None:
            assumptions.append(
                Assumption(code="DE_THRESHOLDS_VIEWER", text="Thresholds: adjusted p < 0.05 and |log2FC| > 0.3, the viewer's defaults")
            )
        if analysis.top_n is None:
            assumptions.append(Assumption(code="TOP_N_DEFAULT", text=f"Top {top_n} genes: the question names no number"))
        names = self._group_names(builder, analysis.group1, analysis.group2)
        assumptions.append(
            Assumption(code="CONTROL_FIRST", text=f"{names[0]} is the control group: fold change is log2({names[1]} / {names[0]})")
        )
        row = builder.add_row("Check RNA-seq-eligible group sizes", "de_preanalysis")
        samples = builder.add_step(
            row, "sample_lists", "server", "Both groups as ProteinPaint samples",
            {"groups": [{"key": "control", "caseIds": control}, {"key": "case", "caseIds": case}]},
        )
        pre = builder.add_step(
            row, "de_preanalysis", "server", "RNA-seq-eligible samples per group",
            {"groups": [
                {"key": "control", "sampleIds": step_ref(samples, "/observed/groups/0/sampleIds")},
                {"key": "case", "sampleIds": step_ref(samples, "/observed/groups/1/sampleIds")},
            ]},
        )
        de_parts.append(_DeParts(analysis, (analysis.group1, analysis.group2), samples, pre, method, rank_by, ascending, thresholds, top_n, index))
        return self._de_paraphrase(builder, analysis, top_n, rank_by)

    def _group_names(self, builder: _Builder, group1: str, group2: str) -> tuple[str, str]:
        first, second = builder.name(group1), builder.name(group2)
        if first == second:
            second = f"{second[:97]} 2"
            builder.names[group2] = second
        return first, second

    def _de_paraphrase(self, builder: _Builder, analysis: ir.DifferentialExpression, top_n: int, rank_by: RankBy) -> str:
        control, case = builder.info(analysis.group1), builder.info(analysis.group2)
        rank = RANK_LABELS[rank_by]
        if control.kind == "difference" and case.kind == "gene" and case.gene is not None:
            who = "mutation-tested cases" if control.root == ir.BASE_REF else f"mutation-tested {cases_label(builder.info(control.root).label)}"
            between = f"{who} with and without a {case.gene} mutation"
        else:
            between = f"{cases_label(control.label)} and {cases_label(case.label)}"
        return f"Compare gene expression between {between} in {builder.scope.name}, and list the top {top_n} genes by {rank}."

    def _save_step(self, builder: _Builder, de_parts: Sequence[_DeParts]) -> str | None:
        """C-SAVE: one save_cohorts step for every proposed cohort, after the reads that size them and
        check eligibility, before any DE run (J3), or after the ranking (J2: TOOLS.md choice 1)."""
        proposed = [ref for ref, state in builder.save_state.items() if state == "proposed"]
        if not proposed:
            return None
        eligible: dict[str, dict[str, JsonValue]] = {}
        for parts in de_parts:
            for i, ref in enumerate(parts.groups):
                eligible.setdefault(ref, step_ref(parts.pre, f"/observed/groups/{i}/eligible"))
        entries: list[JsonValue] = []
        defining: set[str] = set()
        for ref in proposed:
            entry: dict[str, JsonValue] = {"ref": ref, "name": builder.name(ref)}
            if ref in eligible:
                entry["eligible"] = eligible[ref]
            entries.append(entry)
            ids = builder.case_ids(ref)
            defining |= _refs_in(ids)
            if ref in builder.sizing:
                defining.add(builder.sizing[ref])
        row = builder.add_row(f"Save {len(proposed)} cohort{'s' if len(proposed) != 1 else ''}", "save_cohorts")
        return builder.add_step(
            row, "save_cohorts", "client", ", ".join(builder.name(ref) for ref in proposed), {"cohorts": entries}, sorted(defining)
        )

    def _finish_de(self, builder: _Builder, parts: _DeParts, save_step: str | None) -> None:
        """J3 after the save (TOOLS.md 5.3): de_run on the read-back names (DeRunStepArgs), then rank_top_n."""
        after = [save_step] if save_step is not None else []
        thresholds = parts.thresholds.model_dump(mode="json", by_alias=True)
        row = builder.add_row(f"Run DE: {DE_METHOD_SHORT[parts.method]}, rank by {RANK_SHORT[parts.rank_by]}", "de_run")
        builder.main_rows.insert(min(parts.index, len(builder.main_rows)), row)
        names = (builder.name(parts.groups[0]), builder.name(parts.groups[1]))
        de = builder.add_step(
            row, "de_run", "server", f"{DE_METHOD_SHORT[parts.method]}, {names[0]} vs. {names[1]}",
            {
                "groups": [
                    {"name": names[i], "cohort": parts.groups[i], "sampleIds": step_ref(parts.samples, f"/observed/groups/{i}/sampleIds")}
                    for i in range(2)
                ],
                "method": parts.method,
                "thresholds": thresholds,
            },
            after,
        )
        builder.add_step(
            row, "rank_top_n", "exec", f"Top {parts.top_n} by {RANK_SHORT[parts.rank_by]}",
            {
                "rows": de, "key": "gene_name", "rankBy": parts.rank_by, "n": parts.top_n, "ascending": parts.ascending,
                "tieBreak": "gene_name", "thresholds": thresholds,
            },
            after,
        )

    def _cohort_size(self, builder: _Builder, analysis: ir.CohortSize) -> str:
        """TOOLS.md 5.4: a Gen3 count for the scope or a filter cohort; set_operation size for a computed one."""
        ref = analysis.cohort
        info = builder.info(ref)
        name = builder.scope.name if ref == ir.BASE_REF else cohort_label(builder.name(ref))
        if info.kind == "base":
            if builder.scope_step is None:
                row = builder.add_row(f"Count {name}", "cohort_count")
                builder.main_rows.append(row)
                builder.add_step(row, "cohort_count", "server", builder.scope.name, {"filter": {"and": []}})
            else:
                builder.main_rows.append(builder.steps[int(builder.scope_step[1:]) - 1].row)
        elif info.kind == "filter":
            row = builder.add_row(f"Count {name}", "cohort_count")
            builder.main_rows.append(row)
            step = builder.add_step(row, "cohort_count", "server", summarize(info.own, self.vocab) or name, {"filter": {"and": builder.gen3(ref)}})
            builder.sizing.setdefault(ref, step)
        else:
            source = builder.bare_step(ref)
            row = builder.add_row(f"Count {name}", "set_operation")
            builder.main_rows.append(row)
            builder.add_step(row, "set_operation", "exec", f"Size of {name}", {"op": "size", "a": source})
        return f"Count the cases in {name}."

    # -- C-FIT --------------------------------------------------------------------------------------

    def _fit(self, builder: _Builder, plan: ir.Plan, paraphrases: list[str], limitations: list[Limitation]) -> ConsentPending | None:
        """C-FIT: each named tool against the capability matrix. It fits when it runs one of the plan's
        analyses or steps; it fits with hand-off when its job runs in another app (the plan card says
        why and the answer offers Open <app>); otherwise it is a substitution and the plan waits for
        consent before any data call (FR-02, E7). Named tools never enter the signature."""
        kinds = {a.kind for a in plan.analyses}
        tools = {s.tool for s in builder.steps}
        consent: ConsentPending | None = None
        for named in dict.fromkeys(t.tool for t in plan.named_tools):
            cap = CAPABILITIES[named]
            if cap.fits & kinds or cap.steps & tools or cap.app_id is None:
                continue
            hand_off = [i for i, a in enumerate(plan.analyses) if a.kind in cap.hand_off]
            if hand_off:
                self._reason(builder, hand_off[0], f"; {cap.title}, which you named, opens on this cohort from the answer")
                builder.open_apps.append(cap.app_id)
                continue
            if consent is not None:
                continue
            kind = plan.analyses[0].kind
            proposed = ANALYSIS_APPS[kind]
            proposed_cap = _capability_of(proposed)
            ask = ANALYSIS_ASKS[kind]
            consent = ConsentPending(
                kind="consent", named=NearestApp.of(cap.app_id), proposed=NearestApp.of(proposed),
                reason=f"{cap.title} {cap.does}; {app_title(proposed)} {ask.proposed}.",
            )
            limitations.append(
                Limitation(
                    code="NAMED_TOOL_MISMATCH",
                    text=f"{cap.title} {cap.does} and cannot {ask.verb}, so nothing has run yet.",
                    unblock=f"Use {app_title(proposed)}, which {proposed_cap.does if proposed_cap else ask.proposed}, or cancel.",
                    nearest_apps=[NearestApp.of(proposed)],
                )
            )
            self._reason(builder, 0, f"; {cap.title}, which you named, {cap.does}")
            paraphrases[0] = f"{paraphrases[0].rstrip('.')}, in {app_title(proposed)} instead of {cap.title}."
        return consent

    def _reason(self, builder: _Builder, analysis_index: int, suffix: str) -> None:
        if analysis_index >= len(builder.main_rows):
            return
        row = builder.main_rows[analysis_index] - 1
        builder.rows[row] = builder.rows[row].model_copy(update={"reason": builder.rows[row].reason + suffix})

    # -- plan.editable --------------------------------------------------------------------------------

    def _editables(self, builder: _Builder, plan: ir.Plan, scope: ResolvedScope, de_parts: Sequence[_DeParts]) -> list[Editable]:
        """What the plan card may offer (contract "edit_plan fields", TOOLS.md 6.4), each with its value."""
        out: list[Editable] = [EditableScope(field="scope", label="Scope", value=scope.choice())]
        first_de = de_parts[0] if de_parts else None
        if first_de is not None:
            out.append(EditableMethod(field="method", label="Method", value=first_de.method))
        ranked = next((a for a in plan.analyses if isinstance(a, (ir.DifferentialExpression, ir.TopMutatedGenes))), None)
        if isinstance(ranked, ir.DifferentialExpression):
            out.append(EditableTopN(field="topN", label="Top N", value=ranked.top_n or TOP_N_DEFAULT_DE, max=MAX_TOP_N_DE))
        elif isinstance(ranked, ir.TopMutatedGenes):
            out.append(EditableTopN(field="topN", label="Top N", value=ranked.n or TOP_N_DEFAULT_GENES, max=MAX_TOP_N_GENES))
        if first_de is not None:
            out.append(EditableThresholds(field="thresholds", label="Thresholds", value=first_de.thresholds))
        roles = {first_de.groups[0]: "Control cohort name", first_de.groups[1]: "Case cohort name"} if first_de else {}
        for ref, state in builder.save_state.items():
            if state == "proposed":
                out.append(EditableCohortName(field="cohortName", label=roles.get(ref, "Cohort name"), value=builder.name(ref), ref=ref))
        return out

    def _editable_for(self, plan: CompiledPlan, action: EditPlanAction) -> Editable:
        for entry in plan.editable:
            if entry.field != action.field:
                continue
            if isinstance(entry, EditableCohortName) and entry.ref != getattr(action, "ref", None):
                continue
            return entry
        what = f"{action.field} {getattr(action, 'ref', '')}".strip()
        raise EditRejected("INVALID_INPUT", f"plan version {plan.version} cannot take the edit {what}")

    # -- run cohorts ------------------------------------------------------------------------------------

    def _plan_cohorts(self, builder: _Builder) -> list[PlanCohort]:
        scope = builder.scope
        base_step = builder.scope_step if builder.scope_step is not None else "s1"
        out = [
            PlanCohort(
                ref=ir.BASE_REF, role="scope", source=scope.source, name=scope.name, definition_text=builder.info(ir.BASE_REF).text,
                definition=scope.definition(), ids=None if builder.scope_step is None else StepRef(ref=builder.scope_step, pointer=CASE_IDS),
                step_id=base_step, count_fact_id=builder.base_count_fact(), save="none", cohort_id=scope.cohort_id, saved_name=None,
            )
        ]
        for ref in builder.order:
            if ref not in builder.infos:
                continue  # a cohort no analysis uses compiles to no step
            info = builder.info(ref)
            ids = builder.ids.get(ref)
            sizing = builder.sizing.get(ref)
            if sizing is None and ids is not None and ids.get("intersect") is None:
                sizing = str(ids["ref"])
            state = builder.save_state.get(ref, "none")
            reused = builder.reused.get(ref)
            reused_id = reused.id if isinstance(reused, SavedCohort) else reused.cohort_id if reused is not None else None
            out.append(
                PlanCohort(
                    ref=ref, role="planned", source="planned", name=builder.name(ref), definition_text=info.text, definition=info.definition,
                    ids=None if ids is None else StepRef.model_validate(ids), step_id=sizing,
                    count_fact_id=None if sizing is None else f"f.{sizing}.count", save=state, cohort_id=reused_id,
                    saved_name=None if reused is None else reused.name,
                )
            )
        return out

    # -- the budget and the 32 KB snapshot --------------------------------------------------------------

    def _check_budget(self, plan: CompiledPlan) -> None:
        """CALL_BUDGET: each step is one counted call and a run makes at most MAX_TOOL_CALLS_PER_RUN;
        a plan whose largest view would not fit one 32 KB snapshot is kept inside the budget the same
        way (contract "Size: 32 KB"). REQUEST_TOO_LARGE: a step whose own args already exceed the
        gateway's 8 MiB body limit. Both stop at compile time, with no data call."""
        if len(plan.steps) > MAX_TOOL_CALLS_PER_RUN:
            raise _stop(
                "CALL_BUDGET",
                f"This plan needs {len(plan.steps)} tool calls, more than the {MAX_TOOL_CALLS_PER_RUN} one run may make, so nothing was run.",
                "Ask about fewer analyses or fewer groups at a time.",
            )
        for step in plan.steps:
            if len(json.dumps(step.args, separators=(",", ":")).encode("utf-8")) > MAX_SAMPLE_LIST_BODY_BYTES:
                raise _stop(
                    "REQUEST_TOO_LARGE", f"Step {step.id} would send more than the 8 MiB the gateway accepts, so nothing was run.",
                    "Narrow the cohorts.",
                )
        size = largest_snapshot_bytes(plan)
        if size is None or size > self.snapshot_limit_bytes:
            raise _stop(
                "CALL_BUDGET", "This plan would produce more results than one answer can show, so nothing was run.",
                "Ask about fewer analyses, or a smaller top N.",
            )


def _capability_of(app_id: AppId) -> Capability | None:
    return next((c for c in CAPABILITIES.values() if c.app_id == app_id), None)


def _same_cases(left: ResolvedScope, right: ResolvedScope) -> bool:
    a, b = left.definition(), right.definition()
    return a is not None and b is not None and def_sha256(a) == def_sha256(b)


def _patch(plan: ir.Plan, action: EditPlanAction) -> ir.Plan:
    """The IR version n+1 compiles: the edited value written where the question would have stated it."""
    if isinstance(action, EditScopeAction):
        value = action.value
        base = ir.BaseCohort(source=value.source, cohort_name=value.cohort_name, ledger_ref=value.ledger_ref)
        return plan.model_copy(update={"base_cohort": base})
    if isinstance(action, EditCohortNameAction):
        if all(c.ref != action.ref for c in plan.cohorts):
            raise EditRejected("INVALID_INPUT", f"{action.ref} is not a planned cohort")
        cohorts = [c.model_copy(update={"name": action.value}) if c.ref == action.ref else c for c in plan.cohorts]
        return plan.model_copy(update={"cohorts": cohorts})
    analyses = list(plan.analyses)
    for i, analysis in enumerate(analyses):
        patched: ir.Analysis | None = None
        if isinstance(action, EditTopNAction) and isinstance(analysis, ir.DifferentialExpression):
            patched = analysis.model_copy(update={"top_n": action.value})
        elif isinstance(action, EditTopNAction) and isinstance(analysis, ir.TopMutatedGenes):
            if action.value > MAX_TOP_N_GENES:
                raise EditRejected("INVALID_INPUT", f"topN {action.value} is above {MAX_TOP_N_GENES}")
            patched = analysis.model_copy(update={"n": action.value})
        elif isinstance(action, EditMethodAction) and isinstance(analysis, ir.DifferentialExpression):
            patched = analysis.model_copy(update={"method": action.value})
        elif isinstance(action, EditThresholdsAction) and isinstance(analysis, ir.DifferentialExpression):
            t = action.value
            stated = ir.Thresholds(p_value_cutoff=t.p_value_cutoff, p_value_type=t.p_value_type, fold_change_cutoff=t.fold_change_cutoff)
            patched = analysis.model_copy(update={"thresholds": stated})
        if patched is not None:
            analyses[i] = patched
            return plan.model_copy(update={"analyses": analyses})
    raise EditRejected("INVALID_INPUT", f"the plan has no analysis that takes {action.field}")


# --- The largest view a plan can produce (the 32 KB estimate) ----------------------------------------------

_LABEL: Final = "x" * 48
_TEXT: Final = "x" * 60
_SHA: Final = "0" * 64
_LAUNCHERS: Final[dict[str, ViewerKind]] = {"survival_compare": "survival", "top_mutated_genes": "oncomatrix", "de_run": "deDirect"}
_RANK_BYS: Final[dict[str, RankBy]] = {str(r): r for r in get_args(RankBy)}
_RANK_SOURCES: Final[dict[str, RankSourceTool]] = {"de_run": "de_run", "top_mutated_genes": "top_mutated_genes"}


def _rank_of(plan: CompiledPlan, step: StepSpec) -> tuple[RankSourceTool, RankBy] | None:
    source = _RANK_SOURCES.get(plan.step(str(step.args["rows"])).tool)
    rank_by = _RANK_BYS.get(str(step.args["rankBy"]))
    return None if source is None or rank_by is None else (source, rank_by)


def _sample(unit: str) -> int | float | str:
    samples: dict[str, int | float | str] = {"p": 1.2345e-10, "log2FC": -1.2345, "gene": "ABCDEFGH", "days": 1095.5}
    return samples.get(unit, 9999)


def _fact(fact_id: str, step: StepSpec, unit: str) -> Fact:
    return Fact(
        id=fact_id, label=_LABEL, value=_sample(unit), unit=unit, step_id=step.id, row=step.row, tool=step.tool,
        json_pointer="/observed/groups/0/value",
    )


def _group_count(step: StepSpec) -> int:
    groups = step.args.get("groups")
    return len(groups) if isinstance(groups, list) else 2


def _estimated_facts(plan: CompiledPlan) -> list[tuple[StepSpec, list[Fact]]]:
    """Every fact each step can yield at most: per-group facts for every group, and ranked rows up to
    VIEW_RANK_CAP in all (in plan order) with each ranking's count fact."""
    out: list[tuple[StepSpec, list[Fact]]] = []
    shown = 0
    for step in plan.steps:
        facts: list[Fact] = []
        tool = tool_spec(step.tool).name
        if tool == "rank_top_n":
            rank = _rank_of(plan, step)
            rows = min(int(str(step.args["n"])), VIEW_RANK_CAP - shown)
            shown += rows
            specs = fact_specs("rank_top_n", source=rank[0], rank_by=rank[1]) if rank is not None else ()
            for pos in range(1, rows + 1):
                facts += [_fact(f"f.{step.id}.{spec.suffix.replace('${pos}', str(pos))}", step, spec.unit) for spec in specs if spec.repeat == "row"]
            facts.append(_fact(f"f.{step.id}.count", step, "genes"))
        else:
            if tool in ("sample_lists", "de_preanalysis"):
                keys = list(_DE_KEYS)
            elif tool == "derive_km_median":
                keys = [f"g{i}" for i in range(1, _group_count(plan.step(str(step.args["curves"]))) + 1)]
            else:
                keys = [f"g{i}" for i in range(1, _group_count(step) + 1)]
            for spec in fact_specs(tool):
                if spec.repeat == "group":
                    facts += [_fact(f"f.{step.id}.{spec.suffix.replace('${key}', key)}", step, spec.unit) for key in keys]
                else:
                    facts.append(_fact(f"f.{step.id}.{spec.suffix}", step, spec.unit))
        out.append((step, facts))
    return out


def _segments(facts: Sequence[Fact]) -> list[Segment]:
    segments: list[Segment] = []
    for fact in facts:
        segments += [Segment(text=_TEXT[:24]), Segment(text=format_fact(fact.value, fact.unit), fact_id=fact.id)]
    return segments + [Segment(text=".")]


def _limitation(cited: Sequence[Fact]) -> Limitation:
    line = Limitation(code="TIES_AT_CUTOFF", text="x" * 200, unblock=_TEXT)
    return line.model_copy(update={"segments": _segments(cited)}) if cited else line


def largest_view(plan: CompiledPlan, *, paused: bool = False) -> RunView:
    """The largest RunView the plan can produce, for the compile-time 32 KB check: every step done
    with summaries, every fact it can yield, Observed citing each, the most model text the composer
    may write (4 + 3 statements of 300 characters, three follow-ups and open links), a viz card per
    analysis with a full frozen table; `paused`: the save pause with every pending cohort instead."""
    step_facts = _estimated_facts(plan)
    facts = [f for _, fs in step_facts for f in fs]
    cited = facts[:3]
    observed = [Sentence(segments=_segments(fs)) for _, fs in step_facts if fs]
    # The composer's caps: 4 interpretation and 3 hypothesis statements of 300 characters, each citing a fact.
    model = [Sentence(segments=[Segment(text="x" * 290), *_segments(cited[:1])], cites=[cited[0].id]) for _ in range(7)] if cited else []
    refs = [ir.BASE_REF] if any(c.ref == ir.BASE_REF for c in plan.cohorts) else []
    viz: list[VizSpec] = []
    for step in (s for s in plan.steps if s.tool in _LAUNCHERS):
        template = VIZ_TEMPLATES[_LAUNCHERS[step.tool]]
        columns = list(template.columns)
        cells = [Segment(text="9,999", fact_id=cited[0].id) if cited else Segment(text="x") for _ in columns]
        viz.append(
            VizSpec(
                viz_id=f"v{len(viz) + 1}", app_id=template.app_id, launcher=template.launcher, cites_steps=[step.id], pinned=None,
                pinned_hash=_SHA, title="x" * 80, caption="x" * 120, request_pinned_hash=_SHA,
                frozen=FrozenTable(columns=columns, rows=[cells] * 10, note=[Segment(text=_TEXT), *cells[:1]]), cohort_refs=refs,
            )
        )
    write = plan.write_step()
    pending: ConfirmPending | None = None
    if paused and write is not None:
        pending = ConfirmPending(
            kind="confirm", nonce="0" * 32, plan_signature=plan.signature, plan_version=plan.version, step_id=write.id,
            cohorts=[
                PendingCohort(
                    ref=c.ref, name=c.name, definition_text=_TEXT, definition=None, def_sha256=_SHA, case_count=9999, eligible_count=9999,
                    reuse=CohortReuse(cohort_id="x" * 36, name=c.name), case_count_fact_id=f"f.{write.id}.count",
                    eligible_count_fact_id=f"f.{write.id}.eligible.control",
                )
                for c in SaveCohortsStepArgs.model_validate(write.args).cohorts
            ],
        )
    done = StepView.model_validate
    return RunView(
        schema_="orion.run-view/v1", thread_id="0" * 32, run_id="0" * 32, revision=99,
        status="AwaitingConfirmation" if pending is not None else "Complete", code=None, question="x" * MAX_QUESTION_CHARS,
        updated_at="2026-11-05T00:00:00.000Z", plan=plan.view(),
        steps=[done({**s.queued().model_dump(), "status": "Done", "outputSummary": _TEXT, "durationMs": 99999}) for s in plan.steps],
        facts=facts, observed=observed,
        # Per analysis a coverage line and a line with chips (ties, eligible counts), plus two run-wide lines.
        limitations=[_limitation(cited if i % 2 else []) for i in range(2 * max(1, len(viz)) + 2)],
        interpretation=model[:4], hypotheses=model[4:], withheld_reason=None,
        viz_specs=[] if pending is not None else viz, pending=pending,
        cohorts=[c.view(9999).model_copy(update={"def_sha256": _SHA}).to_view() for c in plan.cohorts],
        follow_ups=[FollowUp(label="x" * 60, question="x" * 200, kind="survival_compare")] * 3,
        open_links=[NearestApp.of("CohortComparison")] * 3,
    )


def largest_snapshot_bytes(plan: CompiledPlan) -> int | None:
    """The SSE frame size of the plan's largest view after the contract's trimming (events.fit_view),
    or None when no trimming makes it fit 32 KB. A plan that writes is checked at its save pause too."""
    sizes: list[int] = []
    for paused in (False, True) if plan.write_step() is not None else (False,):
        try:
            event = events.state_snapshot(largest_view(plan, paused=paused))
        except events.SnapshotTooLarge:
            return None
        sizes.append(len(events.encode(event).encode("utf-8")))
    return max(sizes)


# --- Clarification text (code templates; the planner returns only codes) ------------------------------------


def _clarify_term(options: Sequence[ClarifyOption], question: str, vocab: Vocabulary) -> str | None:
    text = f" {_norm_text(question)} "
    for option in options:
        spec = vocab.field(option.field) if option.field else None
        for value in (spec.values or ()) if spec else ():
            word = _norm_text(str(value))
            if len(word) >= 3 and re.search(rf"\b{re.escape(word)}\b", text):
                return word
    return None


def _scope_option(option_id: str, context: CohortContext | None, vocab: Vocabulary) -> ClarifyOption | None:
    if option_id == "all_cases":
        return ClarifyOption(option_id=option_id, field=None, label=ALL_CASES, preview="No filters")
    if context is None:
        return None
    if option_id == "active_cohort":
        clauses, problems = clauses_from_filter_set(context.active.filters)
        preview = "No filters" if not clauses else summarize(clauses, vocab)
        return None if problems else ClarifyOption(option_id=option_id, field=None, label=f"Active cohort: {context.active.name}", preview=preview)
    for saved in context.saved:
        if saved.id == option_id:
            clauses, problems = clauses_from_filter_set(saved.filters)
            if problems:
                return None
            return ClarifyOption(option_id=option_id, field=None, label=saved.name, preview=summarize(clauses, vocab) or "No filters")
    return None


def _question_for(slot: str, term: str | None) -> str:
    if slot == "split_term":
        return f"Which classification defines {term}?" if term else "Which field should split the groups?"
    if slot == "cohort_field":
        return f"Which field defines {term}?" if term else "Which field defines the cohort?"
    if slot == "gene":
        return "Which gene do you mean?"
    return "Which cohort should the analysis use?"
