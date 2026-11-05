"""The executor's four pure functions (TOOLS.md section 3; signatures in base.py), each versioned.

`set_operation`, `check_subset`, `rank_top_n` and `derive_km_median` are deterministic functions
of their arguments. `run_function` runs one as a step: it reads the referenced steps' outputs
through the executor's reader (`schemas.StepValueReader`, which raises LookupError for a pointer
that does not resolve), applies the step's post-guards and returns the ToolResult envelope, with no
provenance and no retry. `call_function` wraps that as the ToolCallOutcome the executor records
(transport `in_process`, httpStatus null). A function that raises fails its step: status `error`,
error code INTERNAL_ERROR, so the CallRecord's error starts INTERNAL_ERROR (records.call_records).
"""

import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, JsonValue, ValidationError

from ..events import iso_utc, utc_now
from ..models import Code, call_id, canonical_json, sha256_hex
from .base import (
    MAX_CASE_IDS,
    Attempt,
    CheckSubsetFn,
    DeriveKmMedianFn,
    ExecutorFunctionName,
    Guard,
    KmCurve,
    KmMedian,
    KmMedianResult,
    KmPoint,
    RankedRow,
    RankResult,
    RankTopNFn,
    SetOp,
    SetOperationFn,
    SetOperationResult,
    SubsetResult,
    ToolCallOutcome,
    ToolContext,
    ToolError,
    ToolResult,
)
from .schemas import (
    RANK_SOURCES,
    CheckSubsetInput,
    DeriveKmMedianInput,
    RankTopNInput,
    SetOperationInput,
    StepValueReader,
    input_json_schema,
    tool_spec,
)

SET_OPERATION_VERSION: Final = tool_spec("set_operation").version
CHECK_SUBSET_VERSION: Final = tool_spec("check_subset").version
RANK_TOP_N_VERSION: Final = tool_spec("rank_top_n").version
DERIVE_KM_MEDIAN_VERSION: Final = tool_spec("derive_km_median").version
OUTSIDE_SAMPLE: Final = 10
KM_UNIT: Final = "days"


class FunctionError(ValueError):
    """A function's input it cannot compute on (a missing field, a non-numeric value, too many ids)."""


# --- The pure functions (base.py signatures) -------------------------------------------------------


def set_operation(op: SetOp, a: Sequence[str], b: Sequence[str] = ()) -> SetOperationResult:
    """difference (a minus b), intersection, union, or size (the count of a). Ids sorted, unique."""
    left, right = set(a), set(b)
    if op == "size":
        if b:
            raise FunctionError("size takes one set")
        return SetOperationResult(version=SET_OPERATION_VERSION, op=op, a_count=len(left), b_count=None, count=len(left), case_ids=[])
    if op == "difference":
        out = left - right
    elif op == "intersection":
        out = left & right
    else:
        out = left | right
    if len(out) > MAX_CASE_IDS:
        raise FunctionError(f"the {op} has {len(out)} case IDs, more than {MAX_CASE_IDS}")
    return SetOperationResult(
        version=SET_OPERATION_VERSION, op=op, a_count=len(left), b_count=len(right), count=len(out), case_ids=sorted(out)
    )


def check_subset(inner: Sequence[str], outer: Sequence[str]) -> SubsetResult:
    """Whether every inner id is in outer; the first ids outside, sorted, for the limitation."""
    inside, containing = set(inner), set(outer)
    outside = sorted(inside - containing)
    return SubsetResult(
        version=CHECK_SUBSET_VERSION, inner_count=len(inside), outer_count=len(containing), is_subset=not outside,
        outside_count=len(outside), outside_sample=outside[:OUTSIDE_SAMPLE],
    )


def _rank_value(row: Mapping[str, JsonValue], rank_by: str) -> float:
    absolute = rank_by.startswith("abs(") and rank_by.endswith(")")
    name = rank_by[4:-1] if absolute else rank_by
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise FunctionError(f"a row's {name} is not a finite number")
    return abs(float(value)) if absolute else float(value)


def rank_top_n(
    rows: Sequence[Mapping[str, JsonValue]], *, key: str, rank_by: str, n: int, ascending: bool, tie_break: str
) -> RankResult:
    """The top n rows by `rank_by` (a row field or abs(<field>)), plus every row tied with the n-th.

    Rows sort by value (ascending or descending), then by `tie_break` in code point order, so the
    order is deterministic. Tied values share a rank (competition ranking: 1, 2, 2, 4).
    `ties_at_cutoff` lists the keys kept beyond the n-th because they tie with it.
    """
    if n < 1:
        raise FunctionError("n is at least 1")
    scored: list[tuple[float, str, str, Mapping[str, JsonValue]]] = []
    for row in rows:
        identity, order = row.get(key), row.get(tie_break)
        if not isinstance(identity, str) or not isinstance(order, str):
            raise FunctionError(f"every row names its {key} and {tie_break} as text")
        scored.append((_rank_value(row, rank_by), order, identity, row))
    scored.sort(key=lambda item: (item[0] if ascending else -item[0], item[1]))
    # Sorted by value, rows tied with the n-th follow it directly: keep the whole run of them.
    end = min(n, len(scored))
    while 0 < end < len(scored) and scored[end][0] == scored[end - 1][0]:
        end += 1
    ranked: list[RankedRow] = []
    for position, (value, _, identity, row) in enumerate(scored[:end]):
        tied = position > 0 and value == scored[position - 1][0]
        rank = ranked[-1].rank if tied else position + 1
        ranked.append(RankedRow(rank=rank, key=identity, value=value, row=dict(row)))
    ties = [row.key for row in ranked[n:]]
    return RankResult(
        version=RANK_TOP_N_VERSION, rank_by=rank_by, ascending=ascending, n_requested=n, rows=ranked, count=len(ranked),
        ties_at_cutoff=ties, total_rows=len(scored),
    )


def passes_thresholds(row: Mapping[str, JsonValue], thresholds: Mapping[str, JsonValue]) -> bool:
    """A DE row passes when its p (adjusted or original, by `pValueType`) is below `pValueCutoff` and its
    |fold_change| above `foldChangeCutoff` (the thresholds' JSON form). A row without the numbers raises."""
    p_field = "adjusted_p_value" if thresholds.get("pValueType") == "adjusted" else "original_p_value"
    cutoff, fold_cutoff = thresholds.get("pValueCutoff"), thresholds.get("foldChangeCutoff")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (cutoff, fold_cutoff)):
        raise FunctionError("the thresholds need a numeric pValueCutoff and foldChangeCutoff")
    assert isinstance(cutoff, (int, float)) and isinstance(fold_cutoff, (int, float))
    return _rank_value(row, p_field) < cutoff and _rank_value(row, "abs(fold_change)") > fold_cutoff


def derive_km_median(curves: Sequence[KmCurve], *, unit: str) -> KmMedianResult:
    """Per curve, the first time whose survival is at most 0.5; null (not reached) when none is."""
    groups: list[KmMedian] = []
    for curve in curves:
        median = next((p.time for p in sorted(curve.points, key=lambda p: p.time) if p.survival <= 0.5), None)
        groups.append(KmMedian(label=curve.label, median=median, reached=median is not None))
    return KmMedianResult(version=DERIVE_KM_MEDIAN_VERSION, unit=unit, groups=groups)


# The functions satisfy base.py's signatures.
_SET_OPERATION: SetOperationFn = set_operation
_CHECK_SUBSET: CheckSubsetFn = check_subset
_RANK_TOP_N: RankTopNFn = rank_top_n
_DERIVE_KM_MEDIAN: DeriveKmMedianFn = derive_km_median


# --- Steps: inputs, reads and post-guards ----------------------------------------------------------


def _case_ids(read: StepValueReader, step_id: str) -> list[str]:
    value = read(step_id, "/observed/caseIds")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise FunctionError(f"{step_id}/observed/caseIds is not a case-ID list")
    return [str(item) for item in value]


def _optional(read: StepValueReader, step_id: str, pointer: str) -> JsonValue:
    try:
        return read(step_id, pointer)
    except LookupError:
        return None


def _rows(read: StepValueReader, inp: RankTopNInput) -> list[Mapping[str, JsonValue]]:
    source = next((s for s in RANK_SOURCES.values() if s.key == inp.key), None)
    if source is None:
        raise FunctionError(f"no ranking source names its rows by {inp.key}")
    rows = read(inp.rows, source.pointer)
    if not isinstance(rows, list):
        raise FunctionError(f"{inp.rows}{source.pointer} is not a list of rows")
    out: list[Mapping[str, JsonValue]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise FunctionError("every ranked row is an object")
        out.append(row)
    if source.extra_pointer is not None:
        extra = _optional(read, inp.rows, source.extra_pointer)
        if isinstance(extra, dict):
            out.append(extra)
    if inp.thresholds is not None:
        limits: dict[str, JsonValue] = inp.thresholds.model_dump(mode="json", by_alias=True)
        out = [row for row in out if passes_thresholds(row, limits)]
    return out


def _settle(observed: BaseModel, status: str = "ok", limitation: Code | None = None, warnings: Sequence[str] = ()) -> ToolResult:
    data: JsonValue = observed.model_dump(mode="json", by_alias=True)
    return ToolResult(status="empty" if status == "empty" else "ok", observed=data, warnings=list(warnings), limitation=limitation)


def _run_set_operation(input: JsonValue, read: StepValueReader) -> ToolResult:
    inp = SetOperationInput.model_validate(input)
    b = _case_ids(read, inp.b) if inp.b is not None else []
    result = set_operation(inp.op, _case_ids(read, inp.a), b)
    return _settle(result, "empty", "EMPTY_COHORT") if result.count == 0 else _settle(result)


def _run_check_subset(input: JsonValue, read: StepValueReader) -> ToolResult:
    inp = CheckSubsetInput.model_validate(input)
    result = check_subset(_case_ids(read, inp.inner), _case_ids(read, inp.outer))
    return _settle(result) if result.is_subset else _settle(result, limitation="NOT_SUBSET")


def _run_rank_top_n(input: JsonValue, read: StepValueReader) -> ToolResult:
    inp = RankTopNInput.model_validate(input)
    result = rank_top_n(_rows(read, inp), key=inp.key, rank_by=inp.rank_by, n=inp.n, ascending=inp.ascending, tie_break=inp.tie_break)
    if result.count == 0:
        return _settle(result, "empty", "FEWER_THAN_N")
    if result.total_rows < inp.n:
        return _settle(result, limitation="FEWER_THAN_N")
    if result.ties_at_cutoff:
        return _settle(result, limitation="TIES_AT_CUTOFF")
    return _settle(result)


def _run_derive_km_median(input: JsonValue, read: StepValueReader) -> ToolResult:
    inp = DeriveKmMedianInput.model_validate(input)
    groups = read(inp.curves, "/observed/groups")
    if not isinstance(groups, list):
        raise FunctionError(f"{inp.curves}/observed/groups is not a list")
    curves: list[KmCurve] = []
    for group in groups:
        label = group.get("label") if isinstance(group, dict) else None
        points = group.get("curve") if isinstance(group, dict) else None
        if not isinstance(label, str) or not isinstance(points, list):
            raise FunctionError("every survival group has a label and a curve")
        curves.append(KmCurve(label=label, points=[KmPoint.model_validate(p) for p in points]))
    return _settle(derive_km_median(curves, unit=KM_UNIT))


RUNNERS: Final[dict[ExecutorFunctionName, Callable[[JsonValue, StepValueReader], ToolResult]]] = {
    "set_operation": _run_set_operation,
    "check_subset": _run_check_subset,
    "rank_top_n": _run_rank_top_n,
    "derive_km_median": _run_derive_km_median,
}


def run_function(name: ExecutorFunctionName, input: JsonValue, read: StepValueReader) -> ToolResult:
    """Run one executor function as a step. Never raises for a bad input or a failed computation:
    the result is `error` with the reason and code INTERNAL_ERROR (TOOLS.md: an executor function that raised)."""
    try:
        return RUNNERS[name](input, read)
    except (FunctionError, ValidationError, LookupError, KeyError, TypeError, ValueError) as error:
        message = f"{name}: {error}"[:500]
        return ToolResult(
            status="error", observed=None, limitation="TOOL_FAILED",
            error=ToolError(code="INTERNAL_ERROR", message=message, retryable=False),
        )


class FunctionTool:
    """An executor function as base.ExecutorFunction, bound to the executor's step reader."""

    def __init__(self, name: ExecutorFunctionName, read: StepValueReader) -> None:
        self.name = name
        self._read = read
        self._spec = tool_spec(name)

    @property
    def version(self) -> str:
        return self._spec.version

    @property
    def pure(self) -> Literal[True]:
        return True

    @property
    def input_schema(self) -> Mapping[str, JsonValue]:
        return input_json_schema(self.name)

    @property
    def output_schema(self) -> Mapping[str, JsonValue]:
        model = self._spec.observed_model
        schema: dict[str, JsonValue] = {} if model is None else model.model_json_schema(by_alias=True)
        return schema

    @property
    def guards(self) -> Sequence[Guard]:
        return self._spec.contract_guards()

    async def invoke(self, input: JsonValue, ctx: ToolContext) -> ToolResult:
        return run_function(self.name, input, self._read)


def call_function(
    name: ExecutorFunctionName,
    input: JsonValue,
    read: StepValueReader,
    *,
    run_id: str,
    plan_version: int,
    step_id: str,
    call_n: int,
    now: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] | None = None,
) -> ToolCallOutcome:
    """One recorded executor-function call: a single attempt, callId `<runId>.v<n>.<stepId>.c<call_n>`."""
    clock = monotonic or time.monotonic
    started_at, started = iso_utc(now()), clock()
    result = run_function(name, input, read)
    elapsed = max(0, round((clock() - started) * 1000))
    request_sha256 = sha256_hex(canonical_json(input, portable=False))
    response_sha256 = None if result.observed is None else sha256_hex(canonical_json(result.observed, portable=False))
    attempt = Attempt(
        call_id=call_id(run_id, plan_version, step_id, call_n), attempt=1, started_at=started_at, elapsed_ms=elapsed,
        status=result.status, http_status=None, error=result.error, backoff_ms=0,
    )
    return ToolCallOutcome(
        tool=name, tool_version=tool_spec(name).version, transport="in_process", request=input, request_sha256=request_sha256,
        result=result, response_sha256=response_sha256, attempts=[attempt], elapsed_ms=elapsed,
    )
