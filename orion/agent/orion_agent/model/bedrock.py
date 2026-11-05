"""The Anthropic SDK on Amazon Bedrock: the only module that imports `anthropic` (pinned 2.5.0).

Client. `runtime` (the default) is AsyncAnthropicBedrock, which calls bedrock-runtime InvokeModel
(`/model/<id>/invoke`) and takes inference-profile ids such as `us.anthropic.claude-opus-5-5`;
`mantle` is AsyncAnthropicBedrockMantle on `bedrock-mantle.<region>.api.aws/anthropic/v1/messages`,
which takes base `anthropic.*` ids. Both sign with SigV4 from the default AWS credential chain (the
Runtime's role); this module reads no credential.

Request (A4): no tools, no tool_choice, no sampling parameters, no prefill; thinking omitted
(adaptive, always on for Opus 5.5); `output_config` carries the role's effort and the JSON schema;
`max_tokens` 16,000. The system prompt is one text block with an explicit `cache_control`
breakpoint (the InvokeModel path rejects top-level automatic caching).

Refusals: Bedrock has no server-side `fallbacks`, so the client carries the SDK's
BetaRefusalFallbackMiddleware with the configured fallback model, and every call runs inside a
BetaFallbackState kept per conversation (a run's role), so a re-compose after a refusal starts on
the model that accepted. Calls go through `client.beta.messages`, which the middleware requires.

Recording: an inner middleware sees every HTTP attempt (each SDK retry and each fallback hop) and
writes one ModelCallRecord per attempt into the active CallLog.
"""

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any, Final

import httpx2
from anthropic import (
    APIConnectionError,
    APIRequest,
    APIStatusError,
    APITimeoutError,
    AnthropicError,
    AsyncAnthropicBedrock,
    AsyncAnthropicBedrockMantle,
    AsyncAPIResponse,
    AsyncCallNext,
    BetaFallbackState,
    BetaRefusalFallbackMiddleware,
    Middleware,
    RateLimitError,
)
from anthropic.types.beta import (
    BetaCacheControlEphemeralParam,
    BetaMessage,
    BetaMessageParam,
    BetaOutputConfigParam,
    BetaTextBlockParam,
)

from .calls import iso_now
from .config import ModelConfig
from .errors import ErrorKind, ModelRoleError
from .transport import CallLog, ModelReply, ModelRequest, Outcome, Usage

SDK_VERSION: Final = "2.5.0"
_FALLBACK_STATES_KEPT: Final = 512
_ACTIVE_LOG: ContextVar[CallLog | None] = ContextVar("orion_model_call_log", default=None)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(data: object) -> Usage:
    if not isinstance(data, dict):
        return Usage()
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return Usage()
    return Usage(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_int(usage.get("cache_creation_input_tokens")),
    )


class CallRecorder(Middleware):
    """Records each HTTP attempt of a model call: placed after the fallback middleware, it sees every hop."""

    async def handle_async(self, request: APIRequest, call_next: AsyncCallNext) -> AsyncAPIResponse[Any]:
        log = _ACTIVE_LOG.get()
        body = request.json
        if log is None or not isinstance(body, dict):
            return await call_next(request)
        model = str(body.get("model") or log.primary_model)
        call_id = log.next_call_id()
        attempt = request.retries_taken + 1
        started_at = iso_now()
        start = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - start) * 1000)

        try:
            response = await call_next(request)
        except asyncio.CancelledError:
            log.add(call_id, model=model, attempt=attempt, started_at=started_at, elapsed_ms=elapsed(), outcome="cancelled", stop_reason=None)
            raise
        except BaseException:
            log.add(call_id, model=model, attempt=attempt, started_at=started_at, elapsed_ms=elapsed(), outcome="error", stop_reason=None)
            raise
        if response.status_code >= 400:
            log.add(call_id, model=model, attempt=attempt, started_at=started_at, elapsed_ms=elapsed(), outcome="error", stop_reason=None)
            return response
        try:
            data: object = json.loads(await response.read())
        except (ValueError, httpx2.HTTPError):
            data = None
        stop = data.get("stop_reason") if isinstance(data, dict) else None
        stop_reason = stop if isinstance(stop, str) else None
        outcome: Outcome = "refused" if stop_reason == "refusal" else "ok"
        log.add(
            call_id, model=model, attempt=attempt, started_at=started_at, elapsed_ms=elapsed(),
            outcome=outcome, stop_reason=stop_reason, usage=_usage(data),
        )
        return response


def _error_kind(error: AnthropicError) -> tuple[ErrorKind, int | None]:
    if isinstance(error, APITimeoutError):
        return "timeout", None
    if isinstance(error, RateLimitError):
        return "rate_limited", error.status_code
    if isinstance(error, APIConnectionError):
        return "unavailable", None
    if isinstance(error, APIStatusError):
        return ("server_error" if error.status_code >= 500 else "rejected"), error.status_code
    return "rejected", None


def build_client(
    config: ModelConfig, *, http_client: httpx2.AsyncClient | None = None, base_url: str | None = None, api_key: str | None = None
) -> AsyncAnthropicBedrock | AsyncAnthropicBedrockMantle:
    """The SDK client for `config`: the fallback middleware outside, the recorder inside.

    `http_client`, `base_url` and `api_key` exist for tests with a mock transport; in a
    deployment all three stay None (default AWS credential chain, the region's endpoint).
    """
    middleware: list[Middleware] = []
    if config.fallback_model is not None:
        middleware.append(BetaRefusalFallbackMiddleware([{"model": config.fallback_model}]))
    middleware.append(CallRecorder())
    if config.bedrock_client == "mantle":
        return AsyncAnthropicBedrockMantle(
            aws_region=config.region, base_url=base_url, api_key=api_key, max_retries=config.max_retries,
            http_client=http_client, middleware=middleware,
        )
    return AsyncAnthropicBedrock(
        aws_region=config.region, base_url=base_url, api_key=api_key, max_retries=config.max_retries,
        http_client=http_client, middleware=middleware,
    )


class BedrockTransport:
    """ModelTransport over the SDK. One per process: the planner and composer share its connections."""

    def __init__(self, config: ModelConfig, client: AsyncAnthropicBedrock | AsyncAnthropicBedrockMantle | None = None) -> None:
        self.config = config
        self.client = client if client is not None else build_client(config)
        self._states: OrderedDict[str, BetaFallbackState] = OrderedDict()

    @property
    def fallback_model(self) -> str | None:
        return self.config.fallback_model

    def fallback_state(self, conversation: str) -> BetaFallbackState:
        """The pin for one conversation; a request without a conversation gets a fresh state."""
        if not conversation:
            return BetaFallbackState()
        state = self._states.pop(conversation, None) or BetaFallbackState()
        self._states[conversation] = state
        while len(self._states) > _FALLBACK_STATES_KEPT:
            self._states.popitem(last=False)
        return state

    async def send(self, request: ModelRequest, log: CallLog | None) -> ModelReply:
        cache_control: BetaCacheControlEphemeralParam = {"type": "ephemeral", "ttl": request.cache_ttl}
        system: list[BetaTextBlockParam] = [{"type": "text", "text": request.system, "cache_control": cache_control}]
        messages: list[BetaMessageParam] = [{"role": "user", "content": request.user}]
        output_config: BetaOutputConfigParam = {
            "effort": request.effort,
            "format": {"type": "json_schema", "schema": dict(request.schema)},
        }
        token = _ACTIVE_LOG.set(log)
        start = time.monotonic()
        try:
            with self.fallback_state(request.conversation):
                message = await self.client.beta.messages.create(
                    model=request.model,
                    max_tokens=request.max_tokens,
                    system=system,
                    messages=messages,
                    output_config=output_config,
                    timeout=request.timeout_s,
                )
        except AnthropicError as error:
            kind, status = _error_kind(error)
            raise ModelRoleError(
                request.role, kind, f"{type(error).__name__}: {error}", log.records if log else (), status_code=status
            ) from error
        finally:
            _ACTIVE_LOG.reset(token)
        return reply_from(message, elapsed_ms=int((time.monotonic() - start) * 1000))


def reply_from(message: BetaMessage, *, elapsed_ms: int = 0) -> ModelReply:
    texts: Sequence[str] = [block.text for block in message.content if block.type == "text"]
    details = message.stop_details
    category = getattr(details, "category", None) if details is not None else None
    usage = message.usage
    return ModelReply(
        text="".join(texts),
        stop_reason=message.stop_reason,
        model=message.model,
        refusal_category=category if isinstance(category, str) else None,
        usage=Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens,
        ),
        elapsed_ms=elapsed_ms,
    )
