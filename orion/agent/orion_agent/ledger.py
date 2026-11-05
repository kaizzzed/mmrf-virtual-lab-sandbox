"""The session ledger (RFA rev 25, A2 "Data model": SessionLedger; C-08, FR-19).

The thread's earlier runs as the planner and compiler see them, built from the run records in the
thread index's order, never from model summaries (A2 ownership table). It reads through the run
store, so it is file-backed under ORION_RUN_DIR in mode L and reads S3 in modes A and H with the
same code (`run_store.S3RunStore`).
"""

import logging
from collections.abc import Sequence
from typing import Final

from . import ir
from .compiler import LedgerCohort, LedgerRun, SessionLedger
from .models import CaseIdsDefinition, CohortRef, FilterDefinition
from .run_store import RecordStore, RunRecord

log = logging.getLogger("orion.ledger")

# The ledger lists at most this many earlier runs (the newest), so the planner prompt stays bounded.
MAX_LEDGER_RUNS: Final = 12
# Fact labels per run the planner sees.
MAX_FACT_LABELS: Final = 40


def _clauses(cohort: CohortRef) -> list[ir.Clause]:
    definition = cohort.definition
    if isinstance(definition, CaseIdsDefinition):
        return [ir.Clause(field="case_id", op="in", values=list(definition.case_ids))] if definition.case_ids else []
    if isinstance(definition, FilterDefinition):
        return [ir.Clause(field=c.field, op=c.op, values=list(c.values)) for c in definition.clauses]
    return []


def plan_ir(record: RunRecord) -> ir.Plan | None:
    """The IR the run's current plan version compiled: its planHistory entry, else the planner's Plan."""
    version = record.plan_versions[-1].version if record.plan_versions else None
    entry = next((h for h in reversed(record.plan_history) if h.version == version and h.ir is not None), None)
    if entry is not None:
        return entry.ir
    return record.planner_output if isinstance(record.planner_output, ir.Plan) else None


def ledger_run(record: RunRecord) -> LedgerRun | None:
    """One earlier run as the ledger lists it, or None when it never had a plan.

    It carries what the compiler rebuilds a follow-up from (compiler integration notes): each run
    cohort's canonical definition (a J3 case-ID cohort cannot be rebuilt from clauses alone), the
    IR of the current plan version ("Show the top 5 instead" reuses its analysis), the active
    cohort the run was asked on (E15: a follow-up names both scopes only when it changed since),
    and the resolved scope as compiled (its cohortId and ledgerRef kept).
    """
    if not record.plan_versions:
        return None
    plan = record.plan_versions[-1]
    planned = [c for c in record.cohorts if c.role == "planned"]
    current = plan_ir(record)
    analyses = [a.kind for a in current.analyses] if current is not None else []
    return LedgerRun(
        run_id=record.run_id,
        scope=plan.scope.model_copy(deep=True),
        cohorts=[
            LedgerCohort(
                ref=c.ref,
                name=c.saved_name or c.name,
                clauses=_clauses(c),
                cohort_id=c.cohort_id,
                def_sha256=c.def_sha256,
                case_count=c.case_count,
                definition=c.definition,
                definition_text=c.definition_text,
            )
            for c in planned
        ],
        question=record.question,
        signature=plan.signature,
        status=record.status,
        analyses=analyses,
        fact_labels=[f.label for f in record.facts if f.label][:MAX_FACT_LABELS],
        plan_ir=current,
        active=record.request.cohort_context.active,
    )


class StoreLedger:
    """interfaces.SessionLedgerStore over a run store (file in mode L, S3 in modes A and H)."""

    def __init__(self, store: RecordStore, *, max_runs: int = MAX_LEDGER_RUNS) -> None:
        self.store = store
        self.max_runs = max_runs

    async def ledger(self, thread_id: str, *, before_run_id: str | None = None) -> SessionLedger:
        index = await self.store.load_thread(thread_id)
        run_ids: Sequence[str] = index.run_ids
        if before_run_id is not None and before_run_id in run_ids:
            run_ids = run_ids[: run_ids.index(before_run_id)]
        runs: list[LedgerRun] = []
        for run_id in list(run_ids)[-self.max_runs :]:
            record = await self.store.load_run(run_id)
            if record is None or record.thread_id != thread_id:
                log.warning("ledger of thread %s skips run %s: no record in this thread", thread_id, run_id)
                continue
            entry = ledger_run(record)
            if entry is not None:
                runs.append(entry)
        return SessionLedger(runs=runs)
