"""Doubles for the tool-layer tests: a scripted HTTP transport, a local HTTP server, a manual clock
and a small synthetic stack. No test case lives here; the test_tools_* modules import it."""

import asyncio
import gzip
import json
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit


from orion_agent.tools.client import Clock
from orion_agent.tools.goldens import GoldenStore
from orion_agent.tools.http import HttpError, HttpResponse
from orion_agent.tools.vlab import VlabClient

ORIGIN = "http://identity:8080"
RUN_ID = "0123456789abcdef0123456789abcdef"
LIVE_ORIGIN = os.environ.get("ORION_VLAB_ORIGIN", "http://identity:8080")
_GOLDENS: GoldenStore | None = None


def goldens() -> GoldenStore:
    global _GOLDENS
    if _GOLDENS is None:
        _GOLDENS = GoldenStore.from_snapshot()
    return _GOLDENS


def json_response(status: int, body: Any, headers: Sequence[tuple[str, str]] = ()) -> HttpResponse:
    return HttpResponse(status=status, headers=(("content-type", "application/json"), *headers), body=json.dumps(body).encode())


@dataclass
class SentRequest:
    method: str
    url: str
    headers: list[tuple[str, str]]
    body: bytes | None
    timeout_s: float

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def json(self) -> Any:
        return json.loads(self.body) if self.body else None

    def header(self, name: str) -> str | None:
        return next((v for k, v in self.headers if k.lower() == name.lower()), None)


Responder = Callable[[SentRequest], Awaitable[HttpResponse] | HttpResponse]


@dataclass
class FakeTransport:
    """Answers by path (or by (method, path)); records every request. A responder may raise HttpError."""

    routes: dict[str, Responder | list[Responder]] = field(default_factory=dict)
    sent: list[SentRequest] = field(default_factory=list)

    async def send(
        self, method: str, url: str, headers: Sequence[tuple[str, str]], body: bytes | None, *, timeout_s: float, max_bytes: int
    ) -> HttpResponse:
        request = SentRequest(method, url, list(headers), body, timeout_s)
        self.sent.append(request)
        route = self.routes.get(f"{method} {request.path}") or self.routes.get(request.path)
        if route is None:
            raise AssertionError(f"unexpected request {method} {request.path}")
        if isinstance(route, list):
            if not route:
                raise AssertionError(f"no scripted response left for {request.path}")
            responder = route.pop(0)
        else:
            responder = route
        answer = responder(request)
        response = await answer if asyncio.iscoroutine(answer) else answer
        assert isinstance(response, HttpResponse)
        if len(response.body) > max_bytes:
            from orion_agent.tools.http import HttpBodyTooLarge

            raise HttpBodyTooLarge("over the cap")
        return response

    def paths(self) -> list[str]:
        return [r.path for r in self.sent]


def fixed(status: int, body: Any) -> Responder:
    return lambda _request: json_response(status, body)


def raises(error: HttpError) -> Responder:
    def respond(_request: SentRequest) -> HttpResponse:
        raise error

    return respond


def obj(value: object) -> dict[str, Any]:
    """`value` as a JSON object, for indexing in assertions."""
    assert isinstance(value, dict), value
    return value


def arr(value: object) -> list[Any]:
    """`value` as a JSON array, for indexing in assertions."""
    assert isinstance(value, list), value
    return value


def vlab(transport: FakeTransport, origin: str = ORIGIN, **kwargs: Any) -> VlabClient:
    return VlabClient(origin, transport, **kwargs)


class ManualClock:
    """Monotonic time that only moves when told to (or when `sleep` is awaited)."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.wall = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def now(self) -> datetime:
        return self.wall + timedelta(seconds=self.t - 1000.0)

    def advance(self, seconds: float) -> None:
        self.t += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)

    def clock(self) -> Clock:
        return Clock(now=self.now, monotonic=self.monotonic, sleep=self.sleep)


# --- A synthetic stack: twelve cases, enough for every tool's shape ---------------------------------

CASES = [f"SYNTH-CASE-{i:04d}" for i in range(1, 13)]
MUTATED = ["SYNTH-CASE-0002", "SYNTH-CASE-0005", "SYNTH-CASE-0007", "SYNTH-CASE-0011"]


def guppy(count: int) -> Responder:
    return fixed(200, {"data": {"CaseCentric__aggregation": {"case_centric": {"_totalCount": count}}}})


def cases_pages(ids: Sequence[str]) -> Responder:
    def respond(request: SentRequest) -> HttpResponse:
        body = request.json
        assert isinstance(body, dict)
        offset, size = int(str(body["offset"])), int(str(body["size"]))
        page = [{"case_id": case} for case in ids[offset : offset + size]]
        return json_response(200, {"data": page, "pagination": {"total": len(ids), "size": size, "offset": offset}})

    return respond


def top_genes(rows: Sequence[tuple[str, int]]) -> Responder:
    def respond(request: SentRequest) -> HttpResponse:
        body = request.json
        assert isinstance(body, dict)
        limit = int(str(body["maxGenes"]))
        genes = [{"gene": g, "mutationStat": [{"dt": 1, "count": n}]} for g, n in rows[:limit]]
        return json_response(200, {"genes": genes})

    return respond


def donors(spec: Sequence[tuple[int, bool, float]]) -> list[dict[str, Any]]:
    """(time, censored, survivalEstimate) per donor, as Gen3 Analysis lists them."""
    return [
        {"time": t, "id": f"SYNTH-CASE-{i + 1:04d}", "submitter_id": f"SYNTH-CASE-{i + 1:04d}", "project_id": "SYNTHETIC-MM",
         "survivalEstimate": s, "censored": c}
        for i, (t, c, s) in enumerate(spec)
    ]


def de_body(names: Sequence[str], sizes: Sequence[int], method: str = "wilcoxon") -> dict[str, Any]:
    dots = [
        {"index": 3, "gene_name": "BRAF", "fold_change": -1.78, "original_p_value": 7.7e-135, "adjusted_p_value": 5.9e-131, "pixel_x": 41.0, "pixel_y": 5.0},
        {"index": 7, "gene_name": "ETV4", "fold_change": -1.76, "original_p_value": 1.2e-133, "adjusted_p_value": 4.6e-130, "pixel_x": 42.1, "pixel_y": 7.7},
        {"index": 9, "gene_name": "MYC", "fold_change": 0.91, "original_p_value": 1.0e-20, "adjusted_p_value": 2.0e-17, "pixel_x": 300.0, "pixel_y": 90.0},
    ]
    return {
        "data": {"dots": dots, "volcanoPng": "data:image/png;base64,AAAA", "plotExtent": {"xMin": -2.0}, "totalRows": 7684,
                 "totalSignificantRows": 3, "totalSignificantUp": 1, "totalSignificantDown": 2, "xOffset": 0, "centered": False,
                 "cacheId": "a04534ac6567948134d7534646e74f23"},
        "sample_size1": sizes[0], "sample_size2": sizes[1], "method": method,
    }


def fake_stack() -> FakeTransport:
    """Every endpoint the tools call, answered from the twelve synthetic cases."""

    def samples(request: SentRequest) -> HttpResponse:
        body = request.json
        assert isinstance(body, dict)
        filter0 = body["filter0"]
        assert isinstance(filter0, dict)
        ids = filter0["and"][0]["in"]["case_id"]
        return json_response(200, [{"id": int(str(case)[-4:]), "name": f"SYNTH-SAMPLE-{str(case)[-4:]}"} for case in ids])

    def pre(request: SentRequest) -> HttpResponse:
        body = request.json
        assert isinstance(body, dict)
        groups = body["samplelst"]["groups"]
        return json_response(200, {"data": {g["name"]: len(g["values"]) for g in groups}})

    def de(request: SentRequest) -> HttpResponse:
        body = request.json
        assert isinstance(body, dict)
        if body.get("preAnalysis"):
            return pre(request)
        groups = body["samplelst"]["groups"]
        return json_response(200, de_body([g["name"] for g in groups], [len(g["values"]) for g in groups], str(body["method"])))

    def survival(request: SentRequest) -> HttpResponse:
        body = request.json
        assert isinstance(body, dict)
        filters = body["filters"]
        assert isinstance(filters, list)
        results = [{"meta": {"id": 1}, "donors": donors([(30, False, 1.0), (60, True, 0.8), (90, False, 0.8), (120, True, 0.4)])} for _ in filters]
        stats = {"pValue": 0.0123, "degreesFreedom": 1} if len(filters) > 1 else {}
        return json_response(200, {"results": results, "overallStats": stats})

    return FakeTransport(
        routes={
            "/sandbox/status": fixed(200, {"status": "OK", "synthetic": True, "cases": 12, "csrf": "synthetic-only"}),
            "/analysis/v0/_version": fixed(200, {"version": "1.0.3"}),
            "/protein-paint/healthcheck": fixed(
                200, {"status": "ok", "versionInfo": {"pkgver": "2.216.0", "codedate": "20261003", "launchdate": "today"}}
            ),
            "/protein-paint/termdb/topMutatedGenes": top_genes([("KRAS", 4), ("NRAS", 3), ("TP53", 3), ("DIS3", 1)]),
            "/guppy/graphql": guppy(7),
            "/analysis/v0/cases/": cases_pages(CASES),
            "/analysis/v0/genomic/gene_cases": fixed(200, list(reversed(MUTATED)) + [MUTATED[0]]),
            "/analysis/v0/survival/": survival,
            "/protein-paint/termdb": samples,
            "/protein-paint/termdb/DE": de,
        }
    )


# --- A local HTTP/1.1 server on 127.0.0.1 -----------------------------------------------------------


@dataclass
class Received:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Reply:
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    chunked: bool = False
    delay_s: float = 0.0
    close_early: bool = False  # send headers and part of the body, then close
    gzip_body: bool = False


Handler = Callable[[Received], Awaitable[Reply] | Reply]


class LocalServer:
    """One request per connection, then close. Records what it received."""

    def __init__(self, handler: Handler, *, ssl_context: Any = None) -> None:
        self._handler = handler
        self._ssl = ssl_context
        self.received: list[Received] = []
        self.closed_early: list[bool] = []
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.port = 0

    async def __aenter__(self) -> "LocalServer":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self._ssl)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await self._server.wait_closed()

    def url(self, path: str = "/", scheme: str = "http", host: str = "127.0.0.1") -> str:
        return f"{scheme}://{host}:{self.port}{path}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            received = Received(method, target, headers, body)
            self.received.append(received)
            answer = self._handler(received)
            reply = await answer if asyncio.iscoroutine(answer) else answer
            assert isinstance(reply, Reply)
            if reply.delay_s:
                await asyncio.sleep(reply.delay_s)
            payload = gzip.compress(reply.body) if reply.gzip_body else reply.body
            out = [f"HTTP/1.1 {reply.status} X", *(f"{k}: {v}" for k, v in reply.headers)]
            if reply.gzip_body:
                out.append("Content-Encoding: gzip")
            out.append("Transfer-Encoding: chunked" if reply.chunked else f"Content-Length: {len(payload)}")
            writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
            if reply.close_early:
                writer.write(payload[: max(1, len(payload) // 2)])
                await writer.drain()
                writer.transport.abort()
                return
            if reply.chunked:
                for i in range(0, len(payload), 7):
                    chunk = payload[i : i + 7]
                    writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                writer.write(b"0\r\n\r\n")
            else:
                writer.write(payload)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            self.closed_early.append(True)
        except asyncio.CancelledError:
            pass
        finally:
            writer.close()
            if task is not None:
                self._tasks.discard(task)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def live_stack_reachable(origin: str = LIVE_ORIGIN) -> bool:
    try:
        with urllib.request.urlopen(f"{origin}/sandbox/status", timeout=3) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False
