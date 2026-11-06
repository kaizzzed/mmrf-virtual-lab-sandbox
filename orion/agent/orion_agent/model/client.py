"""The one Bedrock client both model roles share (RFA rev 25, A4 "Model configuration on Bedrock").

Claude on Bedrock through the Anthropic SDK, on `client.beta.messages`: `AsyncAnthropicBedrock`
(bedrock-runtime, InvokeModel, which takes the `us.anthropic.*` inference-profile ids; the
default, see config.py) or `AsyncAnthropicBedrockMantle` (the bedrock-mantle Messages endpoint,
base `anthropic.*` ids). Both carry two middlewares:

- `BetaRefusalFallbackMiddleware`: Bedrock has no server-side fallback, so a refusal is retried
  client-side on the fallback model (Claude Opus 5, Open question 18). One `BetaFallbackState`
  per run and role pins the run's later calls (the IR repair, the re-compose) to the model that
  accepted.
- `HopRecorder`, inside it: sees every HTTP attempt, the fallback hop and the SDK's retries
  included, and turns each into a ModelCallRecord (callId, model, fallback, effort, attempt,
  outcome, stop reason, tokens with cache reads and writes). `attempt` counts the attempts on
  that model within the call: the fallback hop runs inside one of the SDK's attempts, so the
  SDK's own retry count would number the fallback's first attempt 2.

The middleware's refusal path is completed here, where anthropic 2.5.0 leaves it open for
non-streaming calls (its `handle_async` returns the failed hop's response, and the SDK raises
that status): a refusal whose fallback call then fails is still a refusal (`CallResult.refused`),
and a pin to a fallback that failed is lifted so the run's next call starts at the primary again.
A cancel that lands outside an HTTP attempt (the SDK's retry backoff, request signing) is
recorded as a cancelled hop, so an aborted call always has its `cancelled` entry. Any other
exception (unresolved AWS credentials raise a plain RuntimeError while the request is signed)
becomes a ModelRoleError carrying the calls made.

Every request: the model, `max_tokens`, one cached system block (`cache_control` ephemeral), one
user message, `output_config` with the role's effort and a JSON schema. No tools, `tool_choice`,
`temperature`, `top_p`, `top_k`, prefill or `thinking`: thinking cannot be disabled on Opus 5.5
and runs adaptive when omitted. Calls are single-turn, so no thinking block is ever replayed.
"""

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Final, Generic, Literal, TypeVar

import anthropic
import httpx2
from anthropic import (
    APIRequest,
    AsyncAnthropicBedrock,
    AsyncAnthropicBedrockMantle,
    AsyncAPIResponse,
    AsyncCallNext,
    BetaFallbackState,
    BetaRefusalFallbackMiddleware,
    Middleware,
)
from anthropic.types.beta import BetaMessage, BetaMessageParam, BetaOutputConfigParam, BetaTextBlockParam
from pydantic import JsonValue

from ..models import ModelCallRecord, ModelRole
from .calls import ModelCallScope, current_scope, iso_now
from .config import BedrockClientKind, CacheTtl, Effort, ModelConfig
from .errors import ErrorKind, ModelRoleError

T = TypeVar("T")
Outcome = Literal["ok", "refused", "invalid", "error", "cancelled"]
MAX_FALLBACK_STATES: Final = 512
# Parse failures are reported to the role, which decides on the one IR repair.
ParseFn = Callable[[str], T]


@dataclass
class HopObservation:
    """One HTTP attempt the client made: the first call, a fallback hop or an SDK retry."""

    model: str
    attempt: int
    started_at: str
    elapsed_ms: int
    status_code: int | None
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cancelled: bool = False
    error: str | None = None
    refusal_category: str | None = None

    @property
    def refused(self) -> bool:
        return self.status_code == 200 and self.stop_reason == "refusal"

    @property
    def served(self) -> bool:
        return self.status_code == 200 and self.stop_reason != "refusal"


@dataclass
class _Trace:
    hops: list[HopObservation] = field(default_factory=list)


_TRACE: ContextVar[_Trace | None] = ContextVar("orion_model_trace", default=None)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


MAX_ATTEMPT: Final = 4  # ModelCallRecord.attempt is 1 to 4


def next_attempt(hops: list[HopObservation], model: str) -> int:
    """The attempt number of the next hop on `model`: the hops of this call on that model, plus one."""
    return min(1 + sum(1 for hop in hops if hop.model == model), MAX_ATTEMPT)


class HopRecorder(Middleware):
    """Records every HTTP attempt of a traced call. Runs inside the refusal fallback middleware."""

    async def handle_async(self, request: APIRequest, call_next: AsyncCallNext) -> AsyncAPIResponse[Any]:
        trace = _TRACE.get()
        if trace is None:
            return await call_next(request)
        body = request.json if isinstance(request.json, dict) else {}
        model = str(body.get("model") or "")
        hop = HopObservation(
            model=model,
            attempt=next_attempt(trace.hops, model),
            started_at=iso_now(),
            elapsed_ms=0,
            status_code=None,
        )
        started = time.monotonic()
        try:
            response = await call_next(request)
        except asyncio.CancelledError:
            hop.cancelled, hop.error = True, "CANCELLED"
            raise
        except Exception as exc:
            hop.error = type(exc).__name__
            raise
        finally:
            hop.elapsed_ms = int((time.monotonic() - started) * 1000)
            trace.hops.append(hop)
        hop.status_code = response.status_code
        if response.status_code == 200 and not request.stream:
            try:
                data = json.loads(await response.read())
            except ValueError:
                data = {}
            usage = data.get("usage") if isinstance(data, dict) else None
            usage = usage if isinstance(usage, dict) else {}
            hop.stop_reason = data.get("stop_reason") if isinstance(data, dict) else None
            hop.input_tokens = _int(usage.get("input_tokens"))
            hop.output_tokens = _int(usage.get("output_tokens"))
            hop.cache_read_tokens = _int(usage.get("cache_read_input_tokens"))
            hop.cache_write_tokens = _int(usage.get("cache_creation_input_tokens"))
            details = data.get("stop_details") if isinstance(data, dict) else None
            category = details.get("category") if isinstance(details, dict) else None
            hop.refusal_category = category if isinstance(category, str) else None
        return response


class FallbackStates:
    """One BetaFallbackState per (run, role), kept across invocations of the run in this process."""

    def __init__(self, capacity: int = MAX_FALLBACK_STATES) -> None:
        self._capacity = capacity
        self._states: OrderedDict[tuple[str, str], BetaFallbackState] = OrderedDict()

    def get(self, run_id: str, role: ModelRole) -> BetaFallbackState:
        key = (run_id, role)
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = BetaFallbackState()
            while len(self._states) > self._capacity:
                self._states.popitem(last=False)
        self._states.move_to_end(key)
        return state


@dataclass(frozen=True)
class ModelRequest:
    role: ModelRole
    model: str
    effort: Effort
    system_prefix: str
    user_text: str
    schema: dict[str, JsonValue]
    max_tokens: int
    timeout_s: float
    cache_ttl: CacheTtl = "5m"
    plan_version: int | None = None


@dataclass(frozen=True)
class CallResult(Generic[T]):
    """What one model call produced, after the refusal fallback ran."""

    value: T | None  # the parsed output, when a model served and its text parsed
    # The call ended in a refusal: every model tried declined, or a model declined and the fallback
    # call then failed (no hop served; `served_model` is None and `declined_models` names who declined).
    refused: bool
    refusal_category: str | None
    invalid_detail: str | None  # a model served, but its text did not parse
    text: str | None
    stop_reason: str | None
    served_model: str | None  # the model whose answer was returned (None when refused)
    records: list[ModelCallRecord]
    hops: list[HopObservation]
    declined_models: list[str]  # every model that refused, in order


def _kind_for(exc: anthropic.APIError) -> tuple[ErrorKind, int | None]:
    if isinstance(exc, anthropic.APITimeoutError):
        return "timeout", None
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limited", 429
    if isinstance(exc, anthropic.APIConnectionError):
        return "unavailable", None
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        # Bedrock's ModelTimeoutException is HTTP 408 and its ModelErrorException 424: the model side
        # timed out or failed, which is not a rejected request.
        if status == 408:
            return "timeout", status
        if status == 424 or status >= 500:
            return "server_error", status
        return "rejected", status
    return "server_error", None


def _credentials_missing(exc: Exception) -> bool:
    """anthropic 2.5.0's Bedrock signer raises RuntimeError("could not resolve credentials from
    session") when boto3 finds no credential; botocore's own credential errors say so in their name."""
    return "credential" in f"{type(exc).__name__} {exc}".casefold()


def _outcome(hop: HopObservation, invalid: bool) -> Outcome:
    if hop.cancelled:
        return "cancelled"
    if hop.status_code != 200 or hop.error is not None:
        return "error"
    if hop.stop_reason == "refusal":
        return "refused"
    return "invalid" if invalid else "ok"


def build_records(
    hops: list[HopObservation],
    *,
    scope: ModelCallScope | None,
    role: ModelRole,
    primary_model: str,
    effort: Effort,
    plan_version: int | None,
    invalid: bool = False,
) -> list[ModelCallRecord]:
    """One ModelCallRecord per hop, numbered in the scope; `invalid` marks the served hop."""
    if scope is None:
        return []
    served_index = max((i for i, hop in enumerate(hops) if hop.served), default=None)
    records: list[ModelCallRecord] = []
    for i, hop in enumerate(hops):
        records.append(
            ModelCallRecord(
                call_id=scope.next_call_id(role, plan_version),
                role=role,
                model=hop.model or primary_model,
                fallback=hop.model != primary_model,
                effort=effort,
                attempt=hop.attempt,
                started_at=hop.started_at,
                elapsed_ms=hop.elapsed_ms,
                outcome=_outcome(hop, invalid and i == served_index),
                stop_reason=hop.stop_reason,
                input_tokens=hop.input_tokens,
                output_tokens=hop.output_tokens,
                cache_read_tokens=hop.cache_read_tokens,
                cache_write_tokens=hop.cache_write_tokens,
            )
        )
    scope.add(records)
    return records


def _text(message: BetaMessage) -> str | None:
    texts = [block.text for block in message.content if block.type == "text"]
    return "".join(texts) if texts else None


class ModelClient:
    """Bedrock (runtime or Mantle), async, with the refusal fallback and the hop recorder.

    `http_client`, `base_url` and `skip_auth` (Mantle only) exist for tests, which drive the real
    SDK and its middlewares over an in-process mock transport: no test reaches a model endpoint.
    """

    def __init__(
        self,
        *,
        region: str,
        fallback_model: str | None,
        max_retries: int,
        kind: BedrockClientKind = "runtime",
        http_client: httpx2.AsyncClient | None = None,
        base_url: str | None = None,
        skip_auth: bool = False,
    ) -> None:
        self.kind = kind
        self.fallback_model = fallback_model
        middleware: list[Middleware] = []
        if fallback_model:
            middleware.append(BetaRefusalFallbackMiddleware([{"model": fallback_model}]))
        middleware.append(HopRecorder())
        self.sdk: AsyncAnthropicBedrock | AsyncAnthropicBedrockMantle
        if kind == "mantle":
            self.sdk = AsyncAnthropicBedrockMantle(
                aws_region=region, base_url=base_url, http_client=http_client, max_retries=max_retries,
                middleware=middleware, skip_auth=skip_auth,
            )
        else:
            if skip_auth:
                raise ValueError("skip_auth is a Mantle client option")
            self.sdk = AsyncAnthropicBedrock(
                aws_region=region, base_url=base_url, http_client=http_client, max_retries=max_retries, middleware=middleware,
            )
        self.states = FallbackStates()

    def _targeted(self, state: BetaFallbackState, request: ModelRequest) -> str:
        """The model the call's next attempt goes to: the pinned fallback, or the request's model."""
        return self.fallback_model if self.fallback_model and state.index is not None and state.index >= 0 else request.model

    def _record_cancel(self, trace: _Trace, state: BetaFallbackState, request: ModelRequest) -> None:
        """A cancel outside an HTTP attempt (the SDK's retry backoff, signing) still records the aborted call."""
        if trace.hops and trace.hops[-1].cancelled:
            return
        self._add_unsent(trace, state, request, cancelled=True, error="CANCELLED")

    def _record_failure(self, trace: _Trace, state: BetaFallbackState, request: ModelRequest, exc: Exception) -> None:
        """An exception outside the SDK's error types. Raised inside an attempt (request signing: no credential
        resolved), HopRecorder already holds it as an error; after a 200, that hop is marked failed; before
        any attempt, the call is recorded as one failed attempt."""
        last = trace.hops[-1] if trace.hops else None
        if last is not None and last.status_code is None and last.error is not None:
            return
        if last is not None and last.status_code == 200 and last.error is None:
            last.error = type(exc).__name__
            return
        self._add_unsent(trace, state, request, cancelled=False, error=type(exc).__name__)

    def _add_unsent(self, trace: _Trace, state: BetaFallbackState, request: ModelRequest, *, cancelled: bool, error: str) -> None:
        model = self._targeted(state, request)
        trace.hops.append(
            HopObservation(
                model=model, attempt=next_attempt(trace.hops, model), started_at=iso_now(), elapsed_ms=0,
                status_code=None, cancelled=cancelled, error=error,
            )
        )

    def _unpin_failed_fallback(self, trace: _Trace, state: BetaFallbackState, request: ModelRequest) -> None:
        """The middleware pins the fallback before its hop is sent; when that hop failed, the run's
        next call starts at the primary model again instead of at a fallback that could not answer."""
        last = trace.hops[-1] if trace.hops else None
        if last is not None and last.model != request.model and not last.served:
            state.index = None

    @classmethod
    def from_config(cls, config: ModelConfig) -> "ModelClient":
        return cls(
            region=config.region, fallback_model=config.fallback_model, max_retries=config.max_retries,
            kind=config.bedrock_client,
        )

    async def _create(
        self,
        request: ModelRequest,
        system: list[BetaTextBlockParam],
        messages: list[BetaMessageParam],
        output_config: BetaOutputConfigParam,
    ) -> BetaMessage:
        # The two clients carry distinct (equivalent) beta resources, so each call is typed on its own.
        if isinstance(self.sdk, AsyncAnthropicBedrockMantle):
            return await self.sdk.beta.messages.create(
                model=request.model, max_tokens=request.max_tokens, system=system, messages=messages,
                output_config=output_config, timeout=request.timeout_s,
            )
        # anthropic 2.5.0's bedrock-runtime beta resource assigns the first-party `create` as a class
        # attribute without subclassing it, which mypy reads as a wrong `self` type; the call is sound.
        return await self.sdk.beta.messages.create(  # type: ignore[misc]
            model=request.model, max_tokens=request.max_tokens, system=system, messages=messages,
            output_config=output_config, timeout=request.timeout_s,
        )

    async def call(self, request: ModelRequest, parse: ParseFn[T], *, record: bool = True) -> CallResult[T]:
        """One model call with its refusal fallback. Raises ModelRoleError on anything but an answer
        or a refusal; every attempt is recorded in the run's scope first (`record=False`: warm-up)."""
        scope = current_scope() if record else None
        if record and scope is None:
            raise ModelRoleError(request.role, "missing_scope", "no model_call_scope is active")
        state = self.states.get(scope.run_id, request.role) if scope is not None else BetaFallbackState()
        trace = _Trace()
        token = _TRACE.set(trace)

        def records(invalid: bool = False) -> list[ModelCallRecord]:
            return build_records(
                trace.hops, scope=scope, role=request.role, primary_model=request.model, effort=request.effort,
                plan_version=request.plan_version, invalid=invalid,
            )

        system: list[BetaTextBlockParam] = [
            {"type": "text", "text": request.system_prefix, "cache_control": {"type": "ephemeral", "ttl": request.cache_ttl}}
        ]
        messages: list[BetaMessageParam] = [{"role": "user", "content": request.user_text}]
        output_config: BetaOutputConfigParam = {
            "effort": request.effort,
            "format": {"type": "json_schema", "schema": dict(request.schema)},
        }
        try:
            with state:
                message = await self._create(request, system, messages, output_config)
        except asyncio.CancelledError:
            self._record_cancel(trace, state, request)
            records()
            raise
        except anthropic.APIError as exc:
            self._unpin_failed_fallback(trace, state, request)
            declined = [hop.model for hop in trace.hops if hop.refused]
            if declined:
                # A model declined and the fallback call then failed: the call still ended in a refusal.
                category = next((hop.refusal_category for hop in reversed(trace.hops) if hop.refused), None)
                return CallResult(None, True, category, None, None, "refusal", None, records(), trace.hops, declined)
            kind, status = _kind_for(exc)
            raise ModelRoleError(request.role, kind, f"{type(exc).__name__}: {exc}", records(), status_code=status) from exc
        except Exception as exc:  # outside the SDK's error types: unresolved credentials, a signing failure
            failure: ErrorKind = "credentials" if _credentials_missing(exc) else "unavailable"
            self._record_failure(trace, state, request, exc)
            raise ModelRoleError(request.role, failure, f"{type(exc).__name__}: {exc}", records()) from exc
        finally:
            _TRACE.reset(token)

        declined = [hop.model for hop in trace.hops if hop.refused]
        category = getattr(message.stop_details, "category", None)
        text = _text(message)
        if message.stop_reason == "refusal":
            return CallResult(None, True, category, None, text, "refusal", None, records(), trace.hops, declined)
        served = next((hop.model for hop in reversed(trace.hops) if hop.served), None) or message.model
        if text is None:
            detail = f"no text in the reply (stop reason {message.stop_reason})"
            return CallResult(None, False, None, detail, None, message.stop_reason, served, records(True), trace.hops, declined)
        try:
            value = parse(text)
        except ValueError as exc:  # pydantic.ValidationError and json.JSONDecodeError are ValueErrors
            detail = str(exc)
            if message.stop_reason == "max_tokens":
                detail = f"the reply was cut off at max_tokens; {detail}"
            return CallResult(None, False, None, detail, text, message.stop_reason, served, records(True), trace.hops, declined)
        return CallResult(value, False, None, None, text, message.stop_reason, served, records(), trace.hops, declined)


_SHARED: dict[tuple[str, str, str | None, int], ModelClient] = {}


def shared_client(config: ModelConfig) -> ModelClient:
    """One client per endpoint, region, fallback and retry count: both roles share one connection pool."""
    key = (config.bedrock_client, config.region, config.fallback_model, config.max_retries)
    client = _SHARED.get(key)
    if client is None:
        client = _SHARED[key] = ModelClient.from_config(config)
    return client
