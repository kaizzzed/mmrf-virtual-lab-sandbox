"""Where model calls are recorded: one scope per run, entered by the run controller.

`Planner.plan` and `Composer.compose` take no run id, but every ModelCallRecord needs one in its
callId (`<runId>.planner.c<n>`, `<runId>.v<planVersion>.composer.c<n>`, contract "Run record").
The controller therefore enters a scope around each model role call:

    with model_call_scope(run_id, existing=record.model_calls) as scope:
        result = await planner.plan(question, context, ledger)

`existing` continues the numbering, so every callId in a record stays unique (a clarify_answer
re-plans as c3 after c1 and c2). The scope keeps every record its calls made, in order, including
those of a call that raised or was cancelled: on Stop the controller cancels the task and then
reads `scope.records`, where the aborted call is already recorded with outcome `cancelled`.
"""

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Final

from ..models import ModelCallRecord, ModelRole

_RUN_ID: Final = re.compile(r"^[0-9a-f]{32}$")
_CALL_ID: Final = re.compile(r"^([0-9a-f]{32})\.(planner|v[1-9][0-9]*\.composer)\.c([1-9][0-9]*)$")
_SCOPE: ContextVar["ModelCallScope | None"] = ContextVar("orion_model_call_scope", default=None)


def iso_now() -> str:
    """ISO 8601 UTC with milliseconds and a Z, as every record time is written."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _prefix(role: ModelRole, plan_version: int | None) -> str:
    if role == "planner":
        return "planner"
    if plan_version is None or plan_version < 1:
        raise ValueError("a composer call belongs to a plan version")
    return f"v{plan_version}.composer"


class ModelCallScope:
    """The model calls of one run: callId numbering and the records made inside the scope."""

    def __init__(self, run_id: str, existing: Sequence[ModelCallRecord] = ()) -> None:
        if not _RUN_ID.match(run_id):
            raise ValueError(f"{run_id!r} is not a run id")
        self.run_id = run_id
        self.records: list[ModelCallRecord] = []
        self._last: dict[str, int] = {}
        for record in existing:
            match = _CALL_ID.match(record.call_id)
            if match and match.group(1) == run_id:
                prefix, n = match.group(2), int(match.group(3))
                self._last[prefix] = max(self._last.get(prefix, 0), n)

    def next_call_id(self, role: ModelRole, plan_version: int | None = None) -> str:
        prefix = _prefix(role, plan_version)
        n = self._last.get(prefix, 0) + 1
        self._last[prefix] = n
        return f"{self.run_id}.{prefix}.c{n}"

    def add(self, records: Sequence[ModelCallRecord]) -> None:
        self.records.extend(records)

    def replace(self, record: ModelCallRecord) -> None:
        """Swap the record with the same callId for `record` (an outcome re-marked after validation)."""
        for i, existing in enumerate(self.records):
            if existing.call_id == record.call_id:
                self.records[i] = record
                return
        self.records.append(record)


@contextmanager
def model_call_scope(run_id: str, existing: Sequence[ModelCallRecord] = ()) -> Iterator[ModelCallScope]:
    scope = ModelCallScope(run_id, existing)
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def current_scope() -> ModelCallScope | None:
    return _SCOPE.get()
