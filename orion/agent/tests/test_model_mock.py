"""A scripted Bedrock for the model-role tests: the real Anthropic SDK and its middlewares over an
in-process mock transport, so no test reaches a model endpoint (no network, no credential).

Skipped when the SDK is not installed (orion-agent:dev); run under orion-agent:model.
"""

import asyncio
import importlib.util
import json
import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

HAS_SDK = importlib.util.find_spec("anthropic") is not None
SKIP_REASON = "the Anthropic SDK is not installed (use the orion-agent:model image)"
BASE_URL = "http://bedrock.invalid/anthropic"
PRIMARY = "anthropic.claude-opus-5-5"
FALLBACK = "anthropic.claude-opus-5"
RUN_ID = "0123456789abcdef0123456789abcdef"


def message(model: str, text: str | None, stop_reason: str = "end_turn", *, cache_read: int = 7120, cache_write: int = 0) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [] if text is None else [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": None,
        "usage": {"input_tokens": 840, "output_tokens": 312, "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write},
    }


def refusal(model: str, category: str = "bio") -> dict[str, Any]:
    body = message(model, None, "refusal", cache_read=0)
    body["stop_details"] = {"type": "refusal", "category": category, "explanation": None}
    return body


Reply = dict[str, Any] | int | Exception | Callable[[], Awaitable[Any]]


def status_reply(code: int, *, retry_after_s: float) -> Callable[[], Awaitable[Any]]:
    """An error status whose retry-after keeps the SDK in its backoff (anyio.sleep) for `retry_after_s`."""

    async def reply() -> Any:
        import httpx2

        body = {"type": "error", "error": {"type": "api_error", "message": "scripted"}}
        return httpx2.Response(code, json=body, headers={"retry-after-ms": str(int(retry_after_s * 1000))})

    return reply


@dataclass
class ScriptedBedrock:
    """Replies per model, in order. A dict is a 200 message, an int an error status, an exception is raised;
    a callable is awaited for a 200 message body or a whole httpx2.Response."""

    script: dict[str, list[Reply]]
    requests: list[dict[str, Any]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)

    async def handle(self, request: Any) -> Any:
        import httpx2

        body = json.loads(request.content)
        self.requests.append(body)
        self.urls.append(str(request.url))
        self.headers.append(dict(request.headers))
        # Mantle carries the model in the body; bedrock-runtime moves it into /model/<id>/invoke.
        url = str(request.url)
        model = str(body.get("model") or url.split("/model/", 1)[-1].split("/", 1)[0])
        queue = self.script.get(model) or []
        if not queue:
            return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": f"no reply scripted for {model}"}})
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            kind = "rate_limit_error" if reply == 429 else "api_error"
            return httpx2.Response(reply, json={"type": "error", "error": {"type": kind, "message": "scripted"}}, headers={"retry-after-ms": "1"})
        if callable(reply):
            made = await reply()
            return made if isinstance(made, httpx2.Response) else httpx2.Response(200, json=made)
        return httpx2.Response(200, json=reply)

    def client(self, *, fallback: str | None = FALLBACK, max_retries: int = 0) -> Any:
        import httpx2
        from anthropic import DefaultAsyncHttpxClient

        from orion_agent.model.client import ModelClient

        return ModelClient(
            region="us-east-1", fallback_model=fallback, max_retries=max_retries, kind="mantle", base_url=BASE_URL, skip_auth=True,
            http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handle)),
        )


def config(**values: Any) -> Any:
    from orion_agent.model.config import ModelConfig

    return ModelConfig(planner="bedrock", composer="bedrock", bedrock_client="mantle", max_retries=0, **values)


def run(coroutine: Awaitable[Any]) -> Any:
    async def main() -> Any:
        return await coroutine

    return asyncio.run(main())


@unittest.skipUnless(HAS_SDK, SKIP_REASON)
class ScriptedBedrockTest(unittest.TestCase):
    def test_the_mock_serves_the_real_sdk_with_no_network(self) -> None:
        from orion_agent.model.calls import model_call_scope
        from orion_agent.model.client import ModelRequest

        bedrock = ScriptedBedrock({PRIMARY: [message(PRIMARY, '{"ok": true}')]})
        client = bedrock.client()
        request = ModelRequest(
            role="planner", model=PRIMARY, effort="low", system_prefix="rules", user_text="question",
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
            max_tokens=16000, timeout_s=5,
        )

        async def call() -> Any:
            with model_call_scope(RUN_ID):
                return await client.call(request, json.loads)

        result = run(call())
        self.assertEqual(result.value, {"ok": True})
        self.assertEqual(len(bedrock.urls), 1)
        self.assertTrue(bedrock.urls[0].startswith(f"{BASE_URL}/v1/messages"))


if __name__ == "__main__":
    unittest.main()
