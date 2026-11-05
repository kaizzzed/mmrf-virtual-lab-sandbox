"""The tool contract (RFA rev 25, A2 "Tools" class diagram, A4 timeout budget). Types and Protocols only.

Every counted call shares one shape: a ToolContract validates its input against `input_schema`,
checks its guards, and returns a ToolResult whose `provenance` lists each HTTP request it made.
Server tools run in-process (mode L) or behind AgentCore Gateway (`vlab___<tool>`, modes A and H);
a ToolClient hides the difference and reports every attempt. The executor's pure functions are
ExecutorFunctions with versioned signatures below. Client actions run in the browser; their
agent side only checks what the browser reports back.

`ToolResult` is also the MCP tool output the Gateway's Lambda returns, as camelCase JSON. Full
outputs stay inline: every tool caps its response below Lambda's 6 MB synchronous limit and the
run record keeps it in `calls[].observed` (the design's ArtifactRef is not part of the MVP).

Cancellation (contract "cancel"): ToolClient.call honours asyncio cancellation. Cancelling it
aborts the HTTP request or Gateway invocation at once and re-raises CancelledError; the executor
then records that attempt as a CallRecord with status `error` and an `error` that starts
"CANCELLED", and skips the step "Not run: stopped".
"""

from collections.abc import Mapping, Sequence
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel

from ..models import CallId, CaseId, Code, HexId, IsoTime, Sha256Hex, StepId

# Re-exported: the record keeps these as wire types, so they live in models.py.
from ..models import Provenance as Provenance
from ..models import ToolStatus as ToolStatus
from ..models import Transport as Transport

ServerToolName = Literal[
    "fixture_fingerprint",
    "cohort_count",
    "cases_query",
    "gene_cases",
    "top_mutated_genes",
    "survival_compare",
    "sample_lists",
    "de_preanalysis",
    "de_run",
    "kg_query",
]
ExecutorFunctionName = Literal["set_operation", "check_subset", "rank_top_n", "derive_km_median"]
ClientActionName = Literal["read_cohort_context", "save_cohorts", "set_active_cohort", "mount_viz", "open_full_tool"]
ToolName = ServerToolName | ExecutorFunctionName | ClientActionName

# AgentCore Gateway: one target `vlab` with the nine server tools; `kg_query` is the stretch target `kg`.
GATEWAY_TARGET: Final = "vlab"
KG_GATEWAY_TARGET: Final = "kg"
GATEWAY_TOOL_SEPARATOR: Final = "___"

# A4 timeout budget. Each timer sits inside the one of the layer that calls it.
TOOL_HTTP_TIMEOUT_S: Final = 55.0
LAMBDA_TIMEOUT_S: Final = 58.0
STEP_TIMEOUT_S: Final = 60.0
STEP_CAP_S: Final = 130.0
BACKOFF_429_S: Final = (10.0, 20.0)
RETRY_HTTP_STATUSES: Final = frozenset({429, 502, 503, 504})
MAX_ATTEMPTS: Final = 2  # one retry, reads only, identical body; client writes never retry
# A2 tool table and C-GUARD limits.
MAX_TOOL_CALLS_PER_RUN: Final = 20
MAX_CASE_IDS: Final = 10_000
CASES_PAGE_SIZE: Final = 1000
MIN_SURVIVAL_GROUP: Final = 10
MIN_DE_ELIGIBLE: Final = 10
MAX_DE_SAMPLES: Final = 4000
MAX_SAMPLE_LIST_BODY_BYTES: Final = 8 * 1024 * 1024
MAX_TOOL_RESPONSE_BYTES: Final = 6_000_000  # below Lambda's 6 MB synchronous limit (Assumption, A4)


class ToolModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )


class Guard(ToolModel):
    """A check before (`pre`) or after (`post`) a call; a failure becomes `on_fail` under Limitations."""

    rule: str  # such as "min_group_size>=10" or "subset_of:s3"
    on_fail: Code
    phase: Literal["pre", "post"]


ToolErrorCode = Literal[
    "INVALID_INPUT",  # the input failed `input_schema` or the allow-list; never sent
    "GUARD_FAILED",  # a pre-guard stopped the call; `limitation` names the code
    "HTTP_ERROR",  # a non-2xx status that is not retryable
    "TIMEOUT",  # the 55 s HTTP timeout, or the 60 s step timeout
    "CACHE_BUSY",  # ProteinPaint 429 CACHE_BUSY after the backoff
    "UPSTREAM_SHAPE",  # a 2xx response that does not match `output_schema`
    "NOT_CONNECTED",  # kg_query before its target is registered (KG_NOT_CONNECTED)
    "CANCELLED",  # a Stop or an edit aborted the attempt; never retried
]


class ToolError(ToolModel):
    code: ToolErrorCode
    message: str
    http_status: int | None = None
    retryable: bool


class ToolResult(ToolModel):
    """What every tool returns (A2 class diagram). `empty` is a zero result, never success with no rows."""

    status: ToolStatus
    observed: JsonValue
    warnings: list[str] = Field(default_factory=list)
    limitation: Code | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    error: ToolError | None = None


class ToolContext(ToolModel):
    """Per-call context a tool receives; ids flow into logs and spans (NFR-05)."""

    run_id: HexId
    step_id: StepId
    call_id: CallId
    attempt: int = Field(ge=1, le=MAX_ATTEMPTS)
    timeout_s: float = Field(gt=0)


class ToolContract(Protocol):
    """The shared shape of every server tool, pure function and client action."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...  # such as "cases_query@1"; recorded on every call

    @property
    def input_schema(self) -> Mapping[str, JsonValue]: ...  # JSON Schema; also the MCP inputSchema

    @property
    def output_schema(self) -> Mapping[str, JsonValue]: ...  # JSON Schema of ToolResult.observed

    @property
    def guards(self) -> Sequence[Guard]: ...

    async def invoke(self, input: JsonValue, ctx: ToolContext) -> ToolResult: ...


class ServerTool(ToolContract, Protocol):
    """A read-only HTTP tool through the identity gateway. Its (method, path) pairs are allow-listed."""

    @property
    def endpoint(self) -> str: ...  # such as "POST /analysis/v0/cases/"

    @property
    def golden_template(self) -> JsonValue: ...  # the captured viewer body (orion/golden), or null

    @property
    def timeout_s(self) -> float: ...  # the HTTP timeout, at most TOOL_HTTP_TIMEOUT_S


class ExecutorFunction(ToolContract, Protocol):
    """A versioned pure function the executor runs in-process; recorded as a step like any tool."""

    @property
    def pure(self) -> Literal[True]: ...


class ClientAction(ToolContract, Protocol):
    """A browser action. `invoke` runs agent-side and only checks what the browser reported."""

    @property
    def requires_click(self) -> bool: ...

    @property
    def writes(self) -> bool: ...  # true for save_cohorts and set_active_cohort only


class Attempt(ToolModel):
    # Each attempt is its own CallRecord, with its own callId (`<runId>.v<n>.<stepId>.c<k>`).
    call_id: CallId
    attempt: int = Field(ge=1, le=MAX_ATTEMPTS)
    started_at: IsoTime
    elapsed_ms: int = Field(ge=0)
    status: ToolStatus
    http_status: int | None
    error: ToolError | None
    # The wait before the next attempt (a 429 backs off 10–20 s); 0 for the last attempt.
    backoff_ms: int = Field(ge=0)


class ToolCallOutcome(ToolModel):
    """One call through a ToolClient: the final result plus every attempt, for CallRecords and spans.

    Facts cite the last attempt's callId, the one whose result was used.
    """

    tool: str
    tool_version: str
    transport: Transport
    request: JsonValue
    request_sha256: Sha256Hex  # canonical JSON of `request`; identical on a retry
    result: ToolResult
    response_sha256: Sha256Hex | None  # canonical JSON of result.observed
    attempts: list[Attempt] = Field(min_length=1, max_length=MAX_ATTEMPTS)
    elapsed_ms: int = Field(ge=0)  # every attempt and backoff, within STEP_CAP_S


class RetryHook(Protocol):
    """Called before a retry's backoff with the next attempt number and the wait in ms."""

    async def __call__(self, attempt: int, backoff_ms: int) -> None: ...


class ToolClient(Protocol):
    """Calls a server tool in-process (mode L) or through Gateway MCP over SigV4 (modes A and H).

    Applies the A4 budget: per-attempt `timeout_s`, one retry with an identical body on 429, 502,
    503, 504 or a timeout (after BACKOFF_429_S on 429), and never raises for a tool failure: the
    outcome's result has status `error` or `unavailable` instead. `on_retry` runs before the
    backoff of a retry, so the executor can report the step Retrying (StepSession.retrying).
    The attempts take callIds `<runId>.v<plan_version>.<step_id>.c<first_call>`, then c<first_call + 1>.
    A fault from ORION_FAULTS for this tool (settings.py) replaces the HTTP call in mode L and in a
    test Runtime only.

    Cancelling the call aborts the in-flight request at once and re-raises CancelledError.
    """

    @property
    def transport(self) -> Literal["in_process", "gateway"]: ...

    async def call(
        self,
        name: ServerToolName,
        input: JsonValue,
        *,
        timeout_s: float,
        run_id: str,
        plan_version: int,
        step_id: str,
        first_call: int,
        on_retry: RetryHook | None = None,
    ) -> ToolCallOutcome: ...



# --- Pure function signatures (A2: set_operation, check_subset, rank_top_n, derive_km_median) ---

SetOp = Literal["difference", "intersection", "union", "size"]


class SetOperationResult(ToolModel):
    version: str  # "set_operation@1"
    op: SetOp
    a_count: int = Field(ge=0)
    b_count: int | None = Field(ge=0)
    count: int = Field(ge=0)
    case_ids: list[CaseId] = Field(max_length=MAX_CASE_IDS)  # sorted ascending; empty for op "size"


class SubsetResult(ToolModel):
    version: str  # "check_subset@1"
    inner_count: int = Field(ge=0)
    outer_count: int = Field(ge=0)
    is_subset: bool
    outside_count: int = Field(ge=0)
    outside_sample: list[CaseId] = Field(max_length=10)  # the first outside ids, sorted, for the limitation


class RankedRow(ToolModel):
    rank: int = Field(ge=1)  # ties share a rank
    key: str  # the row's identity, such as a gene symbol
    value: float  # the ranking value, such as adjusted p
    row: dict[str, JsonValue]  # the source row, unchanged


class RankResult(ToolModel):
    version: str  # "rank_top_n@1"
    rank_by: str
    ascending: bool
    n_requested: int = Field(ge=1)
    rows: list[RankedRow]  # the top n plus every row tied with the n-th (TIES_AT_CUTOFF)
    count: int = Field(ge=0)  # len(rows); the fact behind Observed's "all <count> ranked" line past VIEW_RANK_CAP
    ties_at_cutoff: list[str]  # keys tied at the cutoff, disclosed under Limitations
    total_rows: int = Field(ge=0)  # FEWER_THAN_N when below n_requested


class KmPoint(ToolModel):
    time: float = Field(ge=0)
    survival: float = Field(ge=0, le=1)


class KmCurve(ToolModel):
    label: str
    points: list[KmPoint]  # the engine's curve as returned, time ascending


class KmMedian(ToolModel):
    label: str
    median: float | None  # first time survival <= 0.5; null when the curve never reaches it
    reached: bool


class KmMedianResult(ToolModel):
    version: str  # "derive_km_median@1"
    unit: str  # the curve's time unit, such as "days"
    groups: list[KmMedian]


class SetOperationFn(Protocol):
    def __call__(self, op: SetOp, a: Sequence[str], b: Sequence[str] = ()) -> SetOperationResult: ...


class CheckSubsetFn(Protocol):
    def __call__(self, inner: Sequence[str], outer: Sequence[str]) -> SubsetResult: ...


class RankTopNFn(Protocol):
    """Deterministic: ties at the cutoff are kept and listed; `tie_break` orders rows with equal values."""

    def __call__(
        self, rows: Sequence[Mapping[str, JsonValue]], *, key: str, rank_by: str, n: int, ascending: bool, tie_break: str
    ) -> RankResult: ...


class DeriveKmMedianFn(Protocol):
    def __call__(self, curves: Sequence[KmCurve], *, unit: str) -> KmMedianResult: ...
