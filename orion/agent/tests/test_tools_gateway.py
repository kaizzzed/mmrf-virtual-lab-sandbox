"""The Gateway ToolClient: MCP over streamable HTTP, SigV4-signed with botocore, against a local fake
MCP server that checks every signature. Credentials are random per test run; nothing reaches AWS."""

import asyncio
import json
import secrets
import unittest
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, TypeVar

from botocore.auth import SigV4Auth  # type: ignore[import-untyped]
from botocore.awsrequest import AWSRequest  # type: ignore[import-untyped]
from botocore.credentials import Credentials  # type: ignore[import-untyped]
from test_tools_fakes import RUN_ID, FakeTransport, LocalServer, ManualClock, Received, Reply, goldens

from orion_agent.tools.base import ToolResult
from orion_agent.tools.client import GatewayToolClient
from orion_agent.tools.gateway import (
    BotocoreSigner,
    GatewayConfigError,
    McpGatewayClient,
    parse_tool_result,
    validate_gateway_url,
)
from orion_agent.tools.http import H11Transport
from orion_agent.tools.records import ToolCallCancelled, call_records

T = TypeVar("T")
REGION = "us-east-1"
HIGH: dict[str, Any] = {"and": [{"in": {"cgs_risk_key_criteria.cgs_risk_category": ["High risk"]}}]}
PROVENANCE = {"method": "POST", "path": "/guppy/graphql", "requestSha256": "a" * 64, "httpStatus": 200, "responseSha256": "b" * 64, "elapsedMs": 12}


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def fake_credentials(token: bool = False) -> Credentials:
    return Credentials("ORIONTEST" + secrets.token_hex(6).upper(), secrets.token_urlsafe(30), secrets.token_urlsafe(20) if token else None)


def signature_ok(received: Received, url: str, credentials: Credentials) -> bool:
    """Recompute the SigV4 signature from what arrived, as AWS would."""
    authorization = received.headers.get("authorization", "")
    if not authorization.startswith("AWS4-HMAC-SHA256 "):
        return False
    fields = dict(part.strip().split("=", 1) for part in authorization[len("AWS4-HMAC-SHA256 "):].split(","))
    signed = fields["SignedHeaders"].split(";")
    request = AWSRequest(method=received.method, url=url, data=received.body, headers={h: received.headers[h] for h in signed})
    request.context["timestamp"] = received.headers["x-amz-date"]
    auth = SigV4Auth(credentials, "bedrock-agentcore", REGION)
    expected = auth.signature(auth.string_to_sign(request, auth.canonical_request(request)), request)
    return bool(fields["Signature"] == expected and fields["Credential"].endswith(f"/{REGION}/bedrock-agentcore/aws4_request"))


def tool_result(observed: Any, status: str = "ok", **extra: Any) -> dict[str, Any]:
    return {"status": status, "observed": observed, "warnings": [], "limitation": None, "provenance": [PROVENANCE], "error": None, **extra}


@dataclass
class FakeGateway:
    """An MCP server that checks SigV4 on every POST and answers from a script of tools/call replies."""

    credentials: Credentials
    replies: list[Callable[[dict[str, Any]], Reply]] = field(default_factory=list)
    sse: bool = False
    session: str = "mcp-session-1"
    url: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    initializations: int = 0
    rejected: int = 0
    drop_session_once: bool = False

    def reply(self, message: dict[str, Any], status: int = 200, headers: list[tuple[str, str]] | None = None) -> Reply:
        body = json.dumps(message).encode()
        if self.sse:
            return Reply(status, [("Content-Type", "text/event-stream"), *(headers or [])], b": ping\n\nevent: message\ndata: " + body + b"\n\n")
        return Reply(status, [("Content-Type", "application/json"), *(headers or [])], body)

    def __call__(self, received: Received) -> Reply:
        if not signature_ok(received, self.url, self.credentials):
            self.rejected += 1
            return Reply(403, [("Content-Type", "application/json")], b'{"message":"The request signature we calculated does not match"}')
        message = json.loads(received.body)
        method = message.get("method")
        if method == "initialize":
            self.initializations += 1
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-gateway", "version": "1"}}
            return self.reply({"jsonrpc": "2.0", "id": message["id"], "result": result}, headers=[("Mcp-Session-Id", self.session)])
        if method == "notifications/initialized":
            return Reply(202)
        assert method == "tools/call", method
        assert received.headers.get("mcp-session-id") == self.session
        assert received.headers.get("mcp-protocol-version") == "2025-06-18"
        if self.drop_session_once:
            self.drop_session_once = False
            self.session = "mcp-session-2"
            return Reply(404)
        self.calls.append(message["params"])
        responder = self.replies.pop(0) if self.replies else (lambda m: self.reply({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": json.dumps(tool_result({"count": 7}))}], "isError": False}}))
        return responder(message)


def text_result(gateway: FakeGateway, payload: Any, *, is_error: bool = False) -> Callable[[dict[str, Any]], Reply]:
    return lambda m: gateway.reply({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error}})


async def with_gateway(gateway: FakeGateway, body: Callable[[GatewayToolClient], Coroutine[Any, Any, T]], *, signer_credentials: Credentials | None = None,
                       clock: ManualClock | None = None, **options: Any) -> T:
    async with LocalServer(gateway) as server:
        gateway.url = server.url("/mcp")
        creds = signer_credentials or gateway.credentials
        mcp = McpGatewayClient(gateway.url, H11Transport(), BotocoreSigner(REGION, lambda: creds))
        clock = clock or ManualClock()
        client = GatewayToolClient(mcp, clock=clock.clock(), **options)
        return await body(client)


async def count(client: GatewayToolClient, step: str = "s2") -> Any:
    return await client.call("cohort_count", {"filter": HIGH}, timeout_s=60, run_id=RUN_ID, plan_version=1, step_id=step, first_call=1)


class GatewayClientTests(unittest.TestCase):
    def test_a_signed_tools_call_returns_the_lambdas_tool_result(self) -> None:
        gateway = FakeGateway(fake_credentials(token=True))
        outcome = run(with_gateway(gateway, count))
        self.assertEqual((outcome.transport, outcome.result.status, outcome.result.observed), ("gateway", "ok", {"count": 7}))
        self.assertEqual(gateway.calls, [{"name": "vlab___cohort_count", "arguments": {"filter": HIGH}}])
        self.assertEqual(outcome.result.provenance[0].path, "/guppy/graphql")
        self.assertEqual((gateway.initializations, gateway.rejected), (1, 0))
        records = call_records(outcome, plan_version=1, step_id="s2")
        self.assertEqual((records[0].transport, len(records[0].provenance)), ("gateway", 1))

    def test_an_event_stream_answer_and_one_session_for_many_calls(self) -> None:
        gateway = FakeGateway(fake_credentials(), sse=True)

        async def two(client: GatewayToolClient) -> list[Any]:
            return [await count(client, "s2"), await count(client, "s3")]

        outcomes = run(with_gateway(gateway, two))
        self.assertEqual([o.result.observed for o in outcomes], [{"count": 7}, {"count": 7}])
        self.assertEqual((gateway.initializations, len(gateway.calls)), (1, 2))

    def test_a_dropped_session_is_reopened_once(self) -> None:
        gateway = FakeGateway(fake_credentials(), drop_session_once=True)
        outcome = run(with_gateway(gateway, count))
        self.assertEqual((outcome.result.status, gateway.initializations, len(outcome.attempts)), ("ok", 2, 1))

    def test_a_bad_signature_is_refused_and_not_retried(self) -> None:
        gateway = FakeGateway(fake_credentials())
        outcome = run(with_gateway(gateway, count, signer_credentials=fake_credentials()))
        self.assertEqual((outcome.result.error and outcome.result.error.code, outcome.result.error and outcome.result.error.http_status), ("HTTP_ERROR", 403))
        self.assertEqual((gateway.rejected, len(outcome.attempts)), (1, 1))

    def test_a_retryable_lambda_error_is_retried_with_the_same_arguments(self) -> None:
        gateway = FakeGateway(fake_credentials())
        busy = tool_result(None, "error", error={"code": "CACHE_BUSY", "message": "busy", "httpStatus": 429, "retryable": True}, limitation="TOOL_FAILED")
        gateway.replies = [text_result(gateway, busy)]
        clock = ManualClock()
        outcome = run(with_gateway(gateway, count, clock=clock, jitter=lambda: 0.0))
        self.assertEqual(([a.status for a in outcome.attempts], clock.sleeps), (["error", "ok"], [10.0]))
        self.assertEqual(gateway.calls[0], gateway.calls[1])

    def test_gateway_http_failures_retry_and_rpc_errors_do_not(self) -> None:
        gateway = FakeGateway(fake_credentials())
        gateway.replies = [lambda m: Reply(503, [("Content-Type", "application/json")], b"{}")]
        outcome = run(with_gateway(gateway, count))
        self.assertEqual([a.status for a in outcome.attempts], ["error", "ok"])
        self.assertEqual(outcome.attempts[0].http_status, 503)
        gateway = FakeGateway(fake_credentials())
        gateway.replies = [lambda m: gateway.reply({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32602, "message": "Unknown tool"}})]
        outcome = run(with_gateway(gateway, count))
        self.assertEqual((outcome.result.error and outcome.result.error.code, len(outcome.attempts)), ("INVALID_INPUT", 1))

    def test_a_target_error_without_a_tool_result_is_an_http_error(self) -> None:
        gateway = FakeGateway(fake_credentials())
        gateway.replies = [text_result(gateway, "Lambda function timed out", is_error=True)]
        outcome = run(with_gateway(gateway, count))
        self.assertEqual((outcome.result.error and outcome.result.error.code, len(outcome.attempts)), ("HTTP_ERROR", 1))
        self.assertIn("timed out", outcome.result.error.message if outcome.result.error else "")
        gateway = FakeGateway(fake_credentials())
        gateway.replies = [text_result(gateway, {"unexpected": True})]
        outcome = run(with_gateway(gateway, count))
        self.assertEqual(outcome.result.error and outcome.result.error.code, "UPSTREAM_SHAPE")

    def test_an_output_that_is_not_the_catalogs_is_refused(self) -> None:
        gateway = FakeGateway(fake_credentials())
        gateway.replies = [text_result(gateway, tool_result({"count": -1})), text_result(gateway, tool_result({"total": 7}))]

        async def two(client: GatewayToolClient) -> list[Any]:
            return [await count(client, "s2"), await count(client, "s3")]

        outcomes = run(with_gateway(gateway, two))
        self.assertEqual([o.result.error and o.result.error.code for o in outcomes], ["UPSTREAM_SHAPE", "UPSTREAM_SHAPE"])
        self.assertEqual([len(o.attempts) for o in outcomes], [1, 1])

    def test_kg_query_before_its_target_makes_no_call(self) -> None:
        gateway = FakeGateway(fake_credentials())

        async def kg(client: GatewayToolClient) -> Any:
            return await client.call("kg_query", {"question": "q"}, timeout_s=60, run_id=RUN_ID, plan_version=1, step_id="s3", first_call=1)

        outcome = run(with_gateway(gateway, kg))
        self.assertEqual((outcome.result.status, outcome.result.limitation, gateway.initializations), ("unavailable", "KG_NOT_CONNECTED", 0))

    def test_cancelling_a_gateway_call_records_it_cancelled(self) -> None:
        gateway = FakeGateway(fake_credentials())

        async def handler(received: Received) -> Reply:
            if json.loads(received.body).get("method") == "tools/call":
                await asyncio.sleep(30)
                return Reply(200)
            return gateway(received)

        async def go() -> ToolCallCancelled:
            async with LocalServer(handler) as server:
                gateway.url = server.url("/mcp")
                mcp = McpGatewayClient(gateway.url, H11Transport(), BotocoreSigner(REGION, lambda: gateway.credentials))
                task = asyncio.create_task(count(GatewayToolClient(mcp)))
                await asyncio.sleep(0.3)
                task.cancel()
                try:
                    await task
                except ToolCallCancelled as error:
                    return error
                raise AssertionError("not cancelled")

        error = run(go())
        self.assertEqual(error.outcome.attempts[-1].error.code, "CANCELLED")  # type: ignore[union-attr]
        self.assertEqual(gateway.initializations, 1)


class GatewayPartsTests(unittest.TestCase):
    def test_gateway_urls_are_https_amazonaws_or_loopback_http(self) -> None:
        validate_gateway_url("https://orion-gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
        validate_gateway_url("http://127.0.0.1:8000/mcp")
        for bad in ("http://gw.amazonaws.com/mcp", "https://example.com/mcp", "https://u:p@gw.amazonaws.com/mcp"):
            with self.assertRaises(GatewayConfigError):
                validate_gateway_url(bad)

    def test_the_signer_signs_for_bedrock_agentcore_with_the_session_token(self) -> None:
        creds = fake_credentials(token=True)
        headers = dict(BotocoreSigner(REGION, lambda: creds).sign("POST", "https://gw.amazonaws.com/mcp", [("Content-Type", "application/json")], b"{}"))
        self.assertTrue(headers["Authorization"].startswith(f"AWS4-HMAC-SHA256 Credential={creds.access_key}/"))
        self.assertIn("/us-east-1/bedrock-agentcore/aws4_request", headers["Authorization"])
        self.assertEqual(headers["X-Amz-Security-Token"], creds.token)
        self.assertIn("X-Amz-Date", headers)

    def test_parse_tool_result_reads_structured_content_or_text(self) -> None:
        payload = tool_result({"count": 1})
        self.assertIsInstance(parse_tool_result({"structuredContent": payload}), ToolResult)
        self.assertIsInstance(parse_tool_result({"content": [{"type": "text", "text": "not json"}, {"type": "text", "text": json.dumps(payload)}]}), ToolResult)
        self.assertIsNone(parse_tool_result({"content": [{"type": "text", "text": "{}"}]}))

    def test_faulted_tools_run_locally_and_make_no_gateway_call(self) -> None:
        from orion_agent.tools.faults import FaultTransport
        from orion_agent.tools.server import build_server_tools
        from orion_agent.tools.vlab import VlabClient

        transport = FakeTransport()
        offline = VlabClient("https://d1abc.cloudfront.net", FaultTransport("empty"))
        local = {"cohort_count": build_server_tools(offline, goldens())["cohort_count"]}
        mcp = McpGatewayClient("http://127.0.0.1:9/mcp", transport, BotocoreSigner(REGION, fake_credentials))
        outcome = run(count(GatewayToolClient(mcp, local=local)))  # type: ignore[arg-type]
        self.assertEqual((outcome.result.status, outcome.result.observed, transport.sent), ("empty", {"count": 0}, []))


if __name__ == "__main__":
    unittest.main()
