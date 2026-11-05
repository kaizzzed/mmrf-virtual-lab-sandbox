"""The ORION run-event contract v1 (orion/contracts/README.md) as Pydantic models.

These models validate both directions of the wire: the `RunAgentInput.forwardedProps.orion`
payload the browser sends, and every SSE frame the agent emits.
"""

import hashlib
import json
from collections.abc import Iterator
from typing import Annotated, Final, Literal, get_args

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, TypeAdapter, model_validator
from pydantic.alias_generators import to_camel

from .catalog import AppId, app_title

RunStatus = Literal[
    "Planning",
    "Clarifying",
    "AwaitingConsent",
    "Executing",
    "AwaitingConfirmation",
    "Stopping",
    "Answering",
    "Complete",
    "Partial",
    "Limited",
    "Unsupported",
    "ObservedOnly",
    "Cancelled",
    "Failed",
    "Interrupted",
]
StepStatus = Literal[
    "Queued", "AwaitingUser", "Running", "Retrying", "Done", "Empty", "Failed", "Skipped", "Reused"
]
LimitationCode = Literal[
    "NAMED_TOOL_MISMATCH",
    "OUT_OF_PANEL",
    "INELIGIBLE_GROUP",
    "TOOL_FAILED",
    "MODEL_DECLINED",
    "PLANNER_DECLINED",
    "UNSUPPORTED_ANALYSIS",
    "CLINICAL_ADVICE",
    "OUT_OF_SCOPE",
    "EMPTY_COHORT",
    "WRITE_SKIPPED",
    "RUN_INTERRUPTED",
    "WRITE_VERIFY_FAILED",
    "VIEWER_P_ROUNDED",
    "UNSUPPORTED_FIELD",
    "UNKNOWN_FIELD",
    "UNKNOWN_VALUE",
]
ControllerCode = Literal["INVALID_INPUT", "NOT_IMPLEMENTED", "RUN_BUSY", "RUN_NOT_FOUND", "INTERNAL_ERROR"]
Code = Literal[LimitationCode, ControllerCode]
FinishStatus = Literal[
    "Clarifying",
    "AwaitingConsent",
    "AwaitingConfirmation",
    "Complete",
    "Partial",
    "Limited",
    "Unsupported",
    "ObservedOnly",
    "Cancelled",
    "Interrupted",
    "Warmed",
]
ScopeSource = Literal["all_cases", "active_cohort", "saved_cohort", "thread_cohort"]
StepKind = Literal["server", "exec", "client"]

RUN_STATUSES: Final = frozenset(get_args(RunStatus))
STEP_STATUSES: Final = frozenset(get_args(StepStatus))
CODES: Final = frozenset(get_args(LimitationCode)) | frozenset(get_args(ControllerCode))
# A RUN_ERROR with one of these rejects the invocation: no run is created and no run's status changes.
REJECTION_CODES: Final[frozenset[Code]] = frozenset({"INVALID_INPUT", "RUN_BUSY", "RUN_NOT_FOUND", "NOT_IMPLEMENTED"})
WAITING_STATUSES: Final[frozenset[RunStatus]] = frozenset({"Clarifying", "AwaitingConsent", "AwaitingConfirmation"})
END_STATUSES: Final[frozenset[RunStatus]] = frozenset(
    {"Complete", "Partial", "Limited", "Unsupported", "ObservedOnly", "Cancelled", "Failed", "Interrupted"}
)
LEASED_STATUSES: Final[frozenset[RunStatus]] = frozenset({"Planning", "Executing", "Answering"})
BUSY_STATUSES: Final[frozenset[RunStatus]] = LEASED_STATUSES | {"Stopping"}

RUN_VIEW_SCHEMA: Final = "orion.run-view/v1"
VIZ_PINNED_SCHEMA: Final = "orion.viz-pinned/v1"
SNAPSHOT_LIMIT_BYTES: Final = 32 * 1024

HexId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
IsoTime = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
StepId = Annotated[str, Field(pattern=r"^s[1-9][0-9]*$")]
AssumptionCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]


class WireModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )


class Scope(WireModel):
    source: ScopeSource
    name: str
    case_count: int | None = Field(ge=0)


class PlanRow(WireModel):
    row: int = Field(ge=1)
    label: str
    tool: str
    reason: str
    step_ids: list[StepId]


class Assumption(WireModel):
    code: AssumptionCode
    text: str


class PlanView(WireModel):
    version: int = Field(ge=1)
    signature: Sha256Hex
    scope: Scope
    rows: list[PlanRow]
    assumptions: list[Assumption]
    writes: bool
    paraphrase: str


class StepView(WireModel):
    id: StepId
    row: int = Field(ge=1)
    tool: str
    kind: StepKind
    folded: bool
    input_summary: str
    status: StepStatus
    output_summary: str | None
    duration_ms: int | None = Field(ge=0)
    code: Code | None


class Fact(WireModel):
    id: str
    label: str
    value: int | float
    unit: str
    step_id: StepId
    row: int = Field(ge=1)
    tool: str
    json_pointer: Annotated[str, Field(pattern=r"^(/([^/~]|~[01])*)*$")]


class Segment(WireModel):
    text: str
    fact_id: str | None = Field(default=None, exclude_if=lambda value: value is None)


class Sentence(WireModel):
    segments: list[Segment]


class NearestApp(WireModel):
    app_id: AppId
    title: str

    @classmethod
    def of(cls, app_id: AppId) -> "NearestApp":
        return cls(app_id=app_id, title=app_title(app_id))


class Limitation(WireModel):
    code: Code
    text: str
    unblock: str | None
    nearest_apps: list[NearestApp] = Field(default_factory=list)


# --- Pinned viz inputs: what a card mounts, and the hash both sides compute ------------

Gen3Filter = dict[str, JsonValue]
PinnedKind = Literal["survival", "oncomatrix", "deDirect", "deInput"]


class SurvivalPinned(WireModel):
    kind: Literal["survival"]
    filter0: Gen3Filter | None
    term: str
    overlay: str


class OncomatrixPinned(WireModel):
    kind: Literal["oncomatrix"]
    filter0: Gen3Filter | None
    genes: list[str]


class SampleGroup(WireModel):
    name: str
    sample_ids: list[int]


def _refuse_null(value: object) -> object:
    if value is None:
        raise ValueError("an absent optional field is omitted, never written as null")
    return value


# None stands for "absent" and is dumped as no key; the browser hashes a null it is sent as null,
# so accepting one would let a frame carry a pinnedHash the browser cannot reproduce.
_NOT_NULL: Final = BeforeValidator(_refuse_null)


class Volcano(WireModel):
    fold_change_cutoff: Annotated[float | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)
    p_value: Annotated[float | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)
    p_value_type: Annotated[str | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)
    rank_by: Annotated[str | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)


class DeDirectPinned(WireModel):
    kind: Literal["deDirect"]
    groups: list[SampleGroup] = Field(min_length=2, max_length=2)
    method: Literal["wilcoxon", "edgeR", "limma"]
    volcano: Annotated[Volcano | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)


class CohortGroup(WireModel):
    name: str
    cohort_id: str
    filter0: Gen3Filter


class DeInputPinned(WireModel):
    kind: Literal["deInput"]
    groups: list[CohortGroup] = Field(min_length=2, max_length=2)


Pinned = Annotated[SurvivalPinned | OncomatrixPinned | DeDirectPinned | DeInputPinned, Field(discriminator="kind")]
PINNED: Final[TypeAdapter[Pinned]] = TypeAdapter(Pinned)
# JavaScript reads a larger integer back rounded, so it would hash a different number.
_MAX_SAFE_INTEGER: Final = 2**53 - 1


class PinnedNumberError(ValueError):
    """A pinned number outside the range that JavaScript and Python print identically."""


def _canonical_number(value: int | float) -> int | float:
    # JSON has one number type, so an integral float prints as an integer, as JavaScript prints it.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise PinnedNumberError(f"{value} is beyond JavaScript's safe integer range")
        return value
    if not 1e-4 <= abs(value) < 1e15:
        raise PinnedNumberError(f"{value!r} is outside 1e-4 <= |x| < 1e15")
    return value


def _canonical_numbers(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _canonical_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_numbers(item) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _canonical_number(value)
    return value


def canonical_pinned(pinned: Pinned) -> str:
    """The contract's canonical JSON of `{"schema": "orion.viz-pinned/v1", ...pinned}`.

    Normalization sorts each deDirect group's sampleIds; every other array keeps its order.
    Raises PinnedNumberError for a number outside the contract's range.
    """
    if isinstance(pinned, DeDirectPinned):
        groups = [group.model_copy(update={"sample_ids": sorted(group.sample_ids)}) for group in pinned.groups]
        pinned = pinned.model_copy(update={"groups": groups})
    data = _canonical_numbers({"schema": VIZ_PINNED_SCHEMA, **pinned.model_dump(mode="json")})
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def pinned_hash(pinned: Pinned) -> str:
    """sha256 (lowercase hex) of the UTF-8 canonical JSON of the pinned inputs."""
    return hashlib.sha256(canonical_pinned(pinned).encode("utf-8")).hexdigest()


class VizSpec(WireModel):
    viz_id: str
    app_id: str
    launcher: PinnedKind
    cites_steps: list[StepId]
    pinned: Pinned
    pinned_hash: Sha256Hex

    @model_validator(mode="after")
    def _pinned_is_consistent(self) -> "VizSpec":
        if self.launcher != self.pinned.kind:
            raise ValueError(f"launcher {self.launcher!r} must equal pinned.kind {self.pinned.kind!r}")
        if self.pinned_hash != pinned_hash(self.pinned):
            raise ValueError("pinnedHash does not match the pinned inputs")
        return self


class RunView(WireModel):
    schema_: Literal["orion.run-view/v1"] = Field(validation_alias="schema", serialization_alias="schema")
    thread_id: HexId
    run_id: HexId
    revision: int = Field(ge=1)
    status: RunStatus
    code: Code | None
    question: str
    updated_at: IsoTime
    plan: PlanView | None
    steps: list[StepView]
    facts: list[Fact]
    observed: list[Sentence]
    limitations: list[Limitation]
    # The composer (Day 2) defines these; until then they stay null, so the shape is provisional.
    interpretation: list[Sentence] | None
    hypotheses: list[Sentence] | None
    withheld_reason: str | None
    viz_specs: list[VizSpec]


class RunStartedFrame(WireModel):
    type: Literal["RUN_STARTED"]
    thread_id: HexId
    run_id: HexId


class StateSnapshotFrame(WireModel):
    type: Literal["STATE_SNAPSHOT"]
    snapshot: RunView


class StepStartedFrame(WireModel):
    type: Literal["STEP_STARTED"]
    step_name: StepId


class StepFinishedFrame(WireModel):
    type: Literal["STEP_FINISHED"]
    step_name: StepId


class ToolCallStartFrame(WireModel):
    type: Literal["TOOL_CALL_START"]
    tool_call_id: str
    tool_call_name: Literal["save_cohorts"]


class ToolCallArgsFrame(WireModel):
    type: Literal["TOOL_CALL_ARGS"]
    tool_call_id: str
    delta: str


class ToolCallEndFrame(WireModel):
    type: Literal["TOOL_CALL_END"]
    tool_call_id: str


class TextMessageStartFrame(WireModel):
    type: Literal["TEXT_MESSAGE_START"]
    message_id: str
    role: Literal["assistant"]


class TextMessageContentFrame(WireModel):
    type: Literal["TEXT_MESSAGE_CONTENT"]
    message_id: str
    delta: str


class TextMessageEndFrame(WireModel):
    type: Literal["TEXT_MESSAGE_END"]
    message_id: str


class HeartbeatValue(WireModel):
    ts: IsoTime


class HeartbeatFrame(WireModel):
    type: Literal["CUSTOM"]
    name: Literal["orion.heartbeat"]
    value: HeartbeatValue


class RunResult(WireModel):
    status: FinishStatus
    code: Code | None


class RunFinishedFrame(WireModel):
    type: Literal["RUN_FINISHED"]
    thread_id: HexId
    run_id: HexId
    result: RunResult


class RunErrorFrame(WireModel):
    type: Literal["RUN_ERROR"]
    message: str
    code: Code


WireEvent = Annotated[
    RunStartedFrame
    | StateSnapshotFrame
    | StepStartedFrame
    | StepFinishedFrame
    | ToolCallStartFrame
    | ToolCallArgsFrame
    | ToolCallEndFrame
    | TextMessageStartFrame
    | TextMessageContentFrame
    | TextMessageEndFrame
    | HeartbeatFrame
    | RunFinishedFrame
    | RunErrorFrame,
    Field(discriminator="type"),
]
TERMINAL_FRAMES: Final = (RunFinishedFrame, RunErrorFrame)
_WIRE_EVENT: Final[TypeAdapter[WireEvent]] = TypeAdapter(WireEvent)


def parse_frame(data: str) -> WireEvent:
    """Validate one frame's `data:` payload against the contract."""
    return _WIRE_EVENT.validate_json(data)


def iter_sse_data(stream: str) -> Iterator[str]:
    """Yield each SSE event's data, ignoring event/id/retry fields and comment lines."""
    for block in stream.replace("\r\n", "\n").split("\n\n"):
        data = [
            line[5:].removeprefix(" ")
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        if data:
            yield "\n".join(data)


# --- Request: RunAgentInput.forwardedProps.orion ---------------------------------------


class FilterSet(WireModel):
    """A Gen3 FilterSet as the browser's cohort store holds it."""

    mode: Literal["and", "or"]
    root: dict[str, dict[str, JsonValue]]


class ActiveCohort(WireModel):
    id: str | None
    name: str = Field(min_length=1, max_length=200)
    filters: FilterSet
    case_count: int | None = Field(ge=0)


class SavedCohort(WireModel):
    id: str
    name: str = Field(min_length=1, max_length=200)
    filters: FilterSet
    case_count: int | None = Field(ge=0)


class CohortContext(WireModel):
    active: ActiveCohort
    saved: list[SavedCohort] = Field(max_length=200)


class AskAction(WireModel):
    action: Literal["ask"]
    question: str = Field(min_length=1, max_length=4000)
    cohort_context: CohortContext


class WarmupAction(WireModel):
    action: Literal["warmup"]


class CancelAction(WireModel):
    action: Literal["cancel"]


OrionAction = Annotated[AskAction | WarmupAction | CancelAction, Field(discriminator="action")]
ORION_ACTION: Final[TypeAdapter[OrionAction]] = TypeAdapter(OrionAction)
RESERVED_ACTIONS: Final = frozenset(
    {"clarify_answer", "consent", "confirm", "edit_plan", "retry_step", "viz_report"}
)
