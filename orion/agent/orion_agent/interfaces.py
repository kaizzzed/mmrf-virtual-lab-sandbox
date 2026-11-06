"""Internal interfaces between the run controller, the engine and the model roles (RFA rev 25, A2
"Agent topology" and the component table). Types and Protocols only: builders implement them.

    question, cohort context, session ledger
      -> Planner (Claude, effort low)        -> PlannerResult (IR, or declined)
      -> Compiler (code)                     -> CompiledPlan | CompileStop        [compiler.py]
      -> Executor (code) per step:
           ToolClient / ExecutorFunction / ClientAction                            [tools/base.py]
           -> Extractor (code)               -> FactRecord[]
      -> ObservedRenderer (code)             -> ObservedResult (Observed, Limitations, viz specs)
      -> Composer (Claude, effort medium)    -> ComposerResult (placeholder statements)
      -> Verifier (code)                     -> VerifierResult (resolved Sentences, per-rule findings)

Model text names numbers and gene symbols only through fact placeholders, `{{<fact id>}}`, such
as `{{f.s10.top.1.gene}}`; the verifier resolves each into a Segment carrying that factId, with
its text written by `models.format_fact`, as Observed and TEXT_MESSAGE write it.

Model call records: Planner.plan and Composer.compose take no run id. They record their
ModelCallRecords in `orion_agent.model.calls.model_call_scope(run_id, existing=record.model_calls)`,
which the controller enters around each call; the scope numbers the callIds
(`<runId>.planner.c<n>`, `<runId>.v<planVersion>.composer.c<n>`) and its `records` are
authoritative, a cancelled call included.

Cancellation (contract "cancel"): Planner.plan, Composer.compose and ToolClient.call honour
asyncio cancellation. On Stop the controller cancels a planner or composer call itself (recorded
as a ModelCallRecord with outcome `cancelled`); during Executing it sets the session's stop, and
the executor must abort its in-flight call at once (StepSession.wait_stop). If execute has not
returned STOP_GRACE_S after the stop, the controller cancels it.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from . import ir
from .catalog import AnalysisKind, AppId
from .compiler import CompiledPlan, SessionLedger, StepSpec
from .models import (
    CallRecord,
    ClarifySlot,
    CohortContext,
    CohortRef,
    Code,
    ConfirmAction,
    ConfirmPending,
    FactId,
    FactRecord,
    FollowUp,
    Limitation,
    ModelCallRecord,
    NearestApp,
    OptionId,
    RunStatus,
    Sentence,
    StepOrigin,
    StepStatus,
    StepView,
    VerifierRule,
    VizSpec,
)
from .tools.base import ToolCallOutcome
from .vocab import Vocabulary

PLACEHOLDER: Final = re.compile(r"\{\{(f\.s[1-9][0-9]*(?:\.[A-Za-z0-9_-]+)+)\}\}")
# The default models of the default Bedrock client (bedrock-runtime takes inference-profile ids;
# orion_agent.model.config.DEFAULT_MODELS["runtime"], which tests/test_contract.py keeps equal).
# The served model of each call is in its ModelCallRecord; these name a model only where no call
# served one (a planner call cancelled before any attempt finished).
PLANNER_MODEL: Final = "us.anthropic.claude-opus-5-5"
REFUSAL_FALLBACK_MODEL: Final = "us.anthropic.claude-opus-5"


class RoleModel(BaseModel):
    """Structured-output and result models of the model roles: snake_case, like the IR."""

    model_config = ConfigDict(extra="forbid")


# --- Planner -------------------------------------------------------------------------------


class Clarification(RoleModel):
    """The researcher's answer to the run's one clarifying question; the planner must not ask again."""

    slot: ClarifySlot
    option_id: OptionId
    field: str | None


class PlannerResult(RoleModel):
    # None when the planner declined with both models (the run ends Failed, PLANNER_DECLINED).
    output: ir.PlannerOutput | None
    # Every model call this plan took: the first, the refusal fallback, one IR repair.
    model_calls: list[ModelCallRecord]
    serving_model: str | None  # the model whose output was used
    declined: bool
    decline_reason: str | None
    repaired: bool  # the one repair call after the IR failed validation was used


class Planner(Protocol):
    async def plan(
        self,
        question: str,
        cohort_context: CohortContext,
        ledger: SessionLedger | None,
        *,
        clarification: Clarification | None = None,
    ) -> PlannerResult: ...


# --- Executor (engine-internal; the controller implements StepSession) ----------------------


@dataclass(frozen=True)
class ExecutionResult:
    """How execution ended. Facts, calls and cohorts reach the record step by step, through the session.

    `status` is `Answering` when every step finished and the run is ready to answer (the controller
    renders Observed and calls the composer); `AwaitingConfirmation` with `pending` at a write step;
    otherwise the end status (`Partial`, `Limited`, `Cancelled`, `Failed`) with its code.
    """

    status: RunStatus
    code: Code | None
    limitations: list[Limitation]
    # Set when execution stopped at a client write: the run pauses AwaitingConfirmation with it.
    pending: ConfirmPending | None = None


@dataclass(frozen=True)
class ResumeState:
    """What a resumed run's executor starts from (confirm, retry_step, edit_plan, consent).

    The record's current steps, facts, calls and cohorts; the accepted confirm (its read-back) or
    None; the step a retry_step reruns; and, after an edit, each step of the new version whose
    input hash is unchanged, with the version and step whose calls produced it (Reused).
    """

    steps: Sequence[StepView]
    facts: Sequence[FactRecord]
    calls: Sequence[CallRecord]
    cohorts: Sequence[CohortRef]
    confirmation: ConfirmAction | None = None
    pending: ConfirmPending | None = None
    retry_step_id: str | None = None
    reused: Mapping[str, StepOrigin] = field(default_factory=dict)


class StepSession(Protocol):
    """The controller's side of execution. Every call is one transition: a snapshot follows it.

    The engine (`executor.Engine`) also reads the run's context, `executor.EngineSession.context`
    (run id, cohort context, the run's cohorts and each cohort's defining step); the controller's
    session implements that runtime-checkable extension.
    """

    @property
    def stop_requested(self) -> bool: ...

    @property
    def resume(self) -> ResumeState | None:
        """None for an ask; the resume state for any other action that runs steps."""
        ...

    async def wait_stop(self) -> None:
        """Returns when Stop (or an edit that changes the running step) asks for the in-flight call to end."""
        ...

    async def start_step(self, step_id: str) -> None: ...

    async def retrying(self, step_id: str, *, attempt: int, backoff_ms: int) -> None:
        """The step's first attempt failed retryably: it shows Retrying until attempt `attempt` starts."""
        ...

    async def finish_step(
        self,
        step_id: str,
        status: StepStatus,
        output_summary: str | None,
        duration_ms: int | None,
        code: Code | None,
        *,
        facts: Sequence[FactRecord] = (),
        calls: Sequence[CallRecord] = (),
        cohorts: Sequence[CohortRef] = (),
        limitations: Sequence[Limitation] = (),
    ) -> None:
        """Done, Empty or Failed, with every attempt's CallRecord, the step's facts and the cohorts it
        defined (defSha256 and caseCount); all appear in the first snapshot after it finishes."""
        ...

    async def skip_step(
        self, step_id: str, code: Code | None, reason: str, *, calls: Sequence[CallRecord] = (), duration_ms: int | None = None
    ) -> None:
        """Skipped with `reason` ("Not run: stopped"), with the CallRecord of an aborted attempt, if any.

        `duration_ms` is how long an aborted running step ran (the j1-stop fixture's 4840); null for
        a step that never started."""
        ...

    async def reuse_step(self, step_id: str, *, origin: StepOrigin) -> None:
        """Reused: the controller carries the origin step's facts over, renamed to this step id and row."""
        ...


class Executor(Protocol):
    """Runs the plan's steps in order. On a resume, `session.resume` says where to start."""

    async def execute(self, plan: CompiledPlan, session: StepSession) -> ExecutionResult: ...


# --- Extractor and Observed renderer ----------------------------------------------------------


class Extractor(Protocol):
    """Turns one call's output into facts, each with its step, JSON pointer into the call and hashes.

    Pure functions and client writes arrive as ToolCallOutcomes too (transport in_process or client).
    Fact ids follow `f.<step id>.<path>`, such as f.s2.count or f.s10.top.1.gene.
    """

    def extract(self, step: StepSpec, outcome: ToolCallOutcome, *, plan_version: int) -> list[FactRecord]: ...


class ObservedResult(RoleModel):
    template_version: str  # such as "observed@1"; recorded with the answer
    observed: list[Sentence]  # code-written, numbers as fact segments
    limitations: list[Limitation]  # guard failures, ties, rounding, coverage (V-LIM's inputs)
    viz_specs: list[VizSpec]


class ObservedRenderer(Protocol):
    """Fills versioned templates from facts. Its output reaches the screen before the composer runs.

    Every number is a fact segment whose text is `format_fact(value, unit)`; a limitation that names
    a tool-returned number carries `segments`; a ranking lists at most VIEW_RANK_CAP rows in all and
    then one sentence citing the ranking's count fact; a viz spec's requestPinnedHash comes from
    `models.derive_request_pinned` over the cited call's request.
    """

    def render(
        self,
        plan: CompiledPlan,
        steps: Sequence[StepView],
        facts: Sequence[FactRecord],
        calls: Sequence[CallRecord],
        cohorts: Sequence[CohortRef],
    ) -> ObservedResult: ...


# --- Composer ------------------------------------------------------------------------------------


class ComposerStatement(RoleModel):
    # Prose with fact placeholders only: no digits, number words or gene symbols outside them (V-NUM).
    text: str = Field(min_length=1, max_length=300)
    cites: list[FactId] = Field(min_length=1)  # V-REF: every item cites at least one fact


class ComposerHypothesis(ComposerStatement):
    form: Literal["question", "action"]  # never a finding (V-SEC)


class ComposerFollowUp(RoleModel):
    kind: AnalysisKind  # a kind the compiler can run; anything else becomes an open link
    label: str = Field(min_length=1, max_length=60)
    question: str = Field(min_length=1, max_length=200)


class ComposerOutput(RoleModel):
    """The composer's structured output (schema sent with the call)."""

    # Bounded so the largest view fits a 32 KB snapshot (contract "Size: 32 KB").
    interpretation: list[ComposerStatement] = Field(min_length=1, max_length=4)
    hypotheses: list[ComposerHypothesis] = Field(max_length=3)
    follow_ups: list[ComposerFollowUp] = Field(max_length=3)
    open_app_ids: list[AppId] = Field(max_length=3)


class ComposerResult(RoleModel):
    output: ComposerOutput | None  # None when both models declined (MODEL_DECLINED, ObservedOnly)
    model_calls: list[ModelCallRecord]
    serving_model: str | None
    declined: bool
    decline_reason: str | None


# --- Verifier ------------------------------------------------------------------------------------

VerifiedPart = Literal["interpretation", "hypotheses", "follow_ups", "observed", "limitations"]


class RuleFinding(RoleModel):
    rule: VerifierRule
    part: VerifiedPart
    index: int | None  # the item's index in its part; null for a whole-part finding (V-COMP, V-LIM)
    passed: bool
    detail: str


class VerifierResult(RoleModel):
    passed: bool  # every rule passed on this attempt, nothing dropped or inserted
    retry: bool  # a model-text rule failed on the first attempt: re-compose once with `findings`
    findings: list[RuleFinding]
    # Placeholders resolved into segments with factIds; a dropped item stays as a Sentence with `dropped`.
    interpretation: list[Sentence]
    hypotheses: list[Sentence]
    follow_ups: list[FollowUp]
    open_links: list[NearestApp]
    inserted_observed: list[Sentence]  # V-COMP: template sentences code added to Observed
    inserted_limitations: list[Limitation]  # V-LIM: lines code added to Limitations
    observed_only: bool  # no Interpretation sentence survived: the run ends ObservedOnly


class Composer(Protocol):
    """Writes Interpretation and Hypotheses from the question, the facts and Observed.

    `allow_list` is the verifier's V-NUM allow-list for this run (Verifier.allow_list), sent in the
    prompt so the model knows which terms with digits or gene symbols it may write outside
    placeholders; `ledger` names the thread cohorts. Honours asyncio cancellation.
    """

    async def compose(
        self,
        question: str,
        facts: Sequence[FactRecord],
        plan: CompiledPlan,
        observed: ObservedResult,
        *,
        allow_list: Sequence[str],
        ledger: SessionLedger | None,
        feedback: Sequence[RuleFinding] = (),
    ) -> ComposerResult: ...


class Verifier(Protocol):
    """V-NUM, V-REF, V-ENT, V-SEC on model text; V-COMP and V-LIM on Observed and Limitations.

    The V-NUM allow-list comes from `vocab`, the plan and `ledger`: vocabulary terms with digits
    (log2FC, ISS stage values), the run's and the ledger's cohort names (which may contain a gene
    symbol, such as "KRAS mutated (ORION)"), and app names (Gen3 Analysis). A follow-up's label and
    question may carry one integer from 1 to 100 when its kind takes a top N. A resolved placeholder's
    segment text is `format_fact(value, unit)`. Each kept sentence keeps the composer's `cites`.
    """

    def allow_list(self, vocab: Vocabulary, ledger: SessionLedger | None, plan: CompiledPlan) -> list[str]: ...

    def verify(
        self,
        composer_output: ComposerOutput,
        facts: Sequence[FactRecord],
        ledger: SessionLedger | None,
        vocab: Vocabulary,
        *,
        plan: CompiledPlan,
        observed: ObservedResult,
        attempt: int,
        allow_list: Sequence[str],
    ) -> VerifierResult: ...

    def treatment_defined(
        self,
        facts: Sequence[FactRecord],
        ledger: SessionLedger | None,
        vocab: Vocabulary,
        *,
        plan: CompiledPlan,
        observed: ObservedResult,
        allow_list: Sequence[str],
    ) -> bool:
        """Whether the run's own scope, cohorts or settings are defined by a treatment (a regimen or
        therapy value): such a run gets no model-written text at all (V-SEC), only Observed and
        Limitations, because no word list can tell a description of a treatment group from advice."""
        ...


# --- Session ledger ------------------------------------------------------------------------------


class SessionLedgerStore(Protocol):
    """Builds the thread's ledger from its run records, never from model summaries (A2 ownership table)."""

    async def ledger(self, thread_id: str, *, before_run_id: str | None = None) -> SessionLedger: ...
