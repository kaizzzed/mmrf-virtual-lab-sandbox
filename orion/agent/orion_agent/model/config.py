"""Model-role configuration, read once from the environment (RFA rev 25, A4 "Model configuration on Bedrock").

| Variable | Default | Meaning |
| --- | --- | --- |
| `ORION_PLANNER` | `stub` | `stub` (orion_agent.stubs.StubPlanner) or `bedrock` |
| `ORION_COMPOSER` | `stub` | `stub` (StubComposer, mode L without Bedrock) or `bedrock` |
| `ORION_BEDROCK_CLIENT` | `runtime` | `runtime`: AsyncAnthropicBedrock on bedrock-runtime (InvokeModel), which takes inference-profile ids; `mantle`: AsyncAnthropicBedrockMantle on bedrock-mantle, which takes base `anthropic.*` ids |
| `ORION_BEDROCK_REGION` | `us-east-1` | Region of the endpoint |
| `ORION_PLANNER_MODEL` | `us.anthropic.claude-opus-5-5` (`anthropic.claude-opus-5-5` with `mantle`) | The planner's model |
| `ORION_COMPOSER_MODEL` | as the planner | The composer's model |
| `ORION_FALLBACK_MODEL` | `us.anthropic.claude-opus-5` (`anthropic.claude-opus-5` with `mantle`) | Client-side refusal fallback for both roles (Open question 18); `none` or empty turns it off |
| `ORION_PLANNER_EFFORT` | `low` | `low`, `medium` or `high` |
| `ORION_COMPOSER_EFFORT` | `medium` | `low`, `medium` or `high` |
| `ORION_PLANNER_TIMEOUT_S` | `30` | One HTTP attempt of a planner call |
| `ORION_COMPOSER_TIMEOUT_S` | `60` | One HTTP attempt of a composer call (the A4 step timeout) |
| `ORION_MODEL_MAX_RETRIES` | `1` | SDK retries of one attempt on 429, 5xx, a timeout or a lost connection (0 to 3) |
| `ORION_MODEL_MAX_TOKENS` | `16000` | `max_tokens` of every real call; thinking shares the budget (A4) |
| `ORION_MODEL_CACHE_TTL` | `5m` | TTL of the cached system prefix: `5m` or `1h` (A4; Open questions 10 and 12) |

Model ids. On-demand calls to the base ids `anthropic.claude-opus-5-5` and `anthropic.claude-opus-5`
through bedrock-runtime fail with "on-demand throughput isn't supported; use an inference profile"
(verified by the coordinator on 2026-11-05 with Converse in us-east-1, where `us.anthropic.claude-opus-5-5`
and `us.anthropic.claude-opus-5` succeeded). Inference-profile ids are a bedrock-runtime concept:
the Anthropic docs show them only with `AnthropicBedrock` (InvokeModel), while the Mantle endpoint
documents base `anthropic.*` ids. So the default client is `runtime` with the `us.*` profiles;
`global.anthropic.claude-opus-5-5` and `global.anthropic.claude-opus-5` also exist (no pricing
premium, routing across all regions). interfaces.PLANNER_MODEL and REFUSAL_FALLBACK_MODEL equal
the `runtime` defaults below (tests/test_contract.py keeps them so); this module does not read them.

Worst case of one role call. The refusal fallback runs inside one of the SDK's (max_retries + 1)
attempts: the primary's refusal and the fallback hop share that attempt, and the attempts after
it start at the pinned fallback. So a call makes at most max_retries + 2 HTTP requests of
`timeout_s` each: 3 x 30 s = 90 s for the planner with the defaults. The fallback gets only the
attempts left after the refusal (none when the refusal came on the last one); a refusal whose
fallback call then fails ends as a decline, not an error (client.py).

The effort and timeout defaults are the design's starting points. Measured against NFR-01 on
2026-11-05 (orion/notes/bedrock-evals.md): at effort low the plan-visible p95 is 4.19 s over 94
asks, so the efforts stay as the design sets them. A planner attempt stalled until the 30 s timeout
twice in about 250 calls (the SDK retry then answered in seconds); a shorter planner timeout is
left to the team. The record's `ModelCallRecord.effort` takes only `low`, `medium` and `high`, so
those are the efforts this setting accepts.
"""

import os
from collections.abc import Mapping
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RoleKind = Literal["stub", "bedrock"]
BedrockClientKind = Literal["runtime", "mantle"]
Effort = Literal["low", "medium", "high"]
CacheTtl = Literal["5m", "1h"]

DEFAULT_REGION: Final = "us-east-1"
# Inference profiles (bedrock-runtime) and base ids (bedrock-mantle), per client.
DEFAULT_MODELS: Final[dict[BedrockClientKind, tuple[str, str]]] = {
    "runtime": ("us.anthropic.claude-opus-5-5", "us.anthropic.claude-opus-5"),
    "mantle": ("anthropic.claude-opus-5-5", "anthropic.claude-opus-5"),
}
DEFAULT_PLANNER_EFFORT: Final[Effort] = "low"
DEFAULT_COMPOSER_EFFORT: Final[Effort] = "medium"
DEFAULT_PLANNER_TIMEOUT_S: Final = 30.0
DEFAULT_COMPOSER_TIMEOUT_S: Final = 60.0
DEFAULT_MAX_RETRIES: Final = 1
DEFAULT_MAX_TOKENS: Final = 16_000
# A warm-up is a small real call (a max_tokens 0 warm-up rejects structured output, A4).
WARMUP_MAX_TOKENS: Final = 1_024
NO_FALLBACK: Final = frozenset({"", "none", "off"})

ENV_NAMES: Final[dict[str, str]] = {
    "ORION_PLANNER": "planner",
    "ORION_COMPOSER": "composer",
    "ORION_BEDROCK_CLIENT": "bedrock_client",
    "ORION_BEDROCK_REGION": "region",
    "ORION_PLANNER_MODEL": "planner_model",
    "ORION_COMPOSER_MODEL": "composer_model",
    "ORION_FALLBACK_MODEL": "fallback_model",
    "ORION_PLANNER_EFFORT": "planner_effort",
    "ORION_COMPOSER_EFFORT": "composer_effort",
    "ORION_PLANNER_TIMEOUT_S": "planner_timeout_s",
    "ORION_COMPOSER_TIMEOUT_S": "composer_timeout_s",
    "ORION_MODEL_MAX_RETRIES": "max_retries",
    "ORION_MODEL_MAX_TOKENS": "max_tokens",
    "ORION_MODEL_CACHE_TTL": "cache_ttl",
}


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    planner: RoleKind = "stub"
    composer: RoleKind = "stub"
    bedrock_client: BedrockClientKind = "runtime"
    region: str = Field(default=DEFAULT_REGION, min_length=1)
    planner_model: str = Field(default=DEFAULT_MODELS["runtime"][0], min_length=1)
    composer_model: str = Field(default=DEFAULT_MODELS["runtime"][0], min_length=1)
    # None turns the refusal fallback off: a refusal then declines at once.
    fallback_model: str | None = DEFAULT_MODELS["runtime"][1]
    planner_effort: Effort = DEFAULT_PLANNER_EFFORT
    composer_effort: Effort = DEFAULT_COMPOSER_EFFORT
    planner_timeout_s: float = Field(default=DEFAULT_PLANNER_TIMEOUT_S, gt=0, le=600)
    composer_timeout_s: float = Field(default=DEFAULT_COMPOSER_TIMEOUT_S, gt=0, le=600)
    # ModelCallRecord.attempt is 1 to 4, so one call takes at most three retries.
    max_retries: int = Field(default=DEFAULT_MAX_RETRIES, ge=0, le=3)
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=1_024, le=64_000)
    cache_ttl: CacheTtl = "5m"

    @model_validator(mode="before")
    @classmethod
    def _client_defaults(cls, data: object) -> object:
        """Model ids left unset take the defaults of the chosen client."""
        if not isinstance(data, dict):
            return data
        client = str(data.get("bedrock_client", "runtime")).strip().lower()
        primary, fallback = DEFAULT_MODELS["mantle" if client == "mantle" else "runtime"]
        filled = dict(data)
        filled.setdefault("planner_model", primary)
        filled.setdefault("composer_model", primary)
        filled.setdefault("fallback_model", fallback)
        return filled

    @field_validator("planner", "composer", "bedrock_client", "planner_effort", "composer_effort", "cache_ttl", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("planner_model", "composer_model", "region", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("fallback_model", mode="before")
    @classmethod
    def _fallback_off(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in NO_FALLBACK:
            return None
        return value.strip() if isinstance(value, str) else value

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ModelConfig":
        source = os.environ if env is None else env
        values: dict[str, object] = {}
        for name, field in ENV_NAMES.items():
            raw = source.get(name)
            # ORION_FALLBACK_MODEL may be set empty on purpose: that turns the fallback off.
            if raw is not None and (raw.strip() or field == "fallback_model"):
                values[field] = raw
        return cls.model_validate(values)

    @property
    def uses_bedrock(self) -> bool:
        return "bedrock" in (self.planner, self.composer)
