"""Day 1 AM stand-ins for the planner and executor (ORION_PLANNER=stub, ORION_EXECUTOR=stub).

The stub planner knows one journey, J1, and one Unsupported question, a Venn diagram. The stub
executor makes no data call and invents no number: it skips every step and ends the run Limited.
"""

from typing import Final

from . import ir
from .catalog import NamedToolName
from .compiler import CompiledPlan, SessionLedger
from .controller import ExecutionResult, StepSession
from .models import CohortContext, Limitation

CGS_RISK: Final = "cgs_risk_key_criteria.cgs_risk_category"
STUB_SKIP_REASON: Final = "Not run: the step executor lands Day 1 PM"
STUB_LIMITATION: Final = Limitation(
    code="NOT_IMPLEMENTED",
    text="The plan is ready, but no step ran: the step executor lands Day 1 PM, so there are no results yet.",
    unblock=None,
)
_TOOL_PHRASES: Final[dict[str, NamedToolName]] = {
    "correlation plot": "correlation_plot",
    "gen3 analysis": "gen3_analysis_api",
    "cohort builder": "cohort_builder",
    "oncomatrix": "oncomatrix",
    "differential expression": "differential_expression",
    "mutation frequency": "mutation_frequency",
    "cohort comparison": "cohort_comparison",
}


def j1_plan(named_tools: list[NamedToolName] | None = None) -> ir.Plan:
    """J1: survival split by CGS risk category, with no base cohort (the compiler applies the default scope)."""
    return ir.Plan(
        kind="plan",
        analyses=[
            ir.SurvivalCompare(kind="survival_compare", split_term=CGS_RISK, split_values=["high risk", "standard risk"])
        ],
        named_tools=[ir.NamedTool(tool=tool) for tool in named_tools or []],
    )


class StubPlanner:
    async def plan(self, question: str, context: CohortContext, ledger: SessionLedger | None) -> ir.PlannerOutput:
        text = question.casefold()
        if "venn" in text:
            # Design eval E18: no analysis kind draws a Venn diagram; Set Operations is the nearest app.
            return ir.Unsupported(kind="unsupported", reason="UNSUPPORTED_ANALYSIS", nearest_app_ids=["SetOperations"])
        if "surviv" not in text:
            return ir.Unsupported(kind="unsupported", reason="UNSUPPORTED_ANALYSIS", nearest_app_ids=[])
        return j1_plan([tool for phrase, tool in _TOOL_PHRASES.items() if phrase in text])


class StubExecutor:
    async def execute(self, plan: CompiledPlan, session: StepSession) -> ExecutionResult:
        for step in plan.steps:
            if session.stop_requested:
                break
            await session.skip_step(step.id, "NOT_IMPLEMENTED", STUB_SKIP_REASON)
        return ExecutionResult(status="Limited", code="NOT_IMPLEMENTED", limitations=[STUB_LIMITATION])
