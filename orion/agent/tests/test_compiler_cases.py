"""Shared plans, cohort contexts and session ledgers for the compiler and stub planner tests (no tests here)."""

from pydantic import JsonValue
from support import context, vocabulary

from orion_agent import ir
from orion_agent.compiler import (
    CompiledPlan,
    Compiler,
    CompileStop,
    LedgerCohort,
    LedgerRun,
    ResolvedScope,
    SessionLedger,
)
from orion_agent.models import ActiveCohort, CaseIdsDefinition, CohortContext, FilterDefinition, def_sha256

CGS = "cgs_risk_key_criteria.cgs_risk_category"
RACE = "demographic.race"
ISS = "diagnoses.iss_stage"
J3_RUN = "3a1f0c5e9b2d4c6a8e0f1b3d5c7a9e2f"
J2_RUN = "4b2e1d6f0a3c5e7b9d1f2a4c6e8b0d3a"
RACE_ROOT: dict[str, dict[str, JsonValue]] = {RACE: {"operator": "in", "field": RACE, "operands": ["Black or African American"]}}
MUT_IDS = [f"SYNTH-CASE-{n:04d}" for n in range(1, 481)]
WT_IDS = [f"SYNTH-CASE-{n:04d}" for n in range(481, 1001)]

J1: dict[str, JsonValue] = {"kind": "plan", "analyses": [{"kind": "survival_compare", "split_term": CGS, "split_values": ["high risk", "standard risk"]}]}
J2: dict[str, JsonValue] = {
    "kind": "plan",
    "cohorts": [{"ref": "black", "def": {"kind": "filter", "clauses": [{"field": RACE, "op": "in", "values": ["black or african american"]}]}}],
    "analyses": [{"kind": "top_mutated_genes", "cohort": "black", "n": 3}],
}
J3: dict[str, JsonValue] = {
    "kind": "plan",
    "cohorts": [
        {"ref": "tested", "def": {"kind": "mutation_tested", "within": "base"}},
        {"ref": "kras_mut", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "tested"}},
        {"ref": "kras_wt", "def": {"kind": "difference", "a": "tested", "b": "kras_mut"}},
    ],
    "analyses": [{"kind": "differential_expression", "group1": "kras_wt", "group2": "kras_mut", "top_n": 10}],
}


ALL_CASES_ACTIVE = ActiveCohort.model_validate({"id": None, "name": "All cases", "filters": {"mode": "and", "root": {}}, "caseCount": 1000})


def plan(data: dict[str, JsonValue], **changes: JsonValue) -> ir.Plan:
    return ir.Plan.model_validate({**data, **changes})


def compiler() -> Compiler:
    return Compiler(vocabulary(), "active_cohort")


def compile_ir(
    data: ir.Plan | dict[str, JsonValue], ctx: CohortContext | None = None, ledger: SessionLedger | None = None,
    using: Compiler | None = None,
) -> CompiledPlan | CompileStop:
    output = data if isinstance(data, ir.Plan) else ir.Plan.model_validate(data)
    return (using or compiler()).compile(output, ctx or context(), ledger)


def compiled(result: CompiledPlan | CompileStop) -> CompiledPlan:
    assert isinstance(result, CompiledPlan), result
    return result


def stopped(result: CompiledPlan | CompileStop) -> CompileStop:
    assert isinstance(result, CompileStop), result
    return result


def case_id_filter_set(ids: list[str]) -> dict[str, JsonValue]:
    """The FilterSet the browser writes for a case-ID definition (contract "save_cohorts tool call")."""
    operands: list[JsonValue] = list(sorted(ids))
    return {"mode": "and", "root": {"case_id": {"operator": "in", "field": "case_id", "operands": operands}}}


def race_context(saved: list[dict[str, JsonValue]] | None = None) -> CohortContext:
    """The J2 race cohort active (E14, and E15 after the switch)."""
    return context(RACE_ROOT, name="Black or African American", case_count=334, saved=saved)


def saved_j3_cohorts() -> list[dict[str, JsonValue]]:
    """J3's two cohorts as the browser stores them after Create."""
    return [
        {"id": "c-wt", "name": "KRAS not detected (ORION)", "filters": case_id_filter_set(WT_IDS), "caseCount": len(WT_IDS)},
        {"id": "c-mut", "name": "KRAS mutated (ORION)", "filters": case_id_filter_set(MUT_IDS), "caseCount": len(MUT_IDS)},
    ]


def j3_ledger(saved: bool = True, with_ir: bool = True) -> SessionLedger:
    """The thread after J3 ran on all cases: its scope, its cohorts (saved or not) and its IR."""
    mut = CaseIdsDefinition(kind="case_ids", case_ids=MUT_IDS)
    wt = CaseIdsDefinition(kind="case_ids", case_ids=WT_IDS)
    ssm = FilterDefinition.model_validate({"kind": "filter", "clauses": [{"field": "available_variation_data", "op": "in", "values": ["ssm"]}]})
    cohorts = [
        LedgerCohort(ref="tested", name="Mutation-tested cases", clauses=[], definition=ssm, def_sha256=def_sha256(ssm), case_count=1000),
        LedgerCohort(
            ref="kras_mut", name="KRAS mutated (ORION)", clauses=[], definition=mut, def_sha256=def_sha256(mut), case_count=480,
            cohort_id="c-mut" if saved else None, definition_text="Mutation-tested cases with a KRAS mutation, as a case-ID list",
        ),
        LedgerCohort(
            ref="kras_wt", name="KRAS not detected (ORION)", clauses=[], definition=wt, def_sha256=def_sha256(wt), case_count=520,
            cohort_id="c-wt" if saved else None, definition_text="Mutation-tested cases without a KRAS mutation, as a case-ID list",
        ),
    ]
    run = LedgerRun(
        run_id=J3_RUN, scope=ResolvedScope(source="active_cohort", name="All cases", case_count=1000, clauses=[]), cohorts=cohorts,
        question="Differential expression between patients with and without a KRAS mutation, top 10 genes", status="Complete",
        analyses=["differential_expression"], plan_ir=plan(J3) if with_ir else None,
        active=ALL_CASES_ACTIVE,
    )
    return SessionLedger(runs=[run])


def j2_ledger() -> SessionLedger:
    """The thread after J2 ran on all cases and saved its race cohort."""
    black = FilterDefinition.model_validate({"kind": "filter", "clauses": [{"field": RACE, "op": "in", "values": ["black or african american"]}]})
    run = LedgerRun(
        run_id=J2_RUN, scope=ResolvedScope(source="active_cohort", name="All cases", case_count=1000, clauses=[]),
        cohorts=[LedgerCohort(ref="black", name="Black or African American (ORION)", clauses=[], definition=black,
                              def_sha256=def_sha256(black), cohort_id="c-black", case_count=334)],
        status="Complete", analyses=["top_mutated_genes"], plan_ir=plan(J2),
        active=ALL_CASES_ACTIVE,
    )
    return SessionLedger(runs=[run])
