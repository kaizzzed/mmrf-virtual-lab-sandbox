"""The ORION run-event contract v1.2 (orion/contracts/README.md) as Pydantic models.

These models validate both directions of the wire: the `RunAgentInput.forwardedProps.orion`
payload the browser sends, every SSE frame the agent emits, and the run record the BFF's
record route returns. The frontend mirrors every type here in
`src/features/orion/agui/types.ts`; change both together.
"""

import hashlib
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Final, Literal, get_args

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, TypeAdapter, model_validator
from pydantic.alias_generators import to_camel

from .catalog import AnalysisKind, AppId, app_title
from .ir import BASE_REF
from .ir import Plan as IrPlan

CONTRACT_VERSION: Final = "1.2"

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
    # v1.2: guard and coverage codes (A2 C-GUARD, tools table, answer anatomy).
    "AMBIGUOUS_REQUEST",
    "TOO_MANY_IDS",
    "NOT_SUBSET",
    "FEWER_THAN_N",
    "TIES_AT_CUTOFF",
    "PANEL_COVERAGE",
    "STAT_UNAVAILABLE",
    "REQUEST_TOO_LARGE",
    "CALL_BUDGET",
    "KG_NOT_CONNECTED",
    # v1.2: de_run found more genes past the thresholds than ProteinPaint returns as dots (DE_MAX_DOTS).
    "ROWS_CAPPED",
]
ControllerCode = Literal[
    "INVALID_INPUT", "NOT_IMPLEMENTED", "RUN_BUSY", "RUN_NOT_FOUND", "STALE_ACTION", "INTERNAL_ERROR"
]
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
    # Not run statuses: `warmup` ends Warmed, a standalone `viz_report` ends Recorded.
    "Warmed",
    "Recorded",
]
ScopeSource = Literal["all_cases", "active_cohort", "saved_cohort", "thread_cohort"]
StepKind = Literal["server", "exec", "client"]
DeMethod = Literal["wilcoxon", "edgeR", "limma"]
VerifierRule = Literal["V-NUM", "V-REF", "V-ENT", "V-SEC", "V-COMP", "V-LIM"]
ActionName = Literal[
    "warmup", "ask", "cancel", "clarify_answer", "consent", "confirm", "edit_plan", "retry_step", "viz_report"
]

RUN_STATUSES: Final = frozenset(get_args(RunStatus))
STEP_STATUSES: Final = frozenset(get_args(StepStatus))
CODES: Final = frozenset(get_args(LimitationCode)) | frozenset(get_args(ControllerCode))
# A RUN_ERROR with one of these rejects the invocation: no run is created and no run's status changes.
REJECTION_CODES: Final[frozenset[Code]] = frozenset(
    {"INVALID_INPUT", "RUN_BUSY", "RUN_NOT_FOUND", "NOT_IMPLEMENTED", "STALE_ACTION"}
)
WAITING_STATUSES: Final[frozenset[RunStatus]] = frozenset({"Clarifying", "AwaitingConsent", "AwaitingConfirmation"})
END_STATUSES: Final[frozenset[RunStatus]] = frozenset(
    {"Complete", "Partial", "Limited", "Unsupported", "ObservedOnly", "Cancelled", "Failed", "Interrupted"}
)
# A run in one of these statuses holds a lease in its record. The controller renews it every 30 s
# while Planning, Executing or Answering; entering Stopping sets a lease that lapses STOP_LEASE_S
# later and is never renewed, because Stop aborts the in-flight call at once (contract "cancel").
LEASED_STATUSES: Final[frozenset[RunStatus]] = frozenset({"Planning", "Executing", "Answering", "Stopping"})
RENEWED_STATUSES: Final[frozenset[RunStatus]] = frozenset({"Planning", "Executing", "Answering"})
BUSY_STATUSES: Final[frozenset[RunStatus]] = LEASED_STATUSES
STOP_LEASE_S: Final = 30.0
# The executor has this long to abort its in-flight call after Stop before the controller cancels it.
STOP_GRACE_S: Final = 5.0

# The run statuses in which each action applies to its run (contract "Actions"). Anything else
# is STALE_ACTION, except a repeated confirm nonce, which returns the recorded result.
ACTION_STATUSES: Final[dict[ActionName, frozenset[RunStatus]]] = {
    "clarify_answer": frozenset({"Clarifying"}),
    "consent": frozenset({"AwaitingConsent"}),
    "confirm": frozenset({"AwaitingConfirmation"}),
    "edit_plan": frozenset({"Executing", "AwaitingConfirmation", "Answering", "Complete", "Partial", "Limited", "ObservedOnly"}),
    "retry_step": frozenset({"Partial"}),
    "cancel": frozenset(RUN_STATUSES),
    "viz_report": frozenset(RUN_STATUSES),
}

RUN_VIEW_SCHEMA: Final = "orion.run-view/v1"
VIZ_PINNED_SCHEMA: Final = "orion.viz-pinned/v1"
COHORT_DEF_SCHEMA: Final = "orion.cohort-def/v1"
FINGERPRINT_SCHEMA: Final = "orion.fixture-fingerprint/v1"
SNAPSHOT_LIMIT_BYTES: Final = 32 * 1024
# TOOL_CALL_ARGS and TEXT_MESSAGE_CONTENT deltas are at most this many characters, so every frame
# stays under 32 KB.
TOOL_CALL_ARGS_CHUNK: Final = 8 * 1024
TEXT_MESSAGE_CHUNK: Final = 8 * 1024
SYNTHETIC_NOTICE: Final = "Synthetic sandbox — all data is fabricated and has no clinical meaning."
MAX_CASE_IDS: Final = 10_000
# A view shows at most this many ranked rows in all (Observed's ranked lists, their facts and the
# frozen tables); the full ranking stays in the record's calls (contract "Size: 32 KB").
VIEW_RANK_CAP: Final = 20
# A frozen table shows at most this many rows (a DE card freezes to its top 10).
FROZEN_ROWS_CAP: Final = 10
# topN bounds per analysis kind: DE top_n and top mutated genes n (ir.py).
MAX_TOP_N_DE: Final = 100
MAX_TOP_N_GENES: Final = 50

HexId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
IsoTime = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
StepId = Annotated[str, Field(pattern=r"^s[1-9][0-9]*$")]
AssumptionCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]
# A fact id is also its placeholder in model text: {{f.s10.top.1.gene}}.
FactId = Annotated[str, Field(pattern=r"^f\.s[1-9][0-9]*(\.[A-Za-z0-9_-]+)+$")]
CohortRefId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
VizId = Annotated[str, Field(pattern=r"^v[1-9][0-9]*$")]
OptionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,120}$")]
CaseId = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]
CohortName = Annotated[str, Field(min_length=1, max_length=100)]
PlatformCohortId = Annotated[str, Field(min_length=1, max_length=200)]
# One attempt of one counted call: `<runId>.v<planVersion>.<stepId>.c<n>`, n counting every attempt
# of that step in that plan version (a retry and a retry_step rerun each take the next n).
CallId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}\.v[1-9][0-9]*\.s[1-9][0-9]*\.c[1-9][0-9]*$")]
# One model call: `<runId>.planner.c<n>` or `<runId>.v<planVersion>.composer.c<n>`.
ModelCallId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}\.(planner|v[1-9][0-9]*\.composer)\.c[1-9][0-9]*$")]


def call_id(run_id: str, plan_version: int, step_id: str, n: int) -> str:
    return f"{run_id}.v{plan_version}.{step_id}.c{n}"


def model_call_id(run_id: str, role: Literal["planner", "composer"], n: int, plan_version: int | None = None) -> str:
    if role == "planner":
        return f"{run_id}.planner.c{n}"
    if plan_version is None:
        raise ValueError("a composer call belongs to a plan version")
    return f"{run_id}.v{plan_version}.composer.c{n}"


class WireModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )


# --- Canonical JSON (pinned inputs and cohort definitions) -----------------------------

# JavaScript reads a larger integer back rounded, so it would hash a different number.
_MAX_SAFE_INTEGER: Final = 2**53 - 1


class PinnedNumberError(ValueError):
    """A hashed number outside the range that JavaScript and Python print identically."""


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


def canonical_json(value: JsonValue, *, portable: bool = True) -> str:
    """Keys sorted at every level, arrays kept in order, no whitespace, non-ASCII unescaped, null kept.

    `portable` (pinned inputs, cohort definitions: hashes the browser recomputes) also enforces the
    number range JavaScript and Python print alike. Record hashes (requestSha256, responseSha256)
    are computed by the agent only and use portable=False, so a p-value such as 7.1e-60 is allowed.
    """
    numbers = _canonical_numbers(value) if portable else value
    return json.dumps(numbers, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Fact display (contract "Fact values on screen") --------------------------------------


def _decimal(value: float) -> Decimal:
    # The shortest round-trip form, which JavaScript's Number#toString prints too.
    return Decimal(repr(float(value)))


def _plain(number: Decimal) -> str:
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _grouped(number: Decimal) -> str:
    sign = "-" if number < 0 else ""
    whole, _, fraction = _plain(abs(number)).partition(".")
    grouped = f"{int(whole):,}"
    return sign + grouped + (f".{fraction}" if fraction else "")


def format_fact(value: int | float | str, unit: str) -> str:
    """The one way a fact value is written on screen, in Observed, model text and TEXT_MESSAGE.

    Strings stay as they are. Unit "p": 0 prints "0", below 0.001 one decimal of e-notation
    ("7.1e-60"), otherwise three significant digits ("0.0342"). Unit "log2FC": two decimals
    ("1.12", "-0.87"). Any other number: thousands separators, at most two decimals with trailing
    zeros dropped ("1,095", "1,095.5"). Rounding is half away from zero on the shortest decimal
    form of the number. `fixtures/fact-format-vectors.json` holds the cases.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{value!r} is not a finite number")
    number = Decimal(value) if isinstance(value, int) else _decimal(value)
    if unit == "p":
        if number == 0:
            return "0"
        if abs(number) < Decimal("0.001"):
            exponent = number.adjusted()
            mantissa = number.scaleb(-exponent).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            if abs(mantissa) >= 10:
                mantissa, exponent = (mantissa / 10).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), exponent + 1
            return f"{mantissa}e{exponent}"
        step = Decimal(1).scaleb(number.adjusted() - 2)
        return _plain(number.quantize(step, rounding=ROUND_HALF_UP))
    if unit == "log2FC":
        text = format(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
        return "0.00" if text == "-0.00" else text
    return _grouped(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _refuse_null(value: object) -> object:
    if value is None:
        raise ValueError("an absent optional field is omitted, never written as null")
    return value


# None stands for "absent" and is dumped as no key; the browser hashes a null it is sent as null,
# so accepting one would let a frame carry a hash the browser cannot reproduce.
_NOT_NULL: Final = BeforeValidator(_refuse_null)

# A viz report's error is the viewer's own text (two joined viewer messages, a holder's textContent), so it
# can be any length. The agent clips it to VIZ_ERROR_MAX characters, the last one an ellipsis, instead of
# rejecting the turn it rides on (contract "vizReports": a bad report never refuses a Stop or a confirm).
VIZ_ERROR_MAX: Final = 500


def _clip_viz_error(value: object) -> object:
    if isinstance(value, str) and len(value) > VIZ_ERROR_MAX:
        return value[: VIZ_ERROR_MAX - 1] + "…"
    return value


VizError = Annotated[Annotated[str, Field(max_length=VIZ_ERROR_MAX)] | None, BeforeValidator(_clip_viz_error)]


# --- RunView parts ---------------------------------------------------------------------


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


class ScopeChoice(WireModel):
    """A scope the plan can switch to: the value of an `edit_plan` scope edit."""

    source: ScopeSource
    cohort_name: Annotated[str | None, _NOT_NULL] = Field(default=None, max_length=200, exclude_if=lambda v: v is None)
    ledger_ref: Annotated[str | None, _NOT_NULL] = Field(default=None, max_length=64, exclude_if=lambda v: v is None)


class DeThresholds(WireModel):
    """The DE significance thresholds an answer reports (C-DEFAULTS: adjusted p < 0.05, |log2FC| > 0.3)."""

    p_value_cutoff: float = Field(gt=0, le=1)
    p_value_type: Literal["adjusted", "original"]
    fold_change_cutoff: float = Field(ge=0)


class EditableScope(WireModel):
    field: Literal["scope"]
    label: str
    value: ScopeChoice


class EditableMethod(WireModel):
    field: Literal["method"]
    label: str
    value: DeMethod


class EditableTopN(WireModel):
    field: Literal["topN"]
    label: str
    value: int = Field(ge=1, le=MAX_TOP_N_DE)
    # The largest N the analysis takes: 100 for DE, 50 for top mutated genes. A larger edit is INVALID_INPUT.
    max: int = Field(ge=1, le=MAX_TOP_N_DE)

    @model_validator(mode="after")
    def _value_within_max(self) -> "EditableTopN":
        if self.value > self.max:
            raise ValueError(f"topN {self.value} exceeds this analysis's maximum {self.max}")
        return self


class EditableThresholds(WireModel):
    field: Literal["thresholds"]
    label: str
    value: DeThresholds


class EditableCohortName(WireModel):
    field: Literal["cohortName"]
    label: str
    value: CohortName
    ref: CohortRefId


Editable = Annotated[
    EditableScope | EditableMethod | EditableTopN | EditableThresholds | EditableCohortName, Field(discriminator="field")
]
EditField = Literal["scope", "method", "topN", "thresholds", "cohortName"]


class PlanView(WireModel):
    version: int = Field(ge=1)
    signature: Sha256Hex
    scope: Scope
    rows: list[PlanRow]
    assumptions: list[Assumption]
    writes: bool
    paraphrase: str
    # v1.2. The second scope a follow-up names after a cohort switch (E15), and the scope edit value
    # that switches to it: the chip sends {"field": "scope", "value": altScopeChoice}. Both or neither.
    alt_scope: Scope | None = None
    alt_scope_choice: ScopeChoice | None = None
    # v1.2. The settings the plan card can edit, each with its current value.
    editable: list[Editable] = Field(default_factory=list)

    @model_validator(mode="after")
    def _alt_scope_has_its_choice(self) -> "PlanView":
        if (self.alt_scope is None) != (self.alt_scope_choice is None):
            raise ValueError("altScope and altScopeChoice are both set or both null")
        return self


class StepOrigin(WireModel):
    """The plan version and step whose calls produced a Reused step's result (never another Reused step)."""

    plan_version: int = Field(ge=1)
    step_id: StepId


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
    # v1.2. Set exactly when the step is Reused; its calls are the record's calls of that version and step.
    reused_from: StepOrigin | None = None

    @model_validator(mode="after")
    def _reused_names_its_origin(self) -> "StepView":
        if (self.status == "Reused") != (self.reused_from is not None):
            raise ValueError(f"step {self.id}: reusedFrom is set exactly when the step is Reused")
        return self


FactValue = int | float | str


class Fact(WireModel):
    id: FactId
    label: str
    # A number, or a string for gene symbols and other names model text cites by placeholder.
    value: FactValue
    unit: str
    step_id: StepId
    row: int = Field(ge=1)
    tool: str
    json_pointer: Annotated[str, Field(pattern=r"^(/([^/~]|~[01])*)*$")]


class Segment(WireModel):
    text: str
    fact_id: Annotated[str | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)


class Sentence(WireModel):
    segments: list[Segment]
    # v1.2. Set when the verifier dropped this sentence; segments is then empty and the card shows a marker.
    dropped: Annotated[VerifierRule | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)
    # v1.2. The facts a model-written sentence (Interpretation, Hypotheses) cites, from the composer's
    # `cites` (V-REF); present on every kept model sentence, absent on Observed and dropped sentences.
    cites: Annotated[list[FactId] | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _dropped_has_no_text(self) -> "Sentence":
        if self.dropped is not None and (self.segments or self.cites is not None):
            raise ValueError("a dropped sentence carries no segments and no cites")
        if self.cites is not None and not self.cites:
            raise ValueError("cites lists at least one fact")
        return self


class NearestApp(WireModel):
    app_id: AppId
    title: str

    @classmethod
    def of(cls, app_id: AppId) -> "NearestApp":
        return cls(app_id=app_id, title=app_title(app_id))


# An "Open <app>" link under Hypotheses and next steps has the same shape.
OpenLink = NearestApp


class Limitation(WireModel):
    code: Code
    # Plain text, for TEXT_MESSAGE and for a limitation that names no tool-returned number.
    text: str
    unblock: str | None
    nearest_apps: list[NearestApp] = Field(default_factory=list)
    # v1.2. The same line with chips: present exactly when it names a tool-returned number, such as an
    # INELIGIBLE_GROUP's eligible counts or a TIES_AT_CUTOFF's tied genes. The card renders it instead of text.
    segments: Annotated[list[Segment] | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)


class FollowUp(WireModel):
    """A follow-up chip. `kind` is an analysis kind the compiler can run; the click asks `question`."""

    label: str = Field(min_length=1, max_length=60)
    question: str = Field(min_length=1, max_length=200)
    kind: AnalysisKind


# --- Canonical cohort definitions and defSha256 -----------------------------------------

ClauseOp = Literal["in", "!=", ">", ">=", "<", "<="]
ClauseValue = str | int | float
CASE_ID_FIELDS: Final = frozenset({"case_id", "cases.case_id"})
_LEGACY_CASE_KEY: Final = "cases.case_id"


class CaseIdsDefinition(WireModel):
    kind: Literal["case_ids"]
    case_ids: list[CaseId] = Field(max_length=MAX_CASE_IDS)


class FilterClause(WireModel):
    field: str = Field(min_length=1, max_length=120)
    op: ClauseOp
    values: list[ClauseValue] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_value_type(self) -> "FilterClause":
        kinds = {isinstance(v, str) for v in self.values}
        if len(kinds) > 1:
            raise ValueError(f"{self.field}: values are all strings or all numbers")
        if self.op != "in" and len(self.values) != 1:
            raise ValueError(f"{self.field} {self.op} takes exactly one value")
        return self


class FilterDefinition(WireModel):
    kind: Literal["filter"]
    clauses: list[FilterClause] = Field(max_length=20)


CanonicalDefinition = Annotated[CaseIdsDefinition | FilterDefinition, Field(discriminator="kind")]
CANONICAL_DEFINITION: Final[TypeAdapter[CanonicalDefinition]] = TypeAdapter(CanonicalDefinition)


def _sorted_values(values: list[ClauseValue]) -> list[ClauseValue]:
    unique: list[ClauseValue] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if all(isinstance(v, str) for v in unique):
        return sorted(unique, key=str)  # code point order
    return sorted(unique, key=float)


def normalize_definition(definition: CanonicalDefinition) -> CanonicalDefinition:
    """The canonical form both sides hash (contract "Cohort definitions and defSha256").

    Case ids are deduplicated and sorted by code point. A filter clause's values are deduplicated
    and sorted (numbers numerically, strings by code point), clauses are deduplicated and sorted
    by (field, op, canonical JSON of values), and a filter that is exactly one `in` clause on
    `case_id` or `cases.case_id` becomes a case-ID list.
    """
    if isinstance(definition, CaseIdsDefinition):
        return CaseIdsDefinition(kind="case_ids", case_ids=sorted(set(definition.case_ids)))
    clauses: dict[str, FilterClause] = {}
    for clause in definition.clauses:
        values = _sorted_values(list(clause.values))
        normal = FilterClause(field=clause.field, op=clause.op, values=values)
        clauses.setdefault(canonical_json(normal.model_dump(mode="json")), normal)
    ordered = sorted(clauses.values(), key=lambda c: (c.field, c.op, canonical_json(list(c.values))))
    if len(ordered) == 1 and ordered[0].field in CASE_ID_FIELDS and ordered[0].op == "in":
        ids = [str(v) for v in ordered[0].values]
        return CaseIdsDefinition(kind="case_ids", case_ids=sorted(set(ids)))
    return FilterDefinition(kind="filter", clauses=ordered)


def canonical_definition(definition: CanonicalDefinition) -> str:
    """Canonical JSON of `{"schema": "orion.cohort-def/v1", ...normalized definition}`."""
    normal = normalize_definition(definition)
    return canonical_json({"schema": COHORT_DEF_SCHEMA, **normal.model_dump(mode="json")})


def def_sha256(definition: CanonicalDefinition) -> str:
    """defSha256: sha256 (lowercase hex) of the UTF-8 canonical definition."""
    return sha256_hex(canonical_definition(definition))


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


class Volcano(WireModel):
    fold_change_cutoff: Annotated[float | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)
    p_value: Annotated[float | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)
    p_value_type: Annotated[str | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)
    rank_by: Annotated[str | None, _NOT_NULL] = Field(default=None, exclude_if=lambda value: value is None)


class DeDirectPinned(WireModel):
    kind: Literal["deDirect"]
    groups: list[SampleGroup] = Field(min_length=2, max_length=2)
    method: DeMethod
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


def canonical_pinned(pinned: Pinned) -> str:
    """The contract's canonical JSON of `{"schema": "orion.viz-pinned/v1", ...pinned}`.

    Normalization sorts each deDirect group's sampleIds; every other array keeps its order.
    Raises PinnedNumberError for a number outside the contract's range.
    """
    if isinstance(pinned, DeDirectPinned):
        groups = [group.model_copy(update={"sample_ids": sorted(group.sample_ids)}) for group in pinned.groups]
        pinned = pinned.model_copy(update={"groups": groups})
    return canonical_json({"schema": VIZ_PINNED_SCHEMA, **pinned.model_dump(mode="json")})


def pinned_hash(pinned: Pinned) -> str:
    """sha256 (lowercase hex) of the UTF-8 canonical JSON of the pinned inputs."""
    return sha256_hex(canonical_pinned(pinned))


# --- requestPinnedHash: the pinned form of the cited step's actual request -----------------


def _minus_log10(cutoff: float) -> float:
    # The viewer's -log10 p cutoff, two decimals (0.05 -> 1.3), rounded half away from zero.
    return float(_decimal(-math.log10(cutoff)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive_request_pinned(launcher: PinnedKind, request: Mapping[str, JsonValue], observed: JsonValue = None) -> Pinned:
    """The pinned inputs the cited step's recorded request (`calls[].request`) implies, per launcher.

    survival  (survival_compare): {"scope", "term", "split": {"field", "overlay"}, "groups"} gives
              filter0 = scope (null for all cases), term = term, overlay = split.overlay (the split
              field's ProteinPaint twin).
    oncomatrix (top_mutated_genes): {"filter0", ...} with the output's "genes" ([{"gene", ...}],
              display order) gives filter0 = filter0, genes = the output's gene symbols in order.
    deDirect  (de_run): {"groups": [{"name", "sampleIds", ...}] x2, "method", "thresholds"?} gives
              groups (name and sampleIds), method, and volcano {foldChangeCutoff, pValue =
              -log10(pValueCutoff) to 2 decimals, pValueType} when thresholds are present.
    deInput   (de_run on saved cohorts): {"groups": [{"name", "cohortId", "filter0", ...}] x2}
              gives groups (name, cohortId and filter0).

    `fixtures/request-pinned-vectors.json` holds one vector per launcher.
    """
    if launcher == "survival":
        split = request["split"]
        if not isinstance(split, dict):
            raise ValueError("survival_compare's request names its split")
        return SurvivalPinned.model_validate(
            {"kind": "survival", "filter0": request["scope"], "term": request["term"], "overlay": split["overlay"]}
        )
    if launcher == "oncomatrix":
        genes = observed.get("genes") if isinstance(observed, dict) else None
        if not isinstance(genes, list):
            raise ValueError("top_mutated_genes' output lists its genes")
        symbols = [g["gene"] for g in genes if isinstance(g, dict)]
        return OncomatrixPinned.model_validate({"kind": "oncomatrix", "filter0": request["filter0"], "genes": symbols})
    groups = request["groups"]
    if not isinstance(groups, list) or not all(isinstance(g, dict) for g in groups):
        raise ValueError("de_run's request lists its groups")
    if launcher == "deDirect":
        sample_groups: list[JsonValue] = [{"name": g["name"], "sampleIds": g["sampleIds"]} for g in groups if isinstance(g, dict)]
        pinned: dict[str, JsonValue] = {"kind": "deDirect", "groups": sample_groups, "method": request["method"]}
        thresholds = request.get("thresholds")
        if isinstance(thresholds, dict):
            cutoff = thresholds["pValueCutoff"]
            if not isinstance(cutoff, (int, float)):
                raise ValueError("pValueCutoff is a number")
            pinned["volcano"] = {
                "foldChangeCutoff": thresholds["foldChangeCutoff"],
                "pValue": _minus_log10(float(cutoff)),
                "pValueType": thresholds["pValueType"],
            }
        return DeDirectPinned.model_validate(pinned)
    cohort_groups: list[JsonValue] = [
        {"name": g["name"], "cohortId": g["cohortId"], "filter0": g["filter0"]} for g in groups if isinstance(g, dict)
    ]
    return DeInputPinned.model_validate({"kind": "deInput", "groups": cohort_groups})


# --- The fixture fingerprint (s1; RunView.environment, mode H) --------------------------------


def fingerprint_document(
    status: Mapping[str, JsonValue],
    version: Mapping[str, JsonValue],
    healthcheck: Mapping[str, JsonValue],
    ranking: Sequence[Mapping[str, JsonValue]],
) -> dict[str, JsonValue]:
    """The canonical document behind fingerprintSha256, from the four sources fixture_fingerprint reads.

    `status` is GET /sandbox/status, `version` GET /analysis/v0/_version, `healthcheck`
    GET /protein-paint/healthcheck, `ranking` the all-case topMutatedGenes ranking as
    [{"gene", "cases"}] in returned order. Volatile fields (csrf, launch dates, init status)
    are left out, so a restart does not change the fingerprint.
    """
    info = healthcheck.get("versionInfo")
    if not isinstance(info, dict):
        raise ValueError("the ProteinPaint healthcheck carries versionInfo")
    return {
        "schema": FINGERPRINT_SCHEMA,
        "sandbox": {"status": status["status"], "synthetic": status["synthetic"], "cases": status["cases"]},
        "analysis": {"version": version["version"]},
        "proteinpaint": {"pkgver": info["pkgver"], "codedate": info["codedate"]},
        "ranking": [{"gene": row["gene"], "cases": row["cases"]} for row in ranking],
    }


def fingerprint_sha256(document: Mapping[str, JsonValue]) -> str:
    """fingerprintSha256: sha256 of the canonical JSON (portable numbers) of the fingerprint document."""
    return sha256_hex(canonical_json(dict(document)))


class FrozenTable(WireModel):
    """The small cited output a viz card shows once it is no longer one of the two live cards."""

    columns: list[str] = Field(min_length=1, max_length=8)
    rows: list[list[Segment]] = Field(max_length=FROZEN_ROWS_CAP)
    # v1.2: segments, so a tool-returned number in the note carries its chip.
    note: list[Segment] | None

    @model_validator(mode="after")
    def _rows_fit_columns(self) -> "FrozenTable":
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("every frozen row has one cell per column")
        return self


class VizSpec(WireModel):
    viz_id: VizId
    app_id: AppId
    launcher: PinnedKind
    cites_steps: list[StepId] = Field(min_length=1)
    # Null only in a snapshot that had to drop it to fit 32 KB; the view route and the record always carry it.
    pinned: Pinned | None
    pinned_hash: Sha256Hex
    # v1.2
    title: str = Field(min_length=1, max_length=200)
    # One line under the chart, such as the DE sort caption or the J1 rounding footnote.
    caption: str | None
    # pinnedHash of the pinned form derived from the cited step's actual request. The parity badge
    # shows a match only when the browser's recomputed pinnedHash equals this and pinnedHash.
    request_pinned_hash: Sha256Hex
    frozen: FrozenTable | None
    # The run cohorts (CohortView refs) the card is pinned to; hand-off opens on them.
    cohort_refs: list[CohortRefId]

    @model_validator(mode="after")
    def _pinned_is_consistent(self) -> "VizSpec":
        if self.pinned is None:
            return self
        if self.launcher != self.pinned.kind:
            raise ValueError(f"launcher {self.launcher!r} must equal pinned.kind {self.pinned.kind!r}")
        if self.pinned_hash != pinned_hash(self.pinned):
            raise ValueError("pinnedHash does not match the pinned inputs")
        return self


# --- Run cohorts and pending pauses -------------------------------------------------------

CohortRole = Literal["scope", "planned"]
CohortSource = Literal["planned", "all_cases", "active_cohort", "saved_cohort", "thread_cohort"]
CohortSave = Literal["none", "proposed", "saved", "reused", "skipped"]
SCOPE_COHORT_REF: Final = BASE_REF


class CohortView(WireModel):
    """One cohort of the run: its scope (ref `base`) or a cohort the plan builds."""

    ref: CohortRefId
    role: CohortRole
    source: CohortSource
    name: str
    definition_text: str
    # Null until the definition is known: a case-ID cohort is known once its step finishes.
    def_sha256: Sha256Hex | None
    case_count: int | None = Field(ge=0)
    save: CohortSave
    # The platform cohort the card hands off to. For the scope (`base`): the active cohort's id
    # (null when it is unsaved), the matched saved cohort's id, or the ledger cohort's id; null for
    # all cases. For a planned cohort: the read-back id once saved or reused.
    cohort_id: PlatformCohortId | None
    saved_name: str | None
    # v1.2. The fact behind caseCount once a step sized the cohort; null while the count is the
    # browser's (cohortContext) or unknown. The scope line's count cites the `base` cohort's fact.
    case_count_fact_id: FactId | None = None

    @model_validator(mode="after")
    def _count_fact_needs_a_count(self) -> "CohortView":
        if self.case_count_fact_id is not None and self.case_count is None:
            raise ValueError(f"cohort {self.ref}: caseCountFactId needs a caseCount")
        return self


class CohortReuse(WireModel):
    cohort_id: PlatformCohortId
    name: str


class PendingCohort(WireModel):
    ref: CohortRefId
    name: CohortName
    definition_text: str
    # Null only in a snapshot that had to drop it to fit 32 KB; TOOL_CALL_ARGS and the record always carry it.
    definition: CanonicalDefinition | None
    def_sha256: Sha256Hex
    case_count: int = Field(ge=0)
    eligible_count: int | None = Field(ge=0)
    reuse: CohortReuse | None
    # v1.2. The facts behind the two counts the confirmation card shows (FR-16): every count on the
    # card is a chip. Null only when no step produced the number.
    case_count_fact_id: FactId | None = None
    eligible_count_fact_id: FactId | None = None

    @model_validator(mode="after")
    def _hash_matches(self) -> "PendingCohort":
        if self.definition is not None and def_sha256(self.definition) != self.def_sha256:
            raise ValueError(f"cohort {self.ref}: defSha256 does not match its definition")
        if self.eligible_count_fact_id is not None and self.eligible_count is None:
            raise ValueError(f"cohort {self.ref}: eligibleCountFactId needs an eligibleCount")
        return self


ClarifySlot = Literal["split_term", "cohort_field", "gene", "scope"]


class ClarifyOption(WireModel):
    option_id: OptionId
    # The vocabulary field the option names, or null for an option that is not a field (a gene, a scope).
    field: str | None
    label: str
    preview: str


class ClarifyPending(WireModel):
    kind: Literal["clarify"]
    slot: ClarifySlot
    question: str
    options: list[ClarifyOption] = Field(min_length=2, max_length=5)


class ConsentPending(WireModel):
    kind: Literal["consent"]
    named: NearestApp
    proposed: NearestApp
    reason: str


class ConfirmPending(WireModel):
    kind: Literal["confirm"]
    nonce: HexId
    plan_signature: Sha256Hex
    plan_version: int = Field(ge=1)
    step_id: StepId
    cohorts: list[PendingCohort] = Field(min_length=1, max_length=6)


class SaveCohortsArgs(ConfirmPending):
    """The `save_cohorts` TOOL_CALL_ARGS JSON: the confirm pending payload with every definition."""

    @model_validator(mode="after")
    def _definitions_present(self) -> "SaveCohortsArgs":
        if any(c.definition is None for c in self.cohorts):
            raise ValueError("save_cohorts args carry every cohort definition")
        return self


Pending = Annotated[ClarifyPending | ConsentPending | ConfirmPending, Field(discriminator="kind")]


class SupersededSummary(WireModel):
    plan_version: int = Field(ge=1)
    at: IsoTime
    # The run status when the edit replaced that version, and whether it had an answer to mark.
    status: RunStatus
    had_answer: bool


class ViewEnvironment(WireModel):
    """Where the run ran, for the viz card: in mode H it compares the local stack's fingerprint with
    `fingerprintSha256` (the s1 fixture fingerprint, null until s1 ran) and freezes on a mismatch."""

    mode: Literal["L", "A", "H"]
    fingerprint_sha256: Sha256Hex | None


PENDING_KIND: Final[dict[RunStatus, str]] = {
    "Clarifying": "clarify",
    "AwaitingConsent": "consent",
    "AwaitingConfirmation": "confirm",
}


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
    interpretation: list[Sentence] | None
    hypotheses: list[Sentence] | None
    withheld_reason: str | None
    viz_specs: list[VizSpec]
    # v1.2. Defaults exist for construction only: the wire always carries every field.
    pending: Pending | None = None
    cohorts: list[CohortView] = Field(default_factory=list)
    follow_ups: list[FollowUp] = Field(default_factory=list)
    open_links: list[NearestApp] = Field(default_factory=list)
    # Earlier plan versions an edit replaced; the answer card marks a superseded answer.
    superseded: list[SupersededSummary] = Field(default_factory=list)
    notice: str = SYNTHETIC_NOTICE
    environment: ViewEnvironment = Field(default_factory=lambda: ViewEnvironment(mode="L", fingerprint_sha256=None))

    @model_validator(mode="after")
    def _parts_are_consistent(self) -> "RunView":
        expected = PENDING_KIND.get(self.status)
        actual = None if self.pending is None else self.pending.kind
        if expected != actual:
            raise ValueError(f"status {self.status} carries pending {expected!r}, not {actual!r}")
        refs = {cohort.ref for cohort in self.cohorts}
        for spec in self.viz_specs:
            if not set(spec.cohort_refs) <= refs:
                raise ValueError(f"viz {spec.viz_id} is pinned to cohorts the view does not list")
        if self.plan is not None and any(s.plan_version >= self.plan.version for s in self.superseded):
            raise ValueError("a superseded version is older than the current plan")
        return self


# --- Frames ------------------------------------------------------------------------------


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
    # Equals the pending confirm's nonce.
    tool_call_id: HexId
    tool_call_name: Literal["save_cohorts"]


class ToolCallArgsFrame(WireModel):
    type: Literal["TOOL_CALL_ARGS"]
    tool_call_id: HexId
    delta: str = Field(max_length=TOOL_CALL_ARGS_CHUNK)


class ToolCallEndFrame(WireModel):
    type: Literal["TOOL_CALL_END"]
    tool_call_id: HexId


class TextMessageStartFrame(WireModel):
    type: Literal["TEXT_MESSAGE_START"]
    message_id: str
    role: Literal["assistant"]


class TextMessageContentFrame(WireModel):
    type: Literal["TEXT_MESSAGE_CONTENT"]
    message_id: str
    delta: str = Field(max_length=TEXT_MESSAGE_CHUNK)


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


def save_cohorts_args_chunks(args: SaveCohortsArgs) -> list[str]:
    """The TOOL_CALL_ARGS deltas for `args`: its compact JSON split into chunks of at most 8,192 characters."""
    text = args.model_dump_json()
    return [text[i : i + TOOL_CALL_ARGS_CHUNK] for i in range(0, len(text), TOOL_CALL_ARGS_CHUNK)] or [""]


def text_message_chunks(markdown: str) -> list[str]:
    """The TEXT_MESSAGE_CONTENT deltas for the answer markdown: chunks of at most 8,192 characters."""
    return [markdown[i : i + TEXT_MESSAGE_CHUNK] for i in range(0, len(markdown), TEXT_MESSAGE_CHUNK)] or [""]


# --- Request: RunAgentInput.forwardedProps.orion ---------------------------------------


class FilterSet(WireModel):
    """A Gen3 FilterSet as the browser's cohort store holds it."""

    mode: Literal["and", "or"]
    root: dict[str, dict[str, JsonValue]]


class _Unreadable(Exception):
    pass


def _filter_operand(value: JsonValue) -> ClauseValue:
    # ProteinPaint's "Create cohort" stores case ids as {"cases.case_id": id}.
    if isinstance(value, dict) and len(value) == 1 and isinstance(value.get(_LEGACY_CASE_KEY), str):
        value = value[_LEGACY_CASE_KEY]
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _Unreadable()
    return value


_SINGLE_OPS: Final[dict[str, ClauseOp]] = {"=": "in", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}


def _read_filter_operation(key: str, operation: dict[str, JsonValue], clauses: list[FilterClause]) -> None:
    named = operation.get("field")
    field_id = named if isinstance(named, str) else key
    operator = operation.get("operator")
    operands = operation.get("operands")
    if operator == "and" and isinstance(operands, list) and operands:
        for part in operands:
            if not isinstance(part, dict):
                raise _Unreadable()
            _read_filter_operation(field_id, part, clauses)
        return
    if operator in ("in", "includes") and isinstance(operands, list) and operands:
        clauses.append(FilterClause(field=field_id, op="in", values=[_filter_operand(v) for v in operands]))
        return
    if isinstance(operator, str) and operator in _SINGLE_OPS and "operand" in operation:
        clauses.append(FilterClause(field=field_id, op=_SINGLE_OPS[operator], values=[_filter_operand(operation["operand"])]))
        return
    raise _Unreadable()


def definition_from_filter_set(filters: FilterSet) -> CanonicalDefinition | None:
    """A saved cohort's canonical definition, or None when ORION cannot read its filters (it never matches)."""
    if filters.mode == "or" and len(filters.root) > 1:
        return None
    clauses: list[FilterClause] = []
    try:
        for key, operation in filters.root.items():
            _read_filter_operation(key, operation, clauses)
        if len(clauses) > 20:
            return None
        normal = normalize_definition(FilterDefinition(kind="filter", clauses=clauses))
        # A number outside the canonical range (PinnedNumberError) has no portable hash: unreadable.
        canonical_definition(normal)
    except (_Unreadable, ValueError):
        return None
    return normal


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


class VizReport(WireModel):
    """Whether a viz card mounted. It rides on the next turn's `vizReports`, for any run of the thread."""

    run_id: HexId
    viz_id: VizId
    mounted: bool
    error: VizError
    # True when the browser's recomputed pinnedHash equals both pinnedHash and requestPinnedHash; null if not checked.
    pinned_hash_verified: bool | None


class _Action(WireModel):
    viz_reports: list[VizReport] = Field(default_factory=list, max_length=8)


class AskAction(_Action):
    action: Literal["ask"]
    question: str = Field(min_length=1, max_length=4000)
    cohort_context: CohortContext
    # The paused run the researcher chose to cancel ("Cancel the waiting run and send this").
    cancel_waiting: HexId | None = None


class WarmupAction(_Action):
    action: Literal["warmup"]


class CancelAction(_Action):
    action: Literal["cancel"]


class ClarifyAnswerAction(_Action):
    action: Literal["clarify_answer"]
    option_id: OptionId


class ConsentAction(_Action):
    action: Literal["consent"]
    accept: bool


class SavedReadBack(WireModel):
    """One pending cohort after Create, read back from the cohort store."""

    ref: CohortRefId
    cohort_id: PlatformCohortId
    name: CohortName
    # defSha256 the browser computed from the saved cohort's stored filters, as read back; null when
    # those filters cannot be read ("Reading a saved cohort's FilterSet"). Null never equals the
    # pending cohort's hash, so the write step fails WRITE_VERIFY_FAILED (contract "confirm create").
    def_sha256: Sha256Hex | None
    # True when an identical saved cohort was reused instead of created.
    reused: bool


class ConfirmAction(_Action):
    action: Literal["confirm"]
    nonce: HexId
    decision: Literal["create", "dont_save"]
    saved: list[SavedReadBack] = Field(max_length=6)
    make_active: CohortRefId | None = None

    @model_validator(mode="after")
    def _decision_is_consistent(self) -> "ConfirmAction":
        refs = [s.ref for s in self.saved]
        if len(refs) != len(set(refs)):
            raise ValueError("each ref is read back once")
        if self.decision == "dont_save" and (self.saved or self.make_active is not None):
            raise ValueError("dont_save carries no saved cohorts and no makeActive")
        if self.decision == "create" and not self.saved:
            raise ValueError("create carries the read-back of every pending cohort")
        if self.make_active is not None and self.make_active not in refs:
            raise ValueError("makeActive names a saved ref")
        return self


class EditScopeAction(_Action):
    action: Literal["edit_plan"]
    field: Literal["scope"]
    value: ScopeChoice
    # The cohort context at the click; the scope resolves against it.
    cohort_context: CohortContext


class EditMethodAction(_Action):
    action: Literal["edit_plan"]
    field: Literal["method"]
    value: DeMethod


class EditTopNAction(_Action):
    action: Literal["edit_plan"]
    field: Literal["topN"]
    value: int = Field(ge=1, le=100)


class EditThresholdsAction(_Action):
    action: Literal["edit_plan"]
    field: Literal["thresholds"]
    value: DeThresholds


class EditCohortNameAction(_Action):
    action: Literal["edit_plan"]
    field: Literal["cohortName"]
    value: CohortName
    ref: CohortRefId


EditPlanAction = Annotated[
    EditScopeAction | EditMethodAction | EditTopNAction | EditThresholdsAction | EditCohortNameAction,
    Field(discriminator="field"),
]


class RetryStepAction(_Action):
    action: Literal["retry_step"]
    step_id: StepId


class VizReportAction(_Action):
    """A standalone report, for a thread with no turn in flight; normally reports ride on `vizReports`."""

    action: Literal["viz_report"]
    viz_id: VizId
    mounted: bool
    error: VizError
    pinned_hash_verified: bool | None


OrionAction = Annotated[
    AskAction
    | WarmupAction
    | CancelAction
    | ClarifyAnswerAction
    | ConsentAction
    | ConfirmAction
    | EditPlanAction
    | RetryStepAction
    | VizReportAction,
    Field(discriminator="action"),
]
ORION_ACTION: Final[TypeAdapter[OrionAction]] = TypeAdapter(OrionAction)


class RunAgentInputModel(WireModel):
    """The POST body: all seven RunAgentInput fields, with ORION's action in forwardedProps.orion."""

    thread_id: HexId
    run_id: HexId
    state: dict[str, JsonValue]
    messages: list[dict[str, JsonValue]]
    tools: list[JsonValue]
    context: list[JsonValue]
    forwarded_props: dict[str, JsonValue]

    def orion(self) -> OrionAction:
        return ORION_ACTION.validate_python(self.forwarded_props.get("orion"))


# --- Run record parts (orion.run-record/v1, served by the record route) -------------------

ToolStatus = Literal["ok", "empty", "error", "unavailable"]
Transport = Literal["in_process", "gateway", "client"]
ModelRole = Literal["planner", "composer"]


class Provenance(WireModel):
    """One HTTP request a tool made, as the record keeps it (A2 class diagram)."""

    method: Literal["GET", "POST"]
    path: str
    request_sha256: Sha256Hex
    http_status: int | None
    response_sha256: Sha256Hex | None
    elapsed_ms: int = Field(ge=0)


class CallRecord(WireModel):
    """One attempt of one counted call: a server tool, a pure function or a client write.

    A Stop or an edit that aborts an attempt records it too: status `error`, `error` starting
    "CANCELLED", `httpStatus` and `responseSha256` null (contract "cancel").
    """

    call_id: CallId
    plan_version: int = Field(ge=1)
    step_id: StepId
    tool: str
    transport: Transport
    attempt: int = Field(ge=1, le=2)
    started_at: IsoTime
    request: JsonValue
    # sha256 of the canonical JSON of `request`.
    request_sha256: Sha256Hex
    # The last HTTP status the call saw; null for pure functions and client writes.
    http_status: int | None
    # sha256 of the canonical JSON of `observed`; null when the call returned nothing.
    response_sha256: Sha256Hex | None
    elapsed_ms: int = Field(ge=0)
    status: ToolStatus
    # The full tool output. Snapshots never carry it; the Evidence Inspector reads it here.
    observed: JsonValue
    warnings: list[str] = Field(default_factory=list)
    limitation: Code | None
    error: str | None = None
    provenance: list[Provenance] = Field(default_factory=list)


class ModelCallRecord(WireModel):
    call_id: ModelCallId
    role: ModelRole
    # The model that served, such as us.anthropic.claude-opus-5-5, or its refusal fallback.
    model: str
    fallback: bool
    effort: Literal["low", "medium", "high"]
    attempt: int = Field(ge=1, le=4)
    started_at: IsoTime
    elapsed_ms: int = Field(ge=0)
    # `cancelled`: a Stop or an edit aborted the call (Planning or Answering).
    outcome: Literal["ok", "refused", "invalid", "error", "cancelled"]
    stop_reason: str | None
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    cache_read_tokens: int | None = Field(ge=0)
    cache_write_tokens: int | None = Field(ge=0)


class FactRecord(Fact):
    """A fact as the record keeps it: the view's fields plus where the value came from."""

    plan_version: int = Field(ge=1)
    # The attempt whose result the value came from (for a Reused step, the original version's call).
    call_id: CallId
    request_sha256: Sha256Hex
    response_sha256: Sha256Hex
    # The engine that produced the value, such as gen3-analysis@ffa4069; null for ORION's own functions.
    engine: str | None

    def to_view(self) -> Fact:
        return Fact.model_validate(self.model_dump(include=set(Fact.model_fields)))


class CohortRef(CohortView):
    """A run cohort as the record keeps it, with its canonical definition."""

    definition: CanonicalDefinition | None

    def to_view(self) -> CohortView:
        return CohortView.model_validate(self.model_dump(include=set(CohortView.model_fields)))


class AnswerRecord(WireModel):
    observed: list[Sentence] = Field(default_factory=list)
    limitations: list[Limitation] = Field(default_factory=list)
    interpretation: list[Sentence] | None = None
    hypotheses: list[Sentence] | None = None
    withheld_reason: str | None = None
    viz_specs: list[VizSpec] = Field(default_factory=list)
    follow_ups: list[FollowUp] = Field(default_factory=list)
    open_links: list[NearestApp] = Field(default_factory=list)
    notice: str = SYNTHETIC_NOTICE

    @model_validator(mode="after")
    def _record_is_untrimmed(self) -> "AnswerRecord":
        if any(spec.pinned is None for spec in self.viz_specs):
            raise ValueError("the record keeps every viz spec's pinned inputs")
        return self


class SupersededVersion(WireModel):
    """What an edit replaced: the old version's steps, facts and answer, kept for the record."""

    plan_version: int = Field(ge=1)
    at: IsoTime
    status: RunStatus
    steps: list[StepView]
    facts: list[FactRecord]
    cohorts: list[CohortRef]
    answer: AnswerRecord

    def summary(self) -> SupersededSummary:
        return SupersededSummary(plan_version=self.plan_version, at=self.at, status=self.status, had_answer=bool(self.answer.observed))


class PlanHistoryEntry(WireModel):
    """Why each plan version exists. Joins planVersions by `version`."""

    version: int = Field(ge=1)
    cause: Literal["ask", "clarify_answer", "edit_plan"]
    at: IsoTime
    # The IR this version compiled: the planner's Plan, or the patched IR of an edit.
    ir: IrPlan | None
    edit: EditPlanAction | None = None


class ConfirmationRecord(WireModel):
    """One pending nonce's outcome. `voided`: an edit or a cancel replaced the pause before any
    decision (decision null, saved empty). A repeated confirm replays the recorded outcome."""

    nonce: HexId
    decision: Literal["create", "dont_save"] | None
    saved: list[SavedReadBack]
    make_active: CohortRefId | None
    at: IsoTime
    outcome: Literal["accepted", "verify_failed", "voided"]

    @model_validator(mode="after")
    def _voided_has_no_decision(self) -> "ConfirmationRecord":
        if (self.decision is None) != (self.outcome == "voided"):
            raise ValueError("a voided nonce carries no decision, and only a voided one")
        return self


class VizReportRecord(WireModel):
    viz_id: VizId
    mounted: bool
    error: str | None
    pinned_hash_verified: bool | None
    at: IsoTime


class ActionLogEntry(WireModel):
    at: IsoTime
    action: ActionName
    outcome: Literal["accepted", "repeat", "rejected"]
    code: Code | None


class LedgerRefUse(WireModel):
    run_id: HexId
    ref: CohortRefId
    name: str


class LedgerUse(WireModel):
    """What this run took from the session ledger: the run it follows and the thread cohorts it reused."""

    follows: HexId | None = None
    refs: list[LedgerRefUse] = Field(default_factory=list)


# --- BFF route error body ------------------------------------------------------------------

BffErrorCode = Literal[
    "INVALID_INPUT",
    "UNAUTHENTICATED",
    "RUN_NOT_FOUND",
    "SESSION_BUSY",
    "UPSTREAM_ERROR",
    "UPSTREAM_UNAVAILABLE",
    "UPSTREAM_TIMEOUT",
]
# What the browser does with each BFF error (contract "Client handling of BFF errors"); the fork's
# agui/types.ts BFF_ERROR_HANDLING holds the same map.
BffErrorHandling = Literal["reject", "pollView", "checkView", "notStored"]
BFF_ERROR_HANDLING: Final[dict[BffErrorCode, BffErrorHandling]] = {
    "INVALID_INPUT": "reject",
    "UNAUTHENTICATED": "reject",
    "SESSION_BUSY": "pollView",
    "UPSTREAM_ERROR": "checkView",
    "UPSTREAM_TIMEOUT": "checkView",
    "UPSTREAM_UNAVAILABLE": "reject",
    "RUN_NOT_FOUND": "notStored",
}
# The HTTP status each BFF error answers with (contract "BFF routes", Errors).
BFF_ERROR_STATUS: Final[dict[BffErrorCode, int]] = {
    "INVALID_INPUT": 400,
    "UNAUTHENTICATED": 401,
    "RUN_NOT_FOUND": 404,
    "SESSION_BUSY": 409,
    "UPSTREAM_ERROR": 502,
    "UPSTREAM_UNAVAILABLE": 503,
    "UPSTREAM_TIMEOUT": 504,
}


class BffErrorDetail(WireModel):
    code: BffErrorCode
    message: str


class BffError(WireModel):
    """The non-SSE JSON body every BFF route returns on an error."""

    error: BffErrorDetail
