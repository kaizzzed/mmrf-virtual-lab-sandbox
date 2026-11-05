"""MCP over streamable HTTP to AgentCore Gateway, SigV4-signed (RFA rev 25, A2 "Tools", A4 mode A).

The Gateway exposes the nine server tools as `vlab___<tool>` (inbound auth AWS_IAM, so every POST
is signed for the service `bedrock-agentcore` with botocore's SigV4 signer, which the image
already has). One client keeps one MCP session: `initialize`, then `notifications/initialized`,
then `tools/call` per tool call. A response is JSON or a server-sent event stream; both are read
in full (a tool result is at most 6 MB). A 404 on a session the server has dropped starts a new
session once. Credentials come from botocore's chain at run time (the Runtime's execution role)
and are never logged; tests inject them.

What the Gateway Lambda returns is the ToolResult JSON (camelCase); Gateway carries it as the
tool result's text content (or structuredContent). `parse_tool_result` reads it back.
"""

import asyncio
import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from pydantic import JsonValue, ValidationError

from .base import ToolResult
from .http import HttpResponse, HttpTransport

SIGV4_SERVICE: Final = "bedrock-agentcore"
MCP_PROTOCOL_VERSION: Final = "2025-06-18"
SESSION_HEADER: Final = "mcp-session-id"
PROTOCOL_HEADER: Final = "MCP-Protocol-Version"
MAX_GATEWAY_RESPONSE_BYTES: Final = 8 * 1024 * 1024
_LOOPBACK: Final = frozenset({"localhost", "127.0.0.1", "::1"})


class GatewayConfigError(ValueError):
    """ORION_GATEWAY_URL is not an https AgentCore Gateway URL (or http on loopback, for tests)."""


def validate_gateway_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.username or parts.password or parts.query or parts.fragment or not host:
        raise GatewayConfigError("ORION_GATEWAY_URL must be a bare URL without credentials or a query")
    if parts.scheme == "https" and host.endswith(".amazonaws.com"):
        return url
    if parts.scheme == "http" and host in _LOOPBACK:
        return url
    raise GatewayConfigError("ORION_GATEWAY_URL must be an https *.amazonaws.com URL (http only on loopback, for tests)")


class Signer(Protocol):
    def sign(self, method: str, url: str, headers: Sequence[tuple[str, str]], body: bytes) -> list[tuple[str, str]]: ...


CredentialsProvider = Callable[[], Any]


def default_credentials() -> Any:
    """botocore's credential chain (environment, container role, instance role). AWS only."""
    import botocore.session  # type: ignore[import-untyped]

    credentials = botocore.session.get_session().get_credentials()
    if credentials is None:
        raise GatewayConfigError("no AWS credentials for signing Gateway calls")
    return credentials


class BotocoreSigner:
    """SigV4 for `bedrock-agentcore` in `region`, by botocore.auth.SigV4Auth."""

    def __init__(self, region: str, credentials: CredentialsProvider = default_credentials, *, service: str = SIGV4_SERVICE) -> None:
        self._region = region
        self._credentials = credentials
        self._service = service

    def sign(self, method: str, url: str, headers: Sequence[tuple[str, str]], body: bytes) -> list[tuple[str, str]]:
        from botocore.auth import SigV4Auth  # type: ignore[import-untyped]
        from botocore.awsrequest import AWSRequest  # type: ignore[import-untyped]

        source = self._credentials()
        frozen = source.get_frozen_credentials() if hasattr(source, "get_frozen_credentials") else source
        request = AWSRequest(method=method, url=url, data=body, headers=dict(headers))
        SigV4Auth(frozen, self._service, self._region).add_auth(request)
        return [(str(name), str(value)) for name, value in request.headers.items()]


class McpError(Exception):
    """The Gateway answered, but not with a usable JSON-RPC response."""

    def __init__(self, message: str, *, http_status: int | None = None, rpc_code: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.rpc_code = rpc_code


@dataclass(frozen=True)
class McpCallResult:
    http_status: int
    result: dict[str, JsonValue]


def _sse_messages(text: str) -> list[JsonValue]:
    """The JSON payload of each `message` event (or unnamed event) in an event stream."""
    messages: list[JsonValue] = []
    data: list[str] = []
    event = "message"
    for raw in text.replace("\r\n", "\n").split("\n") + [""]:
        if raw == "":
            if data and event == "message":
                try:
                    messages.append(json.loads("\n".join(data)))
                except ValueError:
                    pass
            data, event = [], "message"
        elif raw.startswith(":"):
            continue
        else:
            name, _, value = raw.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "data":
                data.append(value)
            elif name == "event":
                event = value or "message"
    return messages


def _rpc_response(response: HttpResponse, request_id: int) -> dict[str, JsonValue]:
    if response.media_type == "text/event-stream":
        candidates = _sse_messages(response.body.decode("utf-8", errors="replace"))
    else:
        try:
            parsed: JsonValue = json.loads(response.body)
        except ValueError as error:
            raise McpError("the Gateway response is not JSON", http_status=response.status) from error
        candidates = parsed if isinstance(parsed, list) else [parsed]
    for message in candidates:
        if isinstance(message, dict) and message.get("id") == request_id and ("result" in message or "error" in message):
            return message
    raise McpError(f"no JSON-RPC response for request {request_id}", http_status=response.status)


class McpGatewayClient:
    """One MCP session with the Gateway. Safe for concurrent calls (the session opens once)."""

    def __init__(
        self,
        url: str,
        transport: HttpTransport,
        signer: Signer,
        *,
        client_name: str = "orion-agent",
        client_version: str = "1",
        protocol_version: str = MCP_PROTOCOL_VERSION,
    ) -> None:
        self.url = validate_gateway_url(url)
        self._transport = transport
        self._signer = signer
        self._client_info = {"name": client_name, "version": client_version}
        self._protocol_version = protocol_version
        self._negotiated: str | None = None
        self._session_id: str | None = None
        self._ids = itertools.count(1)
        self._session_lock: asyncio.Lock | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._session_lock is None or self._session_loop is not loop:
            self._session_lock, self._session_loop = asyncio.Lock(), loop
        return self._session_lock

    async def _post(self, message: Mapping[str, JsonValue], *, timeout_s: float) -> HttpResponse:
        body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers: list[tuple[str, str]] = [
            ("Content-Type", "application/json"),
            ("Accept", "application/json, text/event-stream"),
        ]
        if self._negotiated is not None:
            headers.append((PROTOCOL_HEADER, self._negotiated))
        if self._session_id is not None:
            headers.append(("Mcp-Session-Id", self._session_id))
        signed = self._signer.sign("POST", self.url, headers, body)
        return await self._transport.send("POST", self.url, signed, body, timeout_s=timeout_s, max_bytes=MAX_GATEWAY_RESPONSE_BYTES)

    async def _open_session(self, timeout_s: float) -> None:
        request_id = next(self._ids)
        self._negotiated = None
        self._session_id = None
        response = await self._post(
            {
                "jsonrpc": "2.0", "id": request_id, "method": "initialize",
                "params": {"protocolVersion": self._protocol_version, "capabilities": {}, "clientInfo": dict(self._client_info)},
            },
            timeout_s=timeout_s,
        )
        if response.status != 200:
            raise McpError(f"initialize answered HTTP {response.status}", http_status=response.status)
        message = _rpc_response(response, request_id)
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpError("initialize failed", http_status=response.status, rpc_code=_rpc_code(message))
        version = result.get("protocolVersion")
        self._negotiated = version if isinstance(version, str) else self._protocol_version
        self._session_id = response.header(SESSION_HEADER)
        notified = await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout_s=timeout_s)
        if notified.status not in (200, 202, 204):
            raise McpError(f"notifications/initialized answered HTTP {notified.status}", http_status=notified.status)

    async def _ensure_session(self, timeout_s: float, *, renew: bool = False) -> None:
        async with self._lock():
            if renew or self._negotiated is None:
                await self._open_session(timeout_s)

    async def call_tool(self, name: str, arguments: JsonValue, *, timeout_s: float, clock: Callable[[], float]) -> McpCallResult:
        """tools/call; raises McpError for a JSON-RPC or HTTP failure, HttpError for no response."""
        deadline = clock() + timeout_s
        await self._ensure_session(timeout_s)
        for renewed in (False, True):
            request_id = next(self._ids)
            message: dict[str, JsonValue] = {
                "jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments},
            }
            response = await self._post(message, timeout_s=max(deadline - clock(), 0.001))
            if response.status == 404 and self._session_id is not None and not renewed:
                await self._ensure_session(max(deadline - clock(), 0.001), renew=True)
                continue
            if response.status != 200:
                raise McpError(f"tools/call answered HTTP {response.status}", http_status=response.status)
            reply = _rpc_response(response, request_id)
            result = reply.get("result")
            if not isinstance(result, dict):
                error = reply.get("error")
                text = error.get("message") if isinstance(error, dict) else None
                raise McpError(f"tools/call failed: {str(text)[:200]}", http_status=200, rpc_code=_rpc_code(reply))
            return McpCallResult(http_status=200, result=result)
        raise McpError("the Gateway dropped the MCP session twice", http_status=404)


def _rpc_code(message: Mapping[str, JsonValue]) -> int | None:
    error = message.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def parse_tool_result(result: Mapping[str, JsonValue]) -> ToolResult | None:
    """The Lambda's ToolResult from an MCP tool result: structuredContent, else a text content item."""
    candidates: list[JsonValue] = []
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        candidates.append(structured)
    content = result.get("content")
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            try:
                candidates.append(json.loads(str(item["text"])))
            except ValueError:
                continue
    for candidate in candidates:
        if isinstance(candidate, dict) and "status" in candidate:
            try:
                return ToolResult.model_validate(candidate)
            except ValidationError:
                continue
    return None


def error_text(result: Mapping[str, JsonValue]) -> str:
    """The text an MCP error result carries (isError true), shortened."""
    content = result.get("content")
    texts = [str(item.get("text")) for item in content if isinstance(item, dict) and item.get("type") == "text"] if isinstance(content, list) else []
    return " ".join(texts)[:300] or "no detail"
