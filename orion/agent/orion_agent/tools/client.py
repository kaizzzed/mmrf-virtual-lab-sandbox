"""The ToolClients (base.ToolClient) and their factory, `make_tool_client(settings)`.

Both clients apply the A4 timeout budget the same way; only the transport of one attempt differs:

- `InProcessToolClient` (mode L, and cut 1 on AWS) runs the server tool in this process.
- `GatewayToolClient` (modes A and H) calls `vlab___<tool>` on AgentCore Gateway over MCP, SigV4-signed;
  the Gateway Lambda runs the same tool and returns its ToolResult.

The budget, per call:

- Each attempt gets `min(timeout_s, what remains of STEP_CAP_S)` (the executor passes
  STEP_TIMEOUT_S, 60 s); the tool's HTTP requests get at most TOOL_HTTP_TIMEOUT_S (55 s) of it, so
  the innermost timer fires first and says why.
- One retry, for server reads only, with the identical request, when the attempt failed with a
  retryable ToolError: 429 (after a 10-20 s backoff, jitter injectable), 502, 503, 504, a
  connection failure or a timeout. Never for client writes (they do not come through here) and
  never for kg_query. The retry runs only if STEP_CAP_S (130 s) leaves room for it.
- Attempts take callIds `<runId>.v<planVersion>.<stepId>.c<first_call>` and `c<first_call + 1>`;
  every attempt is kept (`RecordedOutcome.attempt_results`), and `on_retry` runs before the backoff.
- At most MAX_TOOL_CALLS_PER_RUN (20) calls per run: a call past that makes no request and
  returns GUARD_FAILED with the limitation CALL_BUDGET (the compiler stops such plans first).
- Cancelling `call` aborts the in-flight request at once and raises `ToolCallCancelled`, a
  CancelledError carrying the outcome with the aborted attempt (error CANCELLED, never retried).
- Fault injection (ORION_FAULTS, mode L or a test Runtime only): a faulted tool runs in-process over
  a FaultTransport, in either client, so no real request is made for it.
"""

import asyncio
import logging
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, cast

from pydantic import JsonValue, ValidationError

from .. import AGENT_VERSION
from ..events import iso_utc, utc_now
from ..models import Provenance, call_id, canonical_json, sha256_hex
from ..settings import Settings
from .base import (
    BACKOFF_429_S,
    MAX_ATTEMPTS,
    MAX_TOOL_CALLS_PER_RUN,
    STEP_CAP_S,
    TOOL_HTTP_TIMEOUT_S,
    Attempt,
    RetryHook,
    ServerToolName,
    ToolClient,
    ToolContext,
    ToolResult,
)
from .config import ToolConfig
from .credentials import AccessFileCredential, CredentialSource, NoCredential, SecretsManagerCredential, boto3_secret_reader
from .faults import FaultTransport
from .gateway import BotocoreSigner, CredentialsProvider, GatewayConfigError, McpError, McpGatewayClient, default_credentials
from .gateway import error_text as mcp_error_text
from .gateway import parse_tool_result
from .goldens import GoldenStore
from .http import H11Transport, HttpError, HttpTimeout, HttpTransport
from .records import RecordedOutcome, ToolCallCancelled
from .schemas import SERVER_TOOLS, gateway_tool_name, tool_spec
from .server import ServerTool, ToolFailure, build_server_tools, cancelled_error, failure_result
from .vlab import VlabClient

LOG: Final = logging.getLogger("orion_agent.tools")
BACKOFF_5XX_S: Final = 1.0  # before retrying a 502, 503, 504 or a dropped connection
MIN_RETRY_BUDGET_S: Final = 1.0  # a retry needs at least this much of STEP_CAP_S left
RUNS_REMEMBERED: Final = 4096

Jitter = Callable[[], float]  # a value in [0, 1), spreading the 429 backoff over 10-20 s


@dataclass(frozen=True)
class Clock:
    """Wall time for startedAt, monotonic time for budgets, and the backoff's sleep (tests inject all three)."""

    now: Callable[[], datetime] = utc_now
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)


class CallBudget:
    """At most `limit` tool calls per plan version of a run (A4 "Runaway load or cost"), counted per ToolClient.call.

    Counted like the compiler's 20-step check and the executor's own count: per plan version, so an edit
    (version n+1 of the same run) starts its own budget instead of inheriting the calls of the versions before.
    """

    def __init__(self, limit: int = MAX_TOOL_CALLS_PER_RUN, *, remember: int = RUNS_REMEMBERED) -> None:
        self.limit = limit
        self._used: OrderedDict[tuple[str, int], int] = OrderedDict()
        self._remember = remember

    def take(self, run_id: str, plan_version: int) -> bool:
        key = (run_id, plan_version)
        used = self._used.pop(key, 0)
        self._used[key] = used + 1 if used < self.limit else used
        while len(self._used) > self._remember:
            self._used.popitem(last=False)
        return used < self.limit

    def used(self, run_id: str, plan_version: int) -> int:
        return self._used.get((run_id, plan_version), 0)


def without_nulls(value: JsonValue) -> JsonValue:
    """The arguments as AgentCore Gateway takes them: every null-valued object key left out, at any depth.

    Gateway validates tools/call arguments against the target's tool schema, which cannot say "or
    null" (orion/infra/gateway_tools.py drops the null branch and leaves nullable properties out of
    `required`), so an explicit null is refused before the Lambda runs ("Field '/scope' has invalid
    type: null found, object expected"). Every nullable Input field defaults to None, so the Lambda
    reads an omitted field as null. Only the wire form changes: the recorded request keeps its nulls.
    """
    if isinstance(value, dict):
        return {key: without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [without_nulls(item) for item in value]
    return value


def resolved_request(name: ServerToolName, input: JsonValue) -> tuple[JsonValue, str]:
    """The request as recorded and sent: the validated Input dumped (camelCase, defaults explicit),
    or the input as given when it does not validate (the tool then answers INVALID_INPUT)."""
    request = input
    try:
        request = tool_spec(name).input_model.model_validate(input).model_dump(mode="json", by_alias=True)
    except ValidationError:
        pass
    try:
        digest = sha256_hex(canonical_json(request, portable=False))
    except (TypeError, ValueError):
        digest = sha256_hex(repr(request))
    return request, digest


class ToolClientBase:
    """The A4 budget around one attempt; subclasses implement `_attempt`."""

    transport_name: Literal["in_process", "gateway"] = "in_process"

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        jitter: Jitter | None = None,
        budget: CallBudget | None = None,
        backoff_5xx_s: float = BACKOFF_5XX_S,
    ) -> None:
        self._clock = clock or Clock()
        self._jitter = jitter or random.random
        self.budget = budget or CallBudget()
        self._backoff_5xx_s = backoff_5xx_s

    @property
    def transport(self) -> Literal["in_process", "gateway"]:
        return self.transport_name

    async def _attempt(self, name: ServerToolName, request: JsonValue, ctx: ToolContext, sink: list[Provenance], budget_s: float) -> ToolResult:
        raise NotImplementedError

    def _backoff_s(self, result: ToolResult) -> float:
        error = result.error
        if error is None:
            return 0.0
        if error.code == "CACHE_BUSY" or error.http_status == 429:
            low, high = BACKOFF_429_S
            return low + min(max(self._jitter(), 0.0), 1.0) * (high - low)
        if error.code == "TIMEOUT":
            return 0.0  # the attempt already waited its budget out
        return self._backoff_5xx_s

    async def _timed(self, name: ServerToolName, request: JsonValue, ctx: ToolContext, sink: list[Provenance], budget_s: float) -> ToolResult:
        try:
            async with asyncio.timeout(budget_s):
                return await self._attempt(name, request, ctx, sink, budget_s)
        except TimeoutError:
            failure = ToolFailure("TIMEOUT", f"{name} did not finish within the {budget_s:.0f} s step timeout", retryable=True)
            return failure_result(failure, sink)
        except (HttpError, McpError, ToolFailure) as error:  # defensive: tools and transports map their own
            failure = error if isinstance(error, ToolFailure) else ToolFailure("HTTP_ERROR", f"{name}: {error}", retryable=False)
            return failure_result(failure, sink)
        except Exception as error:
            LOG.exception("tool %s call %s raised", name, ctx.call_id)
            return failure_result(ToolFailure("INVALID_INPUT", f"{name} failed inside ORION ({type(error).__name__})"), sink)

    async def call(
        self,
        name: ServerToolName,
        input: JsonValue,
        *,
        timeout_s: float,
        run_id: str,
        plan_version: int,
        step_id: str,
        first_call: int,
        on_retry: RetryHook | None = None,
    ) -> RecordedOutcome:
        if name not in SERVER_TOOLS:
            raise ValueError(f"{name!r} is not a server tool")
        if timeout_s <= 0 or first_call < 1 or plan_version < 1:
            raise ValueError("timeout_s > 0, first_call >= 1 and plan_version >= 1")
        spec = tool_spec(name)
        request, request_sha256 = resolved_request(name, input)
        clock = self._clock
        began = clock.monotonic()
        attempts: list[Attempt] = []
        results: list[ToolResult] = []

        def outcome() -> RecordedOutcome:
            final = results[-1]
            observed = final.observed
            return RecordedOutcome(
                tool=name, tool_version=spec.version, transport=self.transport, request=request, request_sha256=request_sha256,
                result=final, response_sha256=None if observed is None else sha256_hex(canonical_json(observed, portable=False)),
                attempts=list(attempts), elapsed_ms=max(0, round((clock.monotonic() - began) * 1000)), attempt_results=list(results),
            )

        def record(number: int, cid: str, started_at: str, started: float, result: ToolResult, backoff_ms: int) -> None:
            status = result.provenance[-1].http_status if result.provenance else None
            http_status = result.error.http_status if result.error is not None and result.error.http_status is not None else status
            attempts.append(
                Attempt(
                    call_id=cid, attempt=number, started_at=started_at, elapsed_ms=max(0, round((clock.monotonic() - started) * 1000)),
                    status=result.status, http_status=http_status, error=result.error, backoff_ms=backoff_ms,
                )
            )
            results.append(result)

        if not self.budget.take(run_id, plan_version):
            failure = ToolFailure(
                "GUARD_FAILED", f"plan version {plan_version} of the run has made its {self.budget.limit} tool calls", limitation="CALL_BUDGET"
            )
            record(1, call_id(run_id, plan_version, step_id, first_call), iso_utc(clock.now()), clock.monotonic(), failure_result(failure, []), 0)
            return outcome()

        for number in range(1, MAX_ATTEMPTS + 1):
            budget_s = min(timeout_s, STEP_CAP_S - (clock.monotonic() - began))
            cid = call_id(run_id, plan_version, step_id, first_call + number - 1)
            started_at, started = iso_utc(clock.now()), clock.monotonic()
            sink: list[Provenance] = []
            ctx = ToolContext(
                run_id=run_id, step_id=step_id, call_id=cid, attempt=number, timeout_s=max(min(budget_s, TOOL_HTTP_TIMEOUT_S), 0.001)
            )
            try:
                result = await self._timed(name, request, ctx, sink, max(budget_s, 0.001))
            except asyncio.CancelledError:
                aborted = ToolResult(status="error", observed=None, provenance=list(sink), error=cancelled_error(), limitation=None)
                record(number, cid, started_at, started, aborted, 0)
                raise ToolCallCancelled(outcome()) from None
            error = result.error
            retry = (
                number < MAX_ATTEMPTS and spec.retryable and result.status == "error" and error is not None and error.retryable
            )
            backoff_s = self._backoff_s(result) if retry else 0.0
            if retry and STEP_CAP_S - (clock.monotonic() - began) - backoff_s < MIN_RETRY_BUDGET_S:
                retry, backoff_s = False, 0.0
            backoff_ms = round(backoff_s * 1000)
            record(number, cid, started_at, started, result, backoff_ms)
            if not retry:
                break
            try:
                if on_retry is not None:
                    await on_retry(number + 1, backoff_ms)
                if backoff_s > 0:
                    await clock.sleep(backoff_s)
            except asyncio.CancelledError:
                raise ToolCallCancelled(outcome()) from None
        return outcome()


class InProcessToolClient(ToolClientBase):
    """Mode L: the server tools run in this process (the same handlers the Gateway Lambda runs)."""

    transport_name = "in_process"

    def __init__(
        self,
        tools: Mapping[ServerToolName, ServerTool],
        *,
        clock: Clock | None = None,
        jitter: Jitter | None = None,
        budget: CallBudget | None = None,
        backoff_5xx_s: float = BACKOFF_5XX_S,
    ) -> None:
        super().__init__(clock=clock, jitter=jitter, budget=budget, backoff_5xx_s=backoff_5xx_s)
        missing = SERVER_TOOLS - set(tools)
        if missing:
            raise ValueError(f"no handler for {', '.join(sorted(missing))}")
        self._tools = dict(tools)

    async def _attempt(self, name: ServerToolName, request: JsonValue, ctx: ToolContext, sink: list[Provenance], budget_s: float) -> ToolResult:
        return await self._tools[name].run(request, ctx, sink)


class GatewayToolClient(ToolClientBase):
    """Modes A and H: `vlab___<tool>` on AgentCore Gateway; kg_query only once its target exists."""

    transport_name = "gateway"

    def __init__(
        self,
        mcp: McpGatewayClient,
        *,
        local: Mapping[ServerToolName, ServerTool] | None = None,
        kg_registered: bool = False,
        clock: Clock | None = None,
        jitter: Jitter | None = None,
        budget: CallBudget | None = None,
        backoff_5xx_s: float = BACKOFF_5XX_S,
    ) -> None:
        super().__init__(clock=clock, jitter=jitter, budget=budget, backoff_5xx_s=backoff_5xx_s)
        self._mcp = mcp
        self._local = dict(local or {})
        self._kg_registered = kg_registered

    async def _attempt(self, name: ServerToolName, request: JsonValue, ctx: ToolContext, sink: list[Provenance], budget_s: float) -> ToolResult:
        if name in self._local:
            return await self._local[name].run(request, ctx, sink)
        if name == "kg_query" and not self._kg_registered:
            failure = ToolFailure(
                "NOT_CONNECTED", "kg_target_registered: the knowledge-graph target is not registered",
                limitation="KG_NOT_CONNECTED", status="unavailable",
            )
            return failure_result(failure, sink)
        try:
            reply = await self._mcp.call_tool(gateway_tool_name(name), without_nulls(request), timeout_s=budget_s, clock=self._clock.monotonic)
        except HttpTimeout:
            return failure_result(ToolFailure("TIMEOUT", f"the Gateway did not answer {name} in time", retryable=True), sink)
        except HttpError as error:
            return failure_result(ToolFailure("HTTP_ERROR", f"the Gateway call for {name} failed: {error}", retryable=True), sink)
        except McpError as error:
            status = error.http_status
            if error.rpc_code is not None:
                return failure_result(ToolFailure("INVALID_INPUT", f"the Gateway refused {name}: {error}"), sink)
            retryable = status in (429, 502, 503, 504) or status is None
            return failure_result(
                ToolFailure("HTTP_ERROR", f"the Gateway call for {name} failed: {error}", retryable=retryable, http_status=status), sink
            )
        result = parse_tool_result(reply.result)
        if result is None:
            if reply.result.get("isError") is True:
                text = mcp_error_text(reply.result)
                return failure_result(ToolFailure("HTTP_ERROR", f"the Gateway target failed for {name}: {text}"), sink)
            return failure_result(ToolFailure("UPSTREAM_SHAPE", f"the Gateway answered {name} without a ToolResult"), sink)
        sink.extend(result.provenance)
        model = tool_spec(name).observed_model
        if result.status in ("ok", "empty") and model is not None:
            # The Lambda is another deployment: its Observed must still be this catalog's.
            try:
                model.model_validate(result.observed)
            except ValidationError:
                return failure_result(ToolFailure("UPSTREAM_SHAPE", f"the Gateway's {name} output does not match the catalog"), sink)
        return result


def credential_for(config: ToolConfig, origin: str, region: str) -> CredentialSource:
    """The Basic credential source for a hosted sandbox: Secrets Manager, else the access file."""
    if config.basic_secret_arn:
        return SecretsManagerCredential(config.basic_secret_arn, origin, reader=boto3_secret_reader(region))
    if config.access_file is not None:
        return AccessFileCredential(config.access_file, origin)
    return NoCredential()


def make_tool_client(
    settings: Settings,
    *,
    config: ToolConfig | None = None,
    http: HttpTransport | None = None,
    goldens: GoldenStore | None = None,
    aws_credentials: CredentialsProvider | None = None,
    clock: Clock | None = None,
    jitter: Jitter | None = None,
) -> ToolClient:
    """The ToolClient for this deployment, by `settings.tool_transport` (ORION_TOOLS): in-process
    (mode L, and cut 1 on AWS) or AgentCore Gateway (modes A and H). Faults from settings.faults
    (already gated to mode L or a test Runtime) replace the faulted tools' HTTP calls. The keyword
    arguments are for tests."""
    config = config or ToolConfig.from_env()
    goldens = goldens or GoldenStore.load(config.golden_dir)
    http = http or H11Transport()
    faults: dict[ServerToolName, str] = {cast(ServerToolName, name): kind for name, kind in settings.faults.items() if name in SERVER_TOOLS}
    if faults and settings.mode != "L" and not settings.test_runtime:
        raise ValueError("ORION_FAULTS is honoured only in mode L or a test Runtime")
    if settings.tool_transport == "in_process":
        vlab = VlabClient(settings.vlab_origin, http, credential=credential_for(config, settings.vlab_origin, settings.aws_region))

        def per_tool(name: ServerToolName) -> VlabClient:
            return vlab.with_transport(FaultTransport(faults[name], fallback=http)) if name in faults else vlab

        return InProcessToolClient(build_server_tools(per_tool, goldens), clock=clock, jitter=jitter)
    if not config.gateway_url:
        raise GatewayConfigError("ORION_GATEWAY_URL is required when tools go through Gateway")
    signer = BotocoreSigner(settings.aws_region, aws_credentials or default_credentials)
    mcp = McpGatewayClient(config.gateway_url, http, signer, client_version=AGENT_VERSION)
    local: dict[ServerToolName, ServerTool] = {}
    if faults:
        offline = VlabClient(settings.vlab_origin, FaultTransport("error"))
        tools = build_server_tools(lambda name: offline.with_transport(FaultTransport(faults[name])) if name in faults else offline, goldens)
        local = {name: tools[name] for name in faults}
    return GatewayToolClient(mcp, local=local, kg_registered=config.kg_registered, clock=clock, jitter=jitter)
