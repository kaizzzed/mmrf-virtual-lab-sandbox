"""The seam between the model roles and the Anthropic SDK: one structured-output call, recorded.

The roles (planner, composer) build a ModelRequest and read a ModelReply; `bedrock.py` is the only
module that imports the SDK. A CallLog collects one ModelCallRecord per HTTP attempt of the call
(the refusal fallback's hop and SDK retries each get their own callId), and shares each record
with the run's ModelCallScope at once, so a call that raises or is cancelled is still recorded.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from pydantic import JsonValue

from ..models import ModelCallRecord, ModelRole
from .calls import ModelCallScope
from .config import CacheTtl, Effort

Outcome = Literal["ok", "refused", "invalid", "error", "cancelled"]
REFUSAL: Final = "refusal"


@dataclass(frozen=True)
class ModelRequest:
    role: ModelRole
    model: str
    effort: Effort
    # The cached system prefix (rules, vocabulary, catalog, examples): byte-stable across calls.
    system: str
    # Everything that varies per call: the question, context, facts, feedback.
    user: str
    schema: Mapping[str, JsonValue]
    max_tokens: int
    timeout_s: float
    cache_ttl: CacheTtl = "5m"
    plan_version: int | None = None
    # The refusal fallback pins follow-up calls of one conversation (a run's role) to the model
    # that accepted (BetaFallbackState).
    conversation: str = ""


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


@dataclass(frozen=True)
class ModelReply:
    text: str
    stop_reason: str | None
    model: str  # the model that served
    refusal_category: str | None = None
    usage: Usage = Usage()
    elapsed_ms: int = 0

    @property
    def refused(self) -> bool:
        return self.stop_reason == REFUSAL


@dataclass
class CallLog:
    """The ModelCallRecords of one role invocation, numbered by the run's scope."""

    role: ModelRole
    effort: Effort
    primary_model: str
    scope: ModelCallScope
    plan_version: int | None = None
    records: list[ModelCallRecord] = field(default_factory=list)

    def next_call_id(self) -> str:
        return self.scope.next_call_id(self.role, self.plan_version)

    def add(
        self,
        call_id: str,
        *,
        model: str,
        attempt: int,
        started_at: str,
        elapsed_ms: int,
        outcome: Outcome,
        stop_reason: str | None,
        usage: Usage = Usage(),
    ) -> ModelCallRecord:
        record = ModelCallRecord(
            call_id=call_id,
            role=self.role,
            model=model,
            fallback=model != self.primary_model,
            effort=self.effort,
            attempt=max(1, min(attempt, 4)),
            started_at=started_at,
            elapsed_ms=max(0, elapsed_ms),
            outcome=outcome,
            stop_reason=stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
        self.records.append(record)
        self.scope.add([record])
        return record

    def mark_last_served(self, outcome: Outcome) -> None:
        """Re-mark the record of the attempt that served the last reply (an IR that failed validation is `invalid`)."""
        for i in range(len(self.records) - 1, -1, -1):
            record = self.records[i]
            if record.outcome == "ok":
                updated = record.model_copy(update={"outcome": outcome})
                self.records[i] = updated
                self.scope.replace(updated)
                return


class ModelTransport(Protocol):
    @property
    def fallback_model(self) -> str | None: ...

    async def send(self, request: ModelRequest, log: CallLog | None) -> ModelReply:
        """One structured-output call, through the refusal fallback when one is configured.

        `log` None (a warm-up) records nothing. Raises errors.ModelRoleError for every SDK error,
        carrying the records made so far; honours asyncio cancellation (the aborted attempt is
        recorded `cancelled`).
        """
        ...
