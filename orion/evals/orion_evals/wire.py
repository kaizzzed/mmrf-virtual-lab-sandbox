"""Requests the harness sends, and the contract's status and code vocabularies (contract v1.2).

Every body is a full `RunAgentInput` (all seven fields), with ORION's action in
`forwardedProps.orion`. Ids are 32 lowercase hex characters from 128 random bits (C-42).
"""

import secrets
from collections.abc import Mapping, Sequence
from typing import Any, Final

LEASED_STATUSES: Final = frozenset({"Planning", "Executing", "Answering", "Stopping"})
WAITING_STATUSES: Final = frozenset({"Clarifying", "AwaitingConsent", "AwaitingConfirmation"})
END_STATUSES: Final = frozenset(
    {"Complete", "Partial", "Limited", "Unsupported", "ObservedOnly", "Cancelled", "Failed", "Interrupted"}
)
REJECTION_CODES: Final = frozenset({"INVALID_INPUT", "RUN_BUSY", "RUN_NOT_FOUND", "NOT_IMPLEMENTED", "STALE_ACTION"})
DATA_STEP_STATUSES: Final = frozenset({"Running", "Retrying", "Done", "Empty", "Failed"})

# F2 "Run and step states": the screen word for each run status.
RUN_SCREEN_WORDS: Final[Mapping[str, str]] = {
    "Planning": "Planning",
    "Clarifying": "Question for you",
    "AwaitingConsent": "Needs your choice",
    "Executing": "Running",
    "AwaitingConfirmation": "Waiting for you",
    "Stopping": "Stopping",
    "Answering": "Writing interpretation",
    "Complete": "Complete",
    "Partial": "Partial: a step failed",
    "Limited": "Stopped by a limitation",
    "Unsupported": "Not supported",
    "ObservedOnly": "Interpretation withheld",
    "Cancelled": "Stopped by you",
    "Failed": "Failed",
    "Interrupted": "Interrupted",
}


def new_id() -> str:
    return secrets.token_hex(16)


def all_cases_context(case_count: int | None = None) -> dict[str, Any]:
    """The cohort snapshot of a fresh browser: all cases active, nothing saved."""
    return {
        "active": {"id": None, "name": "All cases", "filters": {"mode": "and", "root": {}}, "caseCount": case_count},
        "saved": [],
    }


def run_agent_input(
    thread_id: str,
    run_id: str,
    orion: Mapping[str, Any],
    *,
    question: str | None = None,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if question is not None:
        messages.append({"id": new_id(), "role": "user", "content": question})
    return {
        "threadId": thread_id,
        "runId": run_id,
        "state": {},
        "messages": messages,
        "tools": [],
        "context": [],
        "forwardedProps": {"orion": dict(orion)},
    }


def _with_reports(payload: dict[str, Any], viz_reports: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    if viz_reports:
        payload["vizReports"] = [dict(r) for r in viz_reports][:8]
    return payload


def ask(
    question: str,
    cohort_context: Mapping[str, Any],
    *,
    cancel_waiting: str | None = None,
    viz_reports: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": "ask", "question": question, "cohortContext": dict(cohort_context)}
    if cancel_waiting is not None:
        payload["cancelWaiting"] = cancel_waiting
    return _with_reports(payload, viz_reports)


def warmup() -> dict[str, Any]:
    return {"action": "warmup"}


def cancel() -> dict[str, Any]:
    return {"action": "cancel"}


def clarify_answer(option_id: str) -> dict[str, Any]:
    return {"action": "clarify_answer", "optionId": option_id}


def consent(accept: bool) -> dict[str, Any]:
    return {"action": "consent", "accept": accept}


def confirm(nonce: str, decision: str, saved: Sequence[Mapping[str, Any]], make_active: str | None) -> dict[str, Any]:
    return {"action": "confirm", "nonce": nonce, "decision": decision, "saved": [dict(s) for s in saved], "makeActive": make_active}


def edit_plan(
    field: str,
    value: Any,
    *,
    ref: str | None = None,
    cohort_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": "edit_plan", "field": field, "value": value}
    if ref is not None:
        payload["ref"] = ref
    if cohort_context is not None:
        payload["cohortContext"] = dict(cohort_context)
    return payload


def retry_step(step_id: str) -> dict[str, Any]:
    return {"action": "retry_step", "stepId": step_id}


def viz_report(viz_id: str, mounted: bool, error: str | None, pinned_hash_verified: bool | None) -> dict[str, Any]:
    return {
        "action": "viz_report",
        "vizId": viz_id,
        "mounted": mounted,
        "error": error,
        "pinnedHashVerified": pinned_hash_verified,
    }
