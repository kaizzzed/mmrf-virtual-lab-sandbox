"""The Gateway Lambda target `vlab` (function orion-vlab-tools; RFA rev 25 A2 "Tools", A4 "Deployment").

AgentCore Gateway invokes this function for `vlab___<tool>` with the tool's arguments as the event
and the tool name in the client context (`bedrockAgentCoreToolName`, `<target>___<tool>`). The
handler strips the target prefix, runs the same server tool the agent runs in-process (one
attempt: the agent's ToolClient owns the retry and the backoff), and returns the ToolResult JSON
(camelCase) within MAX_TOOL_RESPONSE_BYTES, which Gateway hands back as the tool result.

Environment:

| Variable | Meaning |
| --- | --- |
| `ORION_VLAB_ORIGIN` | The sandbox's CloudFront origin (`https://<id>.cloudfront.net`) |
| `ORION_VLAB_BASIC_SECRET_ARN` | Secrets Manager ARN of `orion/vlab-basic`: `{"username", "password"}` (optionally `"origin"`) |
| `AWS_REGION` | Set by Lambda |
| `ORION_GOLDEN_DIR` | Optional; the packaged golden snapshot is used without it |

Timeouts (A4): the function's timeout is LAMBDA_TIMEOUT_S (58 s); one tool attempt gets
TOOL_HTTP_TIMEOUT_S (55 s), less if the invocation has less left, so a slow upstream comes back as a
TIMEOUT tool error, never as a Lambda timeout. The Basic credential is read from Secrets Manager at
run time, cached for five minutes, and never logged.
"""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from pydantic import JsonValue

from orion_agent.tools.base import GATEWAY_TARGET, GATEWAY_TOOL_SEPARATOR, LAMBDA_TIMEOUT_S, MAX_TOOL_RESPONSE_BYTES, TOOL_HTTP_TIMEOUT_S
from orion_agent.tools.base import ServerToolName, ToolContext, ToolResult
from orion_agent.tools.credentials import CredentialError, CredentialSource, NoCredential, SecretsManagerCredential, boto3_secret_reader
from orion_agent.tools.goldens import GoldenError, GoldenStore
from orion_agent.tools.http import H11Transport, HttpTransport
from orion_agent.tools.server import VLAB_TOOLS, ServerTool, ToolFailure, build_server_tools, failure_result
from orion_agent.tools.vlab import OriginError, VlabClient

LOG: Final = logging.getLogger("orion_agent.lambda")
TOOL_NAME_KEY: Final = "bedrockAgentCoreToolName"
REQUEST_ID_KEY: Final = "bedrockAgentCoreAwsRequestId"
# Left for serialising the result and returning it once the tool attempt ends.
RETURN_MARGIN_S: Final = 1.0
_TARGET_PREFIX: Final = f"{GATEWAY_TARGET}{GATEWAY_TOOL_SEPARATOR}"


@dataclass
class Runtime:
    """What a warm function reuses across invocations: the tools, their client and the credential."""

    tools: Mapping[ServerToolName, ServerTool]

    @classmethod
    def build(cls, origin: str, credential: CredentialSource, *, transport: HttpTransport | None = None,
              goldens: GoldenStore | None = None) -> "Runtime":
        vlab = VlabClient(origin, transport or H11Transport(), credential=credential)
        return cls(build_server_tools(vlab, goldens or GoldenStore.load()))

    @classmethod
    def from_env(cls) -> "Runtime":
        origin = os.environ.get("ORION_VLAB_ORIGIN", "")
        arn = os.environ.get("ORION_VLAB_BASIC_SECRET_ARN", "")
        credential: CredentialSource = (
            SecretsManagerCredential(arn, origin, reader=boto3_secret_reader(os.environ.get("AWS_REGION"))) if arn else NoCredential()
        )
        return cls.build(origin, credential)


_RUNTIME: Runtime | None = None


def configure(runtime: Runtime | None) -> None:
    """Replace the warm runtime (tests); None rebuilds it from the environment on the next call."""
    global _RUNTIME
    _RUNTIME = runtime


def _runtime() -> Runtime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = Runtime.from_env()
    return _RUNTIME


def tool_name(context: object) -> str | None:
    """The tool Gateway named, with the `vlab___` target prefix stripped; None if it names none."""
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) if client_context is not None else None
    raw = custom.get(TOOL_NAME_KEY) if isinstance(custom, Mapping) else None
    if not isinstance(raw, str) or GATEWAY_TOOL_SEPARATOR not in raw:
        return None
    target, _, name = raw.partition(GATEWAY_TOOL_SEPARATOR)
    return name if f"{target}{GATEWAY_TOOL_SEPARATOR}" == _TARGET_PREFIX else None


def _context(context: object, budget_s: float) -> ToolContext:
    # Gateway passes no ORION run or step id; the tool uses its context only to label its log line.
    request_id = str(getattr(context, "aws_request_id", "") or "gateway")
    run_id = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    return ToolContext(run_id=run_id, step_id="s1", call_id=f"{run_id}.v1.s1.c1", attempt=1, timeout_s=budget_s)


def _budget_s(context: object) -> float:
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    left = remaining() / 1000 if callable(remaining) else LAMBDA_TIMEOUT_S
    return min(TOOL_HTTP_TIMEOUT_S, left - RETURN_MARGIN_S)


def _within_cap(result: ToolResult) -> dict[str, Any]:
    payload: dict[str, Any] = result.model_dump(mode="json", by_alias=True)
    size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size <= MAX_TOOL_RESPONSE_BYTES:
        return payload
    capped = failure_result(
        ToolFailure("UPSTREAM_SHAPE", f"the tool result is {size} bytes, over the {MAX_TOOL_RESPONSE_BYTES}-byte Gateway cap"),
        result.provenance,
    )
    out: dict[str, Any] = capped.model_dump(mode="json", by_alias=True)
    return out


async def run(name: str | None, arguments: JsonValue, context: object) -> ToolResult:
    if name is None or name not in VLAB_TOOLS:
        return failure_result(ToolFailure("INVALID_INPUT", f"{name!r} is not a tool of the {GATEWAY_TARGET} target"), [])
    budget = _budget_s(context)
    if budget <= 0:
        return failure_result(ToolFailure("TIMEOUT", "the invocation had no time left for the tool", retryable=True), [])
    try:
        runtime = _runtime()
    except (OriginError, CredentialError, GoldenError) as error:
        # A misconfigured function answers every call with the reason, never with a bare crash.
        return failure_result(ToolFailure("INVALID_INPUT", f"the {GATEWAY_TARGET} tool function is misconfigured: {error}"), [])
    return await runtime.tools[cast(ServerToolName, name)].run(arguments, _context(context, budget), [])


def handler(event: Any, context: Any) -> dict[str, Any]:
    """The Lambda entry point (`handler.handler`)."""
    name = tool_name(context)
    result = asyncio.run(run(name, event, context))
    LOG.info(
        "%s", json.dumps({
            "tool": name, "status": result.status, "error": result.error.code if result.error else None,
            "limitation": result.limitation, "requests": len(result.provenance),
            "gatewayRequestId": _custom(context).get(REQUEST_ID_KEY),
        }),
    )
    return _within_cap(result)


def _custom(context: object) -> Mapping[str, Any]:
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) if client_context is not None else None
    return custom if isinstance(custom, Mapping) else {}


logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
