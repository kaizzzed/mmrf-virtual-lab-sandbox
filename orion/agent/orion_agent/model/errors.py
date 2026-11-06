"""Model call failures, mapped to the contract's codes.

A refusal is not an error: the roles return it as a decline (PlannerResult.declined,
ComposerResult.declined). Everything else that stops a model call raises ModelRoleError. The
contract has one controller code for an unrecoverable failure, INTERNAL_ERROR, so every kind maps
to it: a planner error ends the run Failed (RUN_ERROR INTERNAL_ERROR), and a composer error leaves
an Observed-only answer (`answer.write_answer`). `kind` and `reason` say what happened, and
`model_calls` holds every call the role made before it failed, the failed attempts included.
"""

from collections.abc import Sequence
from typing import Final, Literal

from ..models import Code, ModelCallRecord, ModelRole

ErrorKind = Literal[
    "timeout", "rate_limited", "unavailable", "rejected", "server_error", "invalid_output", "missing_scope", "credentials"
]

_REASONS: Final[dict[ErrorKind, str]] = {
    "timeout": "the {role} model did not answer in time",
    "rate_limited": "Bedrock throttled the {role} model call",
    "unavailable": "Bedrock could not be reached for the {role} model call",
    "rejected": "Bedrock rejected the {role} model request",
    "server_error": "Bedrock returned an error for the {role} model call",
    "invalid_output": "the {role} model's output failed validation twice",
    "missing_scope": "the {role} model call had no run to record it in",
    "credentials": "no AWS credentials were available for the {role} model call",
}


class ModelRoleError(Exception):
    code: Code = "INTERNAL_ERROR"

    def __init__(
        self,
        role: ModelRole,
        kind: ErrorKind,
        detail: str,
        model_calls: Sequence[ModelCallRecord] = (),
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"{role} {kind}: {detail}")
        self.role = role
        self.kind = kind
        self.detail = detail
        self.status_code = status_code
        self.model_calls = list(model_calls)

    @property
    def reason(self) -> str:
        """Plain words for the run's error message or the withheld line, never the raw detail."""
        text = _REASONS[self.kind].format(role=self.role)
        return f"{text} (HTTP {self.status_code})" if self.status_code is not None else text

    @property
    def retryable(self) -> bool:
        return self.kind in ("timeout", "rate_limited", "unavailable", "server_error")

    def with_calls(self, model_calls: Sequence[ModelCallRecord]) -> "ModelRoleError":
        """The same error carrying every call of the role invocation, not only the failed one."""
        error = ModelRoleError(self.role, self.kind, self.detail, model_calls, status_code=self.status_code)
        error.__cause__ = self.__cause__
        return error
