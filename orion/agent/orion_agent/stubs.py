"""Stand-ins for the planner and executor in mode L without Bedrock (ORION_PLANNER=stub, ORION_EXECUTOR=stub).

The stub planner turns a question into planner output (ir.PlannerOutput) with deterministic keyword
rules, never a model call: the three reference journeys in both phrasings (E1-E6), the edge cases
the design evaluates (E7-E10, E12, E14, E15, E17, E18), the follow-ups the fixtures offer, every
follow-up chip the stub composer writes, and "Show the top 5 instead". Like the real planner it
fills only what the question states and never drops a population it names (a gene outside the
panel becomes an OTHER cohort, which the compiler stops with OUT_OF_PANEL); it writes no display
text: the compiler fills every default and every name. Both phrasings of a journey produce the same
IR apart from `named_tools`, so they reach the same plan signature.

The stub executor (Day 1) makes no data call and invents no number: it skips every step and ends
the run Limited. The engine's executor replaces it at integration.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from . import ir
from .catalog import AppId, NamedToolName
from .compiler import CompiledPlan, LedgerRun, SessionLedger
from .interfaces import Clarification, ExecutionResult, PlannerResult, StepSession
from .models import CohortContext, Limitation

CGS_RISK: Final = "cgs_risk_key_criteria.cgs_risk_category"
ISS_STAGE: Final = "diagnoses.iss_stage"
RACE: Final = "demographic.race"
GENDER: Final = "demographic.gender"
VITAL_STATUS: Final = "demographic.vital_status"
ETHNICITY: Final = "demographic.ethnicity"
# The model name the stub planner's result and the record report (PlannerResult.serving_model).
STUB_MODEL: Final = "stub"
STUB_SKIP_REASON: Final = "Not run: the step executor lands Day 1 PM"
STUB_LIMITATION: Final = Limitation(
    code="NOT_IMPLEMENTED",
    text="The plan is ready, but no step ran: the step executor lands Day 1 PM, so there are no results yet.",
    unblock=None,
)

# The reference journeys (RFA rev 25, R3): each with an open-ended and a tool-named phrasing.
J1_OPEN: Final = "Compare survival of CGS high-risk vs. CGS standard-risk patients"
J1_NAMED: Final = "Use Correlation Plot to compare Kaplan–Meier survival of CGS high-risk vs. CGS standard-risk patients"
J2_OPEN: Final = "Top 3 most frequently mutated genes in Black or African American patients"
J2_NAMED: Final = "Build a Black or African American cohort in Cohort Builder and show its top 3 mutated genes in OncoMatrix"
J3_OPEN: Final = "Differential expression between patients with and without a KRAS mutation, top 10 genes"
J3_NAMED: Final = (
    "Use the Gen3 Analysis API to get the KRAS-mutated and mutation-tested cases, then run Differential Expression "
    "between them and show the top 10 genes"
)

# Tool names whose words never mean anything else. "Differential expression" names the app only
# after a verb ("run Differential Expression"); on its own it names the analysis.
_TOOL_PHRASES: Final[tuple[tuple[str, NamedToolName], ...]] = (
    ("correlation plot", "correlation_plot"),
    ("gen3 analysis", "gen3_analysis_api"),
    ("cohort builder", "cohort_builder"),
    ("oncomatrix", "oncomatrix"),
    ("mutation frequency", "mutation_frequency"),
    ("cohort comparison", "cohort_comparison"),
    ("differential gene expression", "differential_expression"),
    ("clinical data analysis", "clinical_data_analysis"),
    ("genome browser", "genome_browser"),
    ("set operations", "set_operations"),
    ("gene expression clustering", "gene_expression_clustering"),
    ("single cell", "single_cell_rnaseq"),
    ("umap", "bulk_rnaseq_umap"),
    ("proteinpaint", "proteinpaint"),
)
_DE_TOOL: Final = re.compile(r"\b(use|using|in|with|open|through|run|via) (the )?differential expression\b")
# Questions about one app's own views, which no analysis kind covers: Unsupported, naming the app.
_APP_VIEWS: Final[tuple[tuple[str, AppId], ...]] = (
    ("venn", "SetOperations"),
    ("genome browser", "GB"),
    ("genomic region", "GB"),
    ("umap", "Umap"),
    ("single cell", "SC"),
    ("cluster", "GeneExpression"),
    ("lollipop", "ProteinPaint"),
    ("protein domain", "ProteinPaint"),
    ("clinical data", "cDave"),
    ("download", "Repository"),
)
# Clinical or treatment advice (E17): treatment words, or what a patient "should get".
_CLINICAL: Final = re.compile(
    r"\b(drug|drugs|medication|medicine|prescribe|prescription|dose|dosing|therapy|therapies|treat|treatment|treatments)\b"
    r"|\bshould\b.*\b(get|receive|take)\b"
)
_PANEL: Final[tuple[ir.PanelGene, ...]] = ("KRAS", "NRAS", "TP53", "BRAF", "DIS3", "MYC")
# Upper-case words that are not genes: field and app abbreviations the questions use.
_NOT_GENES: Final = frozenset({"CGS", "ISS", "DE", "API", "RNA", "SSM", "KM", "ORION", "MMRF", "AND", "OR", "VS", "US", "UMAP", "WT"})
_GENE_TOKEN: Final = re.compile(r"\b([A-Z][A-Z0-9]{1,9})(?=[- ]?(?:mutat|mutant|status|wild|positive|negative))")
_TOP_N: Final = re.compile(r"\btop (\d{1,3})\b")


def j1_plan(named_tools: list[NamedToolName] | None = None) -> ir.Plan:
    """J1: survival split by CGS risk category, with no base cohort (the compiler applies the default scope)."""
    return ir.Plan(
        kind="plan",
        analyses=[
            ir.SurvivalCompare(kind="survival_compare", split_term=CGS_RISK, split_values=["high risk", "standard risk"])
        ],
        named_tools=[ir.NamedTool(tool=tool) for tool in named_tools or []],
    )


def _norm(text: str) -> str:
    """Lower case, every run of non-alphanumerics one space, padded: " compare survival of cgs high risk ... "."""
    return f" {' '.join(re.sub(r'[^a-z0-9]+', ' ', text.casefold()).split())} "


def _has(text: str, *phrases: str) -> bool:
    return any(f" {phrase} " in text for phrase in phrases)


@dataclass(frozen=True)
class _Population:
    """A filter the question states as its population: one clause, and the ref the cohort takes."""

    ref: str
    clause: ir.Clause
    at: int  # where the question names it, to keep the question's order


def _populations(text: str) -> list[_Population]:
    """Filter cohorts the question names, in the question's order (C-VOCAB normalizes the values)."""
    found: list[_Population] = []

    def add(ref: str, field: str, value: str, *phrases: str) -> None:
        positions = [text.find(f" {p} ") for p in phrases if f" {p} " in text]
        if positions and all(p.ref != ref for p in found):
            found.append(_Population(ref, ir.Clause(field=field, op="in", values=[value]), min(positions)))

    if _has(text, "cgs", "risk category", "risk categories"):
        add("high", CGS_RISK, "High risk", "high risk")
        add("standard", CGS_RISK, "Standard risk", "standard risk")
    add("black", RACE, "black or african american", "black or african american", "black")
    add("white", RACE, "white", "white")
    add("asian", RACE, "asian", "asian")
    add("female", GENDER, "female", "female", "women", "woman")
    add("male", GENDER, "male", "male", "men", "man")
    # One ISS stage at most: two stages of one field would be an empty cohort. Padding keeps
    # " stage i " from matching inside " stage iii ".
    for value, ref, phrases in (("Stage III", "stage_iii", ("stage iii", "stage 3")), ("Stage II", "stage_ii", ("stage ii", "stage 2")),
                                ("Stage I", "stage_i", ("stage i", "stage 1"))):
        if not any(p.ref.startswith("stage_") for p in found):
            add(ref, ISS_STAGE, value, *phrases)
    return sorted(found, key=lambda p: p.at)


def _genes(question: str) -> list[ir.PanelGene]:
    """Genes the question names: panel symbols, then any other gene-like symbol as OTHER (C-PANEL stops it)."""
    found: list[tuple[int, ir.PanelGene]] = []
    for gene in _PANEL:
        match = re.search(rf"\b{gene}\b", question, flags=re.IGNORECASE)
        if match:
            found.append((match.start(), gene))
    for match in _GENE_TOKEN.finditer(question):
        token = match.group(1)
        if token not in _PANEL and token not in _NOT_GENES:
            found.append((match.start(), "OTHER"))
    return [gene for _, gene in sorted(found)]


def _named_tools(question: str) -> list[ir.NamedTool]:
    """The catalog tools the question names, in the order it names them, each once."""
    text = _norm(question)
    first: dict[NamedToolName, int] = {}
    for phrase, tool in _TOOL_PHRASES:
        if f" {phrase} " in text:
            first.setdefault(tool, text.find(f" {phrase} "))
    de = _DE_TOOL.search(text)
    if de is not None:
        first.setdefault("differential_expression", de.start())
    return [ir.NamedTool(tool=tool) for tool in sorted(first, key=lambda tool: first[tool])]


def _top_n(text: str) -> int | None:
    match = _TOP_N.search(text)
    return int(match.group(1)) if match else None


def _scope(question: str, context: CohortContext) -> ir.BaseCohort | None:
    """The scope the question states: a saved cohort it names (its name is data, E12), all cases, or
    the active cohort; None leaves the default to the compiler."""
    text = _norm(question)
    for saved in sorted(context.saved, key=lambda s: -len(s.name)):
        if _norm(saved.name).strip() and _norm(saved.name) in text:
            return ir.BaseCohort(source="saved_cohort", cohort_name=saved.name)
    if _has(text, "all cases", "all patients", "every patient", "all the patients", "the whole sandbox"):
        return ir.BaseCohort(source="all_cases")
    if _has(text, "my cohort", "the active cohort", "my active cohort", "these patients", "this cohort"):
        return ir.BaseCohort(source="active_cohort")
    return None


class StubPlanner:
    """interfaces.Planner by keyword rules. Its result names the model "stub" (PlannerResult.serving_model)."""

    async def plan(
        self,
        question: str,
        cohort_context: CohortContext,
        ledger: SessionLedger | None,
        *,
        clarification: Clarification | None = None,
    ) -> PlannerResult:
        return PlannerResult(
            output=plan_question(question, cohort_context, ledger, clarification),
            model_calls=[],
            serving_model=STUB_MODEL,
            declined=False,
            decline_reason=None,
            repaired=False,
        )


def plan_question(
    question: str, context: CohortContext, ledger: SessionLedger | None = None, clarification: Clarification | None = None
) -> ir.PlannerOutput:
    """The stub planner's rules, in order: clinical advice, follow-ups on the thread, the four
    analysis kinds (survival, DE, top mutated genes, cohort size), then an app's own views; anything
    else is Unsupported with no app."""
    text = _norm(question)
    if _CLINICAL.search(text):
        # E17: no analysis kind gives clinical advice, and no app is named (G-5).
        return ir.Unsupported(kind="unsupported", reason="CLINICAL_ADVICE", nearest_app_ids=[])
    follow_up = _follow_up(question, text, ledger)
    if follow_up is not None:
        return follow_up
    survival = _has(text, "survival", "kaplan meier", "kaplan")
    de = _has(text, "differential expression", "differentially expressed", "expression between")
    # "Top mutated genes", "the most frequently mutated genes", and the chip "Which genes are most frequently mutated".
    ranked = _has(text, "top", "most frequently", "most often", "most commonly")
    genes = ranked and (" mutated gene" in text or " oncomatrix " in text or (_has(text, "genes") and _has(text, "mutated")))
    size = _has(text, "how many", "count", "number of", "size of")
    named = _named_tools(question)
    scope = _scope(question, context)
    if not (survival or de or genes or size):
        for phrase, app in _APP_VIEWS:
            if phrase in text:
                # E18 and its kind: no analysis kind draws this view; the card names the nearest app.
                return ir.Unsupported(kind="unsupported", reason="UNSUPPORTED_ANALYSIS", nearest_app_ids=[app])
        risk = _risk_population(text, named, scope, clarification)
        if risk is not None:
            return risk
        return ir.Unsupported(kind="unsupported", reason="UNSUPPORTED_ANALYSIS", nearest_app_ids=[])
    # Genes named outside a saved cohort's name ("my KRAS mutated (ORION) cohort" names a cohort, not a gene).
    stated = _genes(_without_saved_names(question, context))
    if survival:
        return _survival(text, named, scope, clarification, stated)
    if de:
        return _differential_expression(question, text, named, scope, clarification)
    if genes:
        return _top_genes(text, named, scope, stated)
    return _cohort_size(question, text, named, scope)


def _without_saved_names(question: str, context: CohortContext) -> str:
    out = question
    for saved in sorted(context.saved, key=lambda s: -len(s.name)):
        if saved.name.strip():
            out = re.sub(re.escape(saved.name), " ", out, flags=re.IGNORECASE)
    return out


# FR-04, E10: a risk population with no field that defines it ("High-risk patients"). CGS risk category
# and ISS stage both define risk; the clarified field sizes that cohort (ISS stage III is its high-risk stage).
_RISK_VALUES: Final[dict[str, dict[str, str]]] = {
    CGS_RISK: {"high risk": "High risk", "standard risk": "Standard risk", "low risk": "Standard risk"},
    ISS_STAGE: {"high risk": "Stage III", "standard risk": "Stage I", "low risk": "Stage I"},
}


def _risk_population(
    text: str, named: list[ir.NamedTool], scope: ir.BaseCohort | None, clarification: Clarification | None
) -> ir.PlannerOutput | None:
    """"High-risk patients" asks once which field defines the risk (CGS risk category, ISS stage), then sizes
    that cohort; None when the question names no risk population, or names the field itself."""
    level = next((phrase for phrase in ("high risk", "standard risk", "low risk") if _has(text, phrase)), None)
    if level is None or _has(text, "cgs", "risk category", "iss", "stage"):
        return None
    field = clarification.field if clarification is not None else None
    if field not in _RISK_VALUES:
        return ir.Clarify(kind="clarify", slot="cohort_field", option_ids=[CGS_RISK, ISS_STAGE])
    assert field is not None
    clause = ir.Clause(field=field, op="in", values=[_RISK_VALUES[field][level]])
    cohort = ir.CohortDef(ref=level.split()[0], definition=ir.FilterDef(kind="filter", clauses=[clause]))
    return ir.Plan(
        kind="plan", base_cohort=scope, cohorts=[cohort], analyses=[ir.CohortSize(kind="cohort_size", cohort=cohort.ref)],
        named_tools=named,
    )


def _survival(
    text: str, named: list[ir.NamedTool], scope: ir.BaseCohort | None, clarification: Clarification | None,
    genes: Sequence[ir.PanelGene] = (),
) -> ir.PlannerOutput:
    """J1 (E1, E2, E7, E10, E14): survival split by a field the question names; else, for a gene it names
    ("Survival by KRAS mutation", J2's chip), the gene-mutated against the other mutation-tested cases; else
    one clarifying question."""
    field: str | None = None
    values: list[ir.Value] | None = None
    if _has(text, "cgs", "risk category", "risk categories"):
        field = CGS_RISK
        if _has(text, "high risk") and _has(text, "standard risk"):
            values = ["high risk", "standard risk"]
    elif _has(text, "iss", "iss stage"):
        field = ISS_STAGE
    elif _has(text, "race"):
        field = RACE
    elif _has(text, "gender", "sex"):
        field = GENDER
    elif _has(text, "vital status"):
        field = VITAL_STATUS
    elif _has(text, "ethnicity"):
        field = ETHNICITY
    elif genes:
        # The gene's mutated cases against the other mutation-tested ones (C-UNTESTED), the question's order;
        # a gene outside the panel is OTHER, which the compiler stops (C-PANEL OUT_OF_PANEL) before any data call.
        mutated, gene_cohorts = _gene_groups(genes[0], ir.BASE_REF)
        return ir.Plan(
            kind="plan", base_cohort=scope, cohorts=gene_cohorts,
            analyses=[ir.SurvivalCompare(kind="survival_compare", groups=[mutated, f"{genes[0].lower()}_wt"])],
            named_tools=named,
        )
    elif clarification is not None and clarification.field is not None:
        field = clarification.field
    if field is None:
        # E10: "high-risk" with no classification named: ask once, with the fields that define risk.
        options = [CGS_RISK, ISS_STAGE] if _has(text, "high risk", "low risk", "risk") else [CGS_RISK, ISS_STAGE, RACE, GENDER]
        return ir.Clarify(kind="clarify", slot="split_term", option_ids=options)
    return ir.Plan(
        kind="plan", base_cohort=scope,
        analyses=[ir.SurvivalCompare(kind="survival_compare", split_term=field, split_values=values)],
        named_tools=named,
    )


def _gene_groups(gene: ir.PanelGene, within: str) -> tuple[str, list[ir.CohortDef]]:
    """The mutation-tested cases, the gene-mutated ones among them and the rest (C-UNTESTED). Returns the mutated ref."""
    key = gene.lower()
    return f"{key}_mut", [
        ir.CohortDef(ref="tested", definition=ir.MutationTestedDef(kind="mutation_tested", within=within)),
        ir.CohortDef(ref=f"{key}_mut", definition=ir.GeneMutatedDef(kind="gene_mutated", gene=gene, within="tested")),
        ir.CohortDef(ref=f"{key}_wt", definition=ir.DifferenceDef(kind="difference", a="tested", b=f"{key}_mut")),
    ]


def _top_genes(text: str, named: list[ir.NamedTool], scope: ir.BaseCohort | None, genes: Sequence[ir.PanelGene] = ()) -> ir.Plan:
    """J2 (E3, E4) and the follow-ups: top mutated genes in a stated population, or in the scope. A gene the
    question names ("in IDH1-mutated patients") is a gene-mutated cohort within it, never dropped: OTHER stops
    at compile time (C-PANEL OUT_OF_PANEL), an in-panel gene ranks within its mutated cases."""
    populations = _populations(text)
    cohorts = _population_cohorts(populations)
    if genes:
        within = cohorts[-1].ref if cohorts else ir.BASE_REF
        gene = genes[0]
        cohorts.append(ir.CohortDef(ref=f"{gene.lower()}_mut", definition=ir.GeneMutatedDef(kind="gene_mutated", gene=gene, within=within)))
    cohort = cohorts[-1].ref if cohorts else ir.BASE_REF
    return ir.Plan(
        kind="plan", base_cohort=scope, cohorts=cohorts,
        analyses=[ir.TopMutatedGenes(kind="top_mutated_genes", cohort=cohort, n=_top_n(text))], named_tools=named,
    )


def _population_cohorts(populations: Sequence[_Population]) -> list[ir.CohortDef]:
    """One filter cohort for every clause the question states (its ref joins theirs: "black", "high")."""
    if not populations:
        return []
    ref = "_".join(p.ref for p in populations)[:32]
    return [ir.CohortDef(ref=ref, definition=ir.FilterDef(kind="filter", clauses=[p.clause for p in populations]))]


def _differential_expression(
    question: str, text: str, named: list[ir.NamedTool], scope: ir.BaseCohort | None, clarification: Clarification | None
) -> ir.PlannerOutput:
    """J3 (E5, E6, E8, E9): DE between cases with and without a gene's mutation (in a stated
    population), or between two populations in the question's order (the first is the control)."""
    genes = _genes(question)
    if not genes and clarification is not None and clarification.slot == "gene":
        genes = [g for g in _PANEL if g == clarification.option_id.upper()]
    stated_method: ir.DeMethodName | None = "edgeR" if _has(text, "edger") else "limma" if _has(text, "limma") else "wilcoxon" if _has(text, "wilcoxon") else None
    rank_by: ir.RankByName | None = "fold_change" if _has(text, "fold change") else "p_value" if _has(text, "p value", "p values", "original p") and not _has(text, "adjusted p") else None
    populations = _populations(text)
    cohorts: list[ir.CohortDef] = []
    if genes:
        gene = genes[0]
        within = ir.BASE_REF
        if populations:
            cohorts += _population_cohorts(populations)
            within = cohorts[-1].ref
        key = gene.lower()
        cohorts += [
            ir.CohortDef(ref="tested", definition=ir.MutationTestedDef(kind="mutation_tested", within=within)),
            ir.CohortDef(ref=f"{key}_mut", definition=ir.GeneMutatedDef(kind="gene_mutated", gene=gene, within="tested")),
            ir.CohortDef(ref=f"{key}_wt", definition=ir.DifferenceDef(kind="difference", a="tested", b=f"{key}_mut")),
        ]
        groups = (f"{key}_wt", f"{key}_mut")
    elif len(populations) >= 2:
        cohorts = [ir.CohortDef(ref=p.ref, definition=ir.FilterDef(kind="filter", clauses=[p.clause])) for p in populations[:2]]
        groups = (cohorts[0].ref, cohorts[1].ref)
    else:
        return ir.Clarify(kind="clarify", slot="gene", option_ids=list(_PANEL[:5]))
    analysis = ir.DifferentialExpression(
        kind="differential_expression", group1=groups[0], group2=groups[1], method=stated_method, rank_by=rank_by, top_n=_top_n(text)
    )
    return ir.Plan(kind="plan", base_cohort=scope, cohorts=cohorts, analyses=[analysis], named_tools=named)


def _cohort_size(question: str, text: str, named: list[ir.NamedTool], scope: ir.BaseCohort | None) -> ir.Plan:
    """Cohort size: a stated population, the cases with a gene's mutation, or the scope."""
    genes = _genes(question)
    if genes:
        gene = genes[0]
        ref = f"{gene.lower()}_mut"
        cohorts = [ir.CohortDef(ref=ref, definition=ir.GeneMutatedDef(kind="gene_mutated", gene=gene, within=ir.BASE_REF))]
    else:
        cohorts = _population_cohorts(_populations(text))
        ref = cohorts[-1].ref if cohorts else ir.BASE_REF
    return ir.Plan(kind="plan", base_cohort=scope, cohorts=cohorts, analyses=[ir.CohortSize(kind="cohort_size", cohort=ref)], named_tools=named)


# --- Follow-ups on the thread (FR-19) --------------------------------------------------------------------


def _follow_up(question: str, text: str, ledger: SessionLedger | None) -> ir.PlannerOutput | None:
    """An explicit follow-up sets `follows` (A2): "Show the top 5 instead" reuses the earlier run's
    analysis and its thread cohorts (E15); a question naming two thread cohorts compares them."""
    if ledger is None or not ledger.runs:
        return None
    n = _top_n(text)
    if _has(text, "instead") and n is not None:
        return _instead(ledger, n)
    if _has(text, "survival", "kaplan meier"):
        return _survival_between(question, ledger)
    return None


def _instead(ledger: SessionLedger, n: int) -> ir.PlannerOutput | None:
    for run in reversed(ledger.runs):
        earlier = run.plan_ir
        if earlier is None:
            continue
        for analysis in earlier.analyses:
            if isinstance(analysis, ir.DifferentialExpression):
                updated: ir.Analysis = analysis.model_copy(update={"top_n": min(n, 100)})
                needed = [analysis.group1, analysis.group2]
            elif isinstance(analysis, ir.TopMutatedGenes):
                updated = analysis.model_copy(update={"n": min(n, 50)})
                needed = [analysis.cohort]
            else:
                continue
            return ir.Plan(kind="plan", follows=run.run_id, cohorts=_reused_cohorts(run, earlier, needed), analyses=[updated])
    return None


def _reused_cohorts(run: LedgerRun, earlier: ir.Plan, needed: Sequence[str]) -> list[ir.CohortDef]:
    """The thread cohorts a follow-up reuses: each from the ledger when the run recorded it, else
    the earlier IR's definitions again (they rebuild within the thread scope)."""
    wanted = [ref for ref in needed if ref != ir.BASE_REF]
    in_ledger = {c.ref for c in run.cohorts if c.definition is not None or c.clauses}
    if all(ref in in_ledger for ref in wanted):
        return [ir.CohortDef(ref=ref, definition=ir.ThreadDef(kind="thread", ledger_ref=ref)) for ref in wanted]
    return list(earlier.cohorts)


def _survival_between(question: str, ledger: SessionLedger) -> ir.Plan | None:
    """Survival between thread cohorts the question names, in its order: J3's follow-up chip, "Compare
    survival between the KRAS mutated (ORION) and KRAS not detected (ORION) cohorts"."""
    text = _norm(question)
    for run in reversed(ledger.runs):
        named: list[tuple[int, str]] = []
        for cohort in run.cohorts:
            names = {_norm(cohort.name), _norm(cohort.name.removesuffix(" (ORION)"))}
            at = min((text.find(n) for n in names if n.strip() and n in text), default=-1)
            if at >= 0 and (cohort.definition is not None or cohort.clauses):
                named.append((at, cohort.ref))
        refs = [ref for _, ref in sorted(named)]
        if len(refs) >= 2:
            refs = refs[:6]
            cohorts = [ir.CohortDef(ref=ref, definition=ir.ThreadDef(kind="thread", ledger_ref=ref)) for ref in refs]
            return ir.Plan(
                kind="plan", follows=run.run_id, cohorts=cohorts,
                analyses=[ir.SurvivalCompare(kind="survival_compare", groups=refs)],
            )
    return None


class StubExecutor:
    async def execute(self, plan: CompiledPlan, session: StepSession) -> ExecutionResult:
        for step in plan.steps:
            if session.stop_requested:
                break
            await session.skip_step(step.id, "NOT_IMPLEMENTED", STUB_SKIP_REASON)
        return ExecutionResult(status="Limited", code="NOT_IMPLEMENTED", limitations=[STUB_LIMITATION])
