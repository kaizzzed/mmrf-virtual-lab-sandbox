"""Compiler: planner IR to a signed plan (RFA rev 25, A2). Day 1 AM rules: C-SCOPE, C-VOCAB,
C-PANEL and C-UNTESTED, plus the step layout the J1 walking skeleton needs.

A compile-time stop makes no data call, so a CompileStop carries zero steps.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Final

from pydantic import BaseModel, ConfigDict, JsonValue
from pydantic.alias_generators import to_camel

from . import ir
from .catalog import ANALYSIS_APPS, TOOL_REASONS
from .models import (
    Assumption,
    CohortContext,
    Code,
    FilterSet,
    HexId,
    Limitation,
    NearestApp,
    PlanRow,
    PlanView,
    SavedCohort,
    Scope,
    ScopeSource,
    StepKind,
    StepView,
)
from .settings import DefaultScope
from .vocab import FieldSpec, VocabProblem, Vocabulary, clauses_from_filter_set

PANEL: Final = ("KRAS", "NRAS", "TP53", "BRAF", "DIS3", "MYC")
GENE_FIELDS: Final = frozenset({"gene.symbol", "gene.gene_id"})
MUTATION_TESTED: Final = ir.Clause(field="available_variation_data", op="in", values=["ssm"])
ALL_CASES: Final = "All cases"
SCOPE_REF: Final = "scope"
CASES_PAGE_SIZE: Final = 1000
MAX_CASE_IDS: Final = 10_000


class CompiledModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )


class ResolvedScope(CompiledModel):
    source: ScopeSource
    name: str
    case_count: int | None
    clauses: list[ir.Clause]

    def view(self) -> Scope:
        return Scope(source=self.source, name=self.name, case_count=self.case_count)


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

    def view(self) -> PlanView:
        return PlanView(
            version=self.version,
            signature=self.signature,
            scope=self.scope.view(),
            rows=self.rows,
            assumptions=self.assumptions,
            writes=self.writes,
            paraphrase=self.paraphrase,
        )


class CompileStop(CompiledModel):
    code: Code
    limitations: list[Limitation]
    assumptions: list[Assumption]
    scope: ResolvedScope | None


class LedgerCohort(CompiledModel):
    ref: str
    name: str
    clauses: list[ir.Clause]


class LedgerRun(CompiledModel):
    run_id: HexId
    scope: ResolvedScope
    cohorts: list[LedgerCohort]


class SessionLedger(CompiledModel):
    """The thread's earlier runs, as far as scope precedence needs them (the full ledger lands Day 2)."""

    runs: list[LedgerRun]


class _Stop(Exception):
    def __init__(self, code: Code, limitations: list[Limitation]) -> None:
        super().__init__(code)
        self.code = code
        self.limitations = limitations


def _stop_for(problem: VocabProblem, where: str, unblock: str) -> _Stop:
    return _Stop(problem.code, [Limitation(code=problem.code, text=f"{where}{problem.text}", unblock=unblock)])


def gql_filter(clauses: list[ir.Clause]) -> dict[str, JsonValue]:
    """Gen3 filter grammar, always `and`-wrapped (an unwrapped filter can be silently ignored)."""
    leaves: list[JsonValue] = []
    for clause in clauses:
        values: list[JsonValue] = list(clause.values)
        leaves.append({clause.op: {clause.field: values if clause.op == "in" else values[0]}})
    return {"and": leaves}


def summarize(clauses: list[ir.Clause], vocab: Vocabulary) -> str:
    parts: list[str] = []
    for clause in clauses:
        spec = vocab.field(clause.field)
        label = spec.label if spec else clause.field
        shown = ", ".join(str(v) for v in clause.values[:3]) + (f" and {len(clause.values) - 3} more" if len(clause.values) > 3 else "")
        parts.append(f"{label} {'=' if clause.op == 'in' and len(clause.values) == 1 else clause.op} {shown}")
    return "; ".join(parts)


def join_words(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def signature(scope: ResolvedScope, steps: list[StepSpec], assumptions: list[Assumption], writes: bool) -> str:
    """sha256 of the compiled plan with its resolved scope; named tools, labels and assumption text are left out."""
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


def apply_untested_rule(cohorts: list[ir.CohortDef]) -> tuple[list[ir.CohortDef], list[str]]:
    """C-UNTESTED: "without mutation X" means mutation-tested minus X-mutated.

    A difference whose `b` is gene-mutated and whose `a` is not mutation-tested gets a new
    `a` = mutation_tested(within the old a). Returns the cohorts and the refs of every
    mutation-tested cohort whose excluded (untested) cases must be counted.
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


def _fresh_ref(base: str, taken: dict[str, ir.CohortDef]) -> str:
    candidate, n = base[:32], 2
    while candidate in taken or candidate == ir.BASE_REF:
        candidate, n = f"{base[:29]}_{n}", n + 1
    return candidate


@dataclass
class _Builder:
    vocab: Vocabulary
    scope: ResolvedScope
    steps: list[StepSpec] = field(default_factory=list)
    rows: list[PlanRow] = field(default_factory=list)
    cohort_steps: dict[str, str] = field(default_factory=dict)
    cohort_clauses: dict[str, list[ir.Clause]] = field(default_factory=dict)

    def add_row(self, label: str, tool: str) -> int:
        self.rows.append(PlanRow(row=len(self.rows) + 1, label=label, tool=tool, reason=TOOL_REASONS.get(tool, ""), step_ids=[]))
        if len(self.rows) == 1:
            # s1 records the data and service versions, folded under row 1, before any other call.
            self.add_step(1, "fixture_fingerprint", "server", "sandbox status, versions, ranking", {}, folded=True)
        return len(self.rows)

    def add_step(
        self, row: int, tool: str, kind: StepKind, summary: str, args: dict[str, JsonValue], depends_on: list[str] | None = None, folded: bool = False
    ) -> str:
        step_id = f"s{len(self.steps) + 1}"
        self.steps.append(
            StepSpec(id=step_id, row=row, tool=tool, kind=kind, folded=folded, input_summary=summary, args=args, depends_on=depends_on or [])
        )
        self.rows[row - 1].step_ids.append(step_id)
        return step_id

    def ref_of(self, ref: str) -> str:
        """A step id producing the cohort's case IDs, or `scope` for the base."""
        return self.cohort_steps.get(ref, SCOPE_REF)

    def within_clauses(self, ref: str) -> list[ir.Clause] | None:
        """The filter for `ref` when it is expressible as one, else None."""
        return list(self.scope.clauses) if ref == ir.BASE_REF else self.cohort_clauses.get(ref)


class Compiler:
    def __init__(self, vocab: Vocabulary, default_scope: DefaultScope) -> None:
        self.vocab = vocab
        self.default_scope = default_scope

    def compile(
        self, plan: ir.Plan, context: CohortContext, ledger: SessionLedger | None = None, version: int = 1
    ) -> CompiledPlan | CompileStop:
        assumptions: list[Assumption] = []
        limitations: list[Limitation] = []
        scope: ResolvedScope | None = None
        try:
            self._check_panel(plan)
            scope = self._resolve_scope(plan, context, ledger, assumptions, limitations)
            cohorts = self._check_cohort_vocab(plan.cohorts, context, ledger)
            cohorts, tested_refs = apply_untested_rule(cohorts)
            if tested_refs:
                assumptions.append(
                    Assumption(
                        code="EXCLUDE_UNTESTED",
                        text="Without a mutation means mutation-tested cases without it; cases without mutation data are left out and counted.",
                    )
                )
            builder = _Builder(self.vocab, scope)
            self._scope_row(builder)
            self._compile_cohorts(builder, cohorts, tested_refs)
            paraphrases = [self._compile_analysis(builder, analysis, assumptions) for analysis in plan.analyses]
        except _Stop as stop:
            return CompileStop(code=stop.code, limitations=limitations + stop.limitations, assumptions=assumptions, scope=scope)
        writes = False
        return CompiledPlan(
            version=version,
            signature=signature(scope, builder.steps, assumptions, writes),
            scope=scope,
            rows=builder.rows,
            steps=builder.steps,
            assumptions=assumptions,
            limitations=limitations,
            writes=writes,
            paraphrase=" ".join(paraphrases),
        )

    # -- C-PANEL ----------------------------------------------------------------

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
            raise _Stop(
                "OUT_OF_PANEL",
                [
                    Limitation(
                        code="OUT_OF_PANEL",
                        text=f"{join_words(sorted(set(outside)))} is outside the six-gene mutation panel ({', '.join(PANEL)}), so nothing was run.",
                        unblock=f"Ask about {join_words(list(PANEL)).replace(' and ', ' or ')}.",
                    )
                ],
            )

    def _panel_values(self, field_id: str) -> tuple[str, ...]:
        spec = self.vocab.field(field_id)
        return tuple(str(v).upper() for v in (spec.values or ())) if spec else PANEL

    # -- C-SCOPE ----------------------------------------------------------------

    def _resolve_scope(
        self,
        plan: ir.Plan,
        context: CohortContext,
        ledger: SessionLedger | None,
        assumptions: list[Assumption],
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
            resolved = self._thread_scope(plan, ledger)
            if resolved is not None:
                return resolved
            if named_thread:
                raise _Stop(
                    "UNKNOWN_VALUE",
                    [
                        Limitation(
                            code="UNKNOWN_VALUE",
                            text="The earlier cohort this question names is not in the session ledger, so nothing was run.",
                            unblock="Ask again and name the cohort.",
                        )
                    ],
                )
            scope = self._default_scope(context, assumptions)
            limitations.append(
                Limitation(
                    code="UNKNOWN_VALUE",
                    text=f"This follows an earlier answer, but that answer's scope is not in the session ledger, so the default scope, {scope.name}, is used.",
                    unblock="Ask again and name the cohort.",
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
        return ResolvedScope(source="active_cohort", name=name, case_count=active.case_count, clauses=clauses)

    def _saved_scope(self, name: str, context: CohortContext) -> ResolvedScope:
        saved = self._find_saved(name, context)
        clauses = self._browser_clauses(saved.filters, f"Saved cohort {saved.name}: ")
        return ResolvedScope(source="saved_cohort", name=saved.name, case_count=saved.case_count, clauses=clauses)

    def _find_saved(self, name: str, context: CohortContext) -> SavedCohort:
        wanted = name.strip().casefold()
        for saved in context.saved:
            if saved.name.strip().casefold() == wanted:
                return saved
        raise _Stop(
            "UNKNOWN_VALUE",
            [
                Limitation(
                    code="UNKNOWN_VALUE",
                    text=f"No saved cohort named {name or 'that'} is in this browser's cohort list, so nothing was run.",
                    unblock="Pick a saved cohort by its exact name, or ask about the active cohort.",
                )
            ],
        )

    def _thread_scope(self, plan: ir.Plan, ledger: SessionLedger | None) -> ResolvedScope | None:
        if ledger is None:
            return None
        runs = [r for r in ledger.runs if plan.follows is None or r.run_id == plan.follows]
        ref = plan.base_cohort.ledger_ref if plan.base_cohort is not None else None
        for run in reversed(runs):
            if ref is None:
                where = f"Earlier scope {run.scope.name}: "
                clauses = [self._vocab_clause(c, where) for c in run.scope.clauses]
                return run.scope.model_copy(update={"source": "thread_cohort", "clauses": clauses})
            for cohort in run.cohorts:
                if cohort.ref == ref:
                    clauses = [self._vocab_clause(c, f"Earlier cohort {cohort.name}: ") for c in cohort.clauses]
                    return ResolvedScope(source="thread_cohort", name=cohort.name, case_count=None, clauses=clauses)
        return None

    def _browser_clauses(self, filters: FilterSet, where: str) -> list[ir.Clause]:
        """A browser FilterSet is validated exactly like planner output; a problem stops the plan as a limitation."""
        clauses, problems = clauses_from_filter_set(filters)
        unblock = "Remove that filter from the cohort, or ask about all cases."
        if problems:
            raise _Stop(
                problems[0].code,
                [Limitation(code=p.code, text=f"{where}{p.text} ORION cannot reproduce this cohort, so nothing was run.", unblock=unblock) for p in problems],
            )
        return [self._vocab_clause(c, where, unblock) for c in clauses]

    # -- C-VOCAB ----------------------------------------------------------------

    def _vocab_clause(self, clause: ir.Clause, where: str, unblock: str = "Rephrase using a Virtual Lab field and value.") -> ir.Clause:
        checked = self.vocab.check_clause(clause)
        if isinstance(checked, VocabProblem):
            raise _stop_for(checked, where, unblock)
        return checked

    def _check_cohort_vocab(
        self, cohorts: list[ir.CohortDef], context: CohortContext, ledger: SessionLedger | None
    ) -> list[ir.CohortDef]:
        checked: list[ir.CohortDef] = []
        for cohort in cohorts:
            definition = cohort.definition
            if isinstance(definition, ir.FilterDef):
                clauses = [self._vocab_clause(c, "") for c in definition.clauses]
                cohort = cohort.model_copy(update={"definition": definition.model_copy(update={"clauses": clauses})})
            elif isinstance(definition, ir.SavedDef):
                saved = self._find_saved(definition.cohort_name, context)
                clauses = self._browser_clauses(saved.filters, f"Saved cohort {saved.name}: ")
                if not clauses:
                    raise _Stop(
                        "UNKNOWN_VALUE",
                        [Limitation(code="UNKNOWN_VALUE", text=f"Saved cohort {saved.name} has no filter to reuse.", unblock=None)],
                    )
                cohort = ir.CohortDef(ref=cohort.ref, definition=ir.FilterDef(kind="filter", clauses=clauses))
            elif isinstance(definition, ir.ThreadDef):
                found = next(
                    (c for run in (ledger.runs if ledger else []) for c in run.cohorts if c.ref == definition.ledger_ref), None
                )
                if found is None or not found.clauses:
                    raise _Stop(
                        "UNKNOWN_VALUE",
                        [Limitation(code="UNKNOWN_VALUE", text="An earlier cohort this question reuses is not in the session ledger.", unblock="Ask again and name the cohort.")],
                    )
                clauses = [self._vocab_clause(c, f"Earlier cohort {found.name}: ") for c in found.clauses]
                cohort = ir.CohortDef(ref=cohort.ref, definition=ir.FilterDef(kind="filter", clauses=clauses))
            checked.append(cohort)
        return checked

    def _split_field(self, analysis: ir.SurvivalCompare) -> tuple[FieldSpec, list[str]]:
        if analysis.split_term is None:
            raise _Stop(
                "NOT_IMPLEMENTED",
                [Limitation(code="NOT_IMPLEMENTED", text="A survival comparison needs a split; asking which field to split by lands Day 1 PM.", unblock="Name the field to compare, such as CGS risk category.")],
            )
        spec = self.vocab.field(analysis.split_term)
        if spec is None:
            problem = VocabProblem("UNKNOWN_FIELD", analysis.split_term, f"{analysis.split_term} is not a field in the Virtual Lab vocabulary.")
            raise _stop_for(problem, "", "Name a Virtual Lab field.")
        if spec.values is None or spec.type not in ("enum", "multiselect"):
            raise _Stop(
                "UNSUPPORTED_FIELD",
                [Limitation(code="UNSUPPORTED_FIELD", text=f"Survival can be split only by a categorical field, and {spec.label} is not one.", unblock=None)],
            )
        if analysis.split_values is None:
            return spec, [str(v) for v in spec.values]
        checked = self._vocab_clause(ir.Clause(field=spec.id, op="in", values=analysis.split_values), "")
        return spec, [str(v) for v in checked.values]

    # -- steps ------------------------------------------------------------------

    def _scope_row(self, builder: _Builder) -> None:
        """C-SCOPE: a filtered scope compiles to exactly one cases_query step; an empty filter needs none."""
        if not builder.scope.clauses:
            return
        row = builder.add_row(f"Find cases in {builder.scope.name}", "cases_query")
        builder.add_step(
            row,
            "cases_query",
            "server",
            summarize(builder.scope.clauses, self.vocab),
            {"filter": gql_filter(builder.scope.clauses), "pageSize": CASES_PAGE_SIZE, "maxIds": MAX_CASE_IDS},
        )

    def _compile_cohorts(self, builder: _Builder, cohorts: list[ir.CohortDef], tested_refs: list[str]) -> None:
        for cohort in cohorts:
            definition = cohort.definition
            if isinstance(definition, ir.FilterDef):
                row = builder.add_row(f"Find {cohort.ref} cases", "cases_query")
                clauses = builder.scope.clauses + definition.clauses
                builder.cohort_clauses[cohort.ref] = clauses
                builder.cohort_steps[cohort.ref] = builder.add_step(
                    row, "cases_query", "server", summarize(definition.clauses, self.vocab),
                    {"filter": gql_filter(clauses), "pageSize": CASES_PAGE_SIZE, "maxIds": MAX_CASE_IDS},
                )
            elif isinstance(definition, ir.MutationTestedDef):
                within = builder.within_clauses(definition.within)
                if within is None:
                    raise _Stop("NOT_IMPLEMENTED", [Limitation(code="NOT_IMPLEMENTED", text="Mutation-tested cases within a computed cohort compile from Day 1 PM.", unblock=None)])
                row = builder.add_row("Find mutation-tested cases", "cases_query")
                clauses = within + [MUTATION_TESTED]
                builder.cohort_clauses[cohort.ref] = clauses
                step = builder.add_step(
                    row, "cases_query", "server", "Mutation-tested cases (available variation data = ssm)",
                    {"filter": gql_filter(clauses), "pageSize": CASES_PAGE_SIZE, "maxIds": MAX_CASE_IDS},
                )
                builder.cohort_steps[cohort.ref] = step
                if cohort.ref in tested_refs:
                    source = builder.ref_of(definition.within)
                    builder.add_step(
                        row, "set_operation", "exec", "Cases without mutation data, left out and counted",
                        {"op": "difference", "a": source, "b": step, "output": "count"},
                        [d for d in (source, step) if d != SCOPE_REF],
                    )
            elif isinstance(definition, ir.GeneMutatedDef):
                row = builder.add_row(f"Find {definition.gene}-mutated cases", "gene_cases")
                inside = builder.ref_of(definition.within)
                builder.cohort_steps[cohort.ref] = builder.add_step(
                    row, "gene_cases", "server", f"{definition.gene}-mutated cases",
                    {"gene": definition.gene, "within": inside}, [inside] if inside != SCOPE_REF else [],
                )
            elif isinstance(definition, (ir.DifferenceDef, ir.IntersectionDef)):
                a, b = builder.ref_of(definition.a), builder.ref_of(definition.b)
                verb = "Subtract" if definition.kind == "difference" else "Intersect"
                row = builder.add_row(f"{verb} to get {cohort.ref}", "set_operation")
                builder.cohort_steps[cohort.ref] = builder.add_step(
                    row, "set_operation", "exec", f"{definition.a} {'minus' if definition.kind == 'difference' else 'and'} {definition.b}",
                    {"op": definition.kind, "a": a, "b": b}, [d for d in (a, b) if d != SCOPE_REF],
                )

    def _compile_analysis(
        self, builder: _Builder, analysis: ir.SurvivalCompare | ir.TopMutatedGenes | ir.DifferentialExpression | ir.CohortSize, assumptions: list[Assumption]
    ) -> str:
        if isinstance(analysis, ir.SurvivalCompare) and analysis.groups is None:
            return self._compile_survival_split(builder, analysis, assumptions)
        if isinstance(analysis, ir.CohortSize):
            name = builder.scope.name if analysis.cohort == ir.BASE_REF else analysis.cohort
            row = builder.add_row(f"Count {name}", "cohort_count")
            filter_clauses = builder.within_clauses(analysis.cohort)
            if filter_clauses is not None:
                summary = summarize(filter_clauses, self.vocab) or builder.scope.name
                builder.add_step(row, "cohort_count", "server", summary, {"filter": gql_filter(filter_clauses)})
            else:
                source = builder.ref_of(analysis.cohort)
                builder.add_step(row, "set_operation", "exec", f"Size of {analysis.cohort}", {"op": "size", "a": source}, [source])
            return f"Count the cases in {name}."
        stub = Limitation(
            code="NOT_IMPLEMENTED",
            text=f"Plans for {analysis.kind.replace('_', ' ')} compile from Day 1 PM.",
            unblock=None,
            nearest_apps=[NearestApp.of(ANALYSIS_APPS[analysis.kind])],
        )
        raise _Stop("NOT_IMPLEMENTED", [stub])

    def _compile_survival_split(self, builder: _Builder, analysis: ir.SurvivalCompare, assumptions: list[Assumption]) -> str:
        spec, values = self._split_field(analysis)
        code = spec.split_assumption or f"SPLIT_{spec.id.rsplit('.', 1)[-1].upper()}"
        assumptions.append(Assumption(code=code, text=f"{' vs. '.join(values)} from {spec.label}"))
        count_row = builder.add_row(
            f"Count both {spec.group} groups" if len(values) == 2 else f"Count the {len(values)} {spec.group} groups", "cohort_count"
        )
        groups: list[JsonValue] = []
        count_steps: list[str] = []
        for value in values:
            clauses = builder.scope.clauses + [ir.Clause(field=spec.id, op="in", values=[value])]
            count_steps.append(
                builder.add_step(count_row, "cohort_count", "server", f"{spec.label} = {value}", {"filter": gql_filter(clauses)})
            )
            groups.append({"label": value, "filter": gql_filter(clauses)})
        survival_row = builder.add_row(f"Compare survival: Kaplan–Meier, split by {spec.label}", "survival_compare")
        survival = builder.add_step(
            survival_row, "survival_compare", "server", f"{len(values)} groups, Kaplan–Meier", {"groups": groups}, count_steps
        )
        builder.add_step(survival_row, "derive_km_median", "exec", f"{len(values)} curves", {"curves": survival}, [survival])
        prefix = f"{spec.value_prefix} " if spec.value_prefix else ""
        return f"Compare overall survival between {prefix}{join_words(values)} cases in {builder.scope.name}."
