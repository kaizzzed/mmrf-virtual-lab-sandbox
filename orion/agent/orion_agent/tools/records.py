"""From a ToolCallOutcome to the run record's CallRecords (contract "Run record", `calls`).

Every attempt is its own CallRecord with its own callId. The ToolClients return a
`RecordedOutcome`, which keeps each attempt's full ToolResult (observed, warnings and provenance),
so a failed first attempt's requests stay in the record too. A cancelled call raises
`ToolCallCancelled`, a CancelledError that carries the outcome so far, the aborted attempt last
(status `error`, error starting CANCELLED).
"""

import asyncio

from pydantic import Field

from ..models import CallRecord, canonical_json, sha256_hex
from .base import ToolCallOutcome, ToolError, ToolResult
from .schemas import EXEC_FUNCTIONS


class RecordedOutcome(ToolCallOutcome):
    """A ToolCallOutcome that also keeps each attempt's ToolResult, in attempt order."""

    attempt_results: list[ToolResult] = Field(default_factory=list)


class ToolCallCancelled(asyncio.CancelledError):
    """ToolClient.call was cancelled (Stop, or an edit that changes the running step).

    It is a CancelledError, so cancellation propagates as asyncio expects; `outcome` holds every
    attempt so far, the aborted one included, for the record.
    """

    def __init__(self, outcome: RecordedOutcome) -> None:
        super().__init__("tool call cancelled")
        self.outcome = outcome


def error_text(tool: str, error: ToolError | None) -> str | None:
    """The CallRecord's `error`: the ToolError code, then the message. An aborted attempt's starts
    CANCELLED; an executor function that raised (code INTERNAL_ERROR) starts INTERNAL_ERROR, whatever
    code an older client gave it."""
    if error is None:
        return None
    code = "INTERNAL_ERROR" if tool in EXEC_FUNCTIONS and error.code != "CANCELLED" else error.code
    return f"{code}: {error.message}"


def call_records(outcome: ToolCallOutcome, *, plan_version: int, step_id: str) -> list[CallRecord]:
    """One CallRecord per attempt, as the record's `calls` keeps them. Without per-attempt results
    (a plain ToolCallOutcome), earlier attempts record their status, status code and error only."""
    per_attempt = outcome.attempt_results if isinstance(outcome, RecordedOutcome) else []
    records: list[CallRecord] = []
    for index, attempt in enumerate(outcome.attempts):
        last = index == len(outcome.attempts) - 1
        result = per_attempt[index] if index < len(per_attempt) else (outcome.result if last else None)
        if result is None:
            result = ToolResult(status=attempt.status, observed=None, error=attempt.error)
        observed = result.observed
        records.append(
            CallRecord(
                call_id=attempt.call_id, plan_version=plan_version, step_id=step_id, tool=outcome.tool, transport=outcome.transport,
                attempt=attempt.attempt, started_at=attempt.started_at, request=outcome.request, request_sha256=outcome.request_sha256,
                http_status=attempt.http_status,
                response_sha256=None if observed is None else sha256_hex(canonical_json(observed, portable=False)),
                elapsed_ms=attempt.elapsed_ms, status=result.status, observed=observed, warnings=list(result.warnings),
                # An attempt that was retried leaves no limitation of its own (as run-record.j1-retry.json records it).
                limitation=result.limitation if last else None, error=error_text(outcome.tool, result.error),
                provenance=list(result.provenance),
            )
        )
    return records
