"""The one async HTTP client every tool request goes through (RFA rev 25, A2 "Tools", A4 timeout budget).

HTTP/1.1 on asyncio streams with h11 as the protocol engine. Both are already in the agent image
(h11 comes with uvicorn), so the tool layer adds no dependency, and the same code runs in the
Gateway Lambda. One connection per request (`Connection: close`): a tool call makes at most a
dozen requests, and a fresh connection cannot carry state from an aborted request into the next.

Every request runs under one deadline (`timeout_s`) that covers connect, TLS, the request and the
whole response, and honours asyncio cancellation at once: a cancelled request aborts its socket and
re-raises CancelledError. A response body is read up to `max_bytes`, after any Content-Encoding is
undone; more is HttpBodyTooLarge. This module never sees, logs or stores credentials beyond the
header values the caller passes in, and its errors never quote headers.
"""

import asyncio
import contextlib
import ssl
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urlsplit

import h11

USER_AGENT: Final = "orion-agent-tools/1"
READ_CHUNK_BYTES: Final = 64 * 1024
MAX_HEADER_BYTES: Final = 64 * 1024
CLOSE_GRACE_S: Final = 1.0
DEFAULT_PORTS: Final = {"http": 80, "https": 443}


class HttpError(Exception):
    """A request that ended without a complete HTTP response."""


class HttpTimeout(HttpError):
    """The deadline passed before the response ended (connect, TLS, send or read)."""


class HttpConnectError(HttpError):
    """The connection could not be opened, or TLS failed."""


class HttpProtocolError(HttpError):
    """The peer broke HTTP/1.1, closed early, or sent an encoding this client cannot read."""


class HttpBodyTooLarge(HttpError):
    """The response body (decoded) is larger than the caller's `max_bytes`."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]  # names lowercased, in received order
    body: bytes  # decoded (Content-Encoding undone)

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((value for key, value in self.headers if key == wanted), None)

    @property
    def media_type(self) -> str:
        value = self.header("content-type") or ""
        return value.split(";", 1)[0].strip().lower()


class HttpTransport(Protocol):
    """Sends one request and returns the full response. Raises HttpError subclasses only."""

    async def send(
        self,
        method: str,
        url: str,
        headers: Sequence[tuple[str, str]],
        body: bytes | None,
        *,
        timeout_s: float,
        max_bytes: int,
    ) -> HttpResponse: ...


@dataclass(frozen=True)
class Target:
    """Where a URL points: scheme, host, port and the request target (path plus query)."""

    scheme: str
    host: str
    port: int
    target: str

    @classmethod
    def parse(cls, url: str) -> "Target":
        parts = urlsplit(url)
        if parts.scheme not in DEFAULT_PORTS or not parts.hostname:
            raise ValueError("an http or https URL with a host is required")
        if parts.username or parts.password:
            raise ValueError("credentials never travel in a URL")
        try:
            port = parts.port or DEFAULT_PORTS[parts.scheme]
        except ValueError as error:
            raise ValueError("the URL's port is not a number") from error
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return cls(parts.scheme, parts.hostname, port, target)

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == DEFAULT_PORTS[self.scheme] else f"{host}:{self.port}"


def _decode(body: bytes, encoding: str | None, max_bytes: int) -> bytes:
    if encoding is None or encoding.strip().lower() in ("", "identity"):
        return body
    name = encoding.strip().lower()
    if name in ("gzip", "x-gzip"):
        wbits = 16 + zlib.MAX_WBITS
    elif name == "deflate":
        wbits = zlib.MAX_WBITS
    else:
        raise HttpProtocolError(f"unsupported Content-Encoding {name!r}")
    inflater = zlib.decompressobj(wbits)
    try:
        out = inflater.decompress(body, max_bytes + 1)
    except zlib.error as error:
        raise HttpProtocolError("the response body could not be decoded") from error
    if len(out) > max_bytes or inflater.unconsumed_tail:
        raise HttpBodyTooLarge(f"the decoded response is larger than {max_bytes} bytes")
    return out


class H11Transport:
    """HTTP/1.1 over asyncio streams. TLS for https, verified against the system trust store."""

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context

    def _tls(self) -> ssl.SSLContext:
        if self._ssl_context is None:
            self._ssl_context = ssl.create_default_context()
        return self._ssl_context

    async def send(
        self,
        method: str,
        url: str,
        headers: Sequence[tuple[str, str]],
        body: bytes | None,
        *,
        timeout_s: float,
        max_bytes: int,
    ) -> HttpResponse:
        target = Target.parse(url)
        if timeout_s <= 0:
            raise HttpTimeout("no time left for the request")
        try:
            async with asyncio.timeout(timeout_s):
                return await self._exchange(method, target, headers, body, max_bytes)
        except TimeoutError as error:
            raise HttpTimeout(f"no complete response within {timeout_s:.1f} s") from error

    async def _exchange(
        self, method: str, target: Target, headers: Sequence[tuple[str, str]], body: bytes | None, max_bytes: int
    ) -> HttpResponse:
        try:
            if target.scheme == "https":
                reader, writer = await asyncio.open_connection(
                    target.host, target.port, ssl=self._tls(), server_hostname=target.host
                )
            else:
                reader, writer = await asyncio.open_connection(target.host, target.port)
        except (OSError, ssl.SSLError) as error:
            if isinstance(error, TimeoutError):
                raise
            raise HttpConnectError(f"cannot connect to {target.host}:{target.port} ({type(error).__name__})") from error
        clean = False
        try:
            response = await self._roundtrip(reader, writer, method, target, headers, body, max_bytes)
            clean = True
            return response
        finally:
            if clean:
                writer.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), CLOSE_GRACE_S)
            else:
                writer.transport.abort()

    async def _roundtrip(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        target: Target,
        headers: Sequence[tuple[str, str]],
        body: bytes | None,
        max_bytes: int,
    ) -> HttpResponse:
        connection = h11.Connection(our_role=h11.CLIENT, max_incomplete_event_size=MAX_HEADER_BYTES)
        sent: list[tuple[str, str]] = [("Host", target.host_header), ("User-Agent", USER_AGENT), ("Connection", "close")]
        names = {name.lower() for name, _ in headers}
        if "accept-encoding" not in names:
            sent.append(("Accept-Encoding", "identity"))
        sent.extend((name, value) for name, value in headers if name.lower() not in ("host", "connection", "content-length"))
        if body is not None:
            sent.append(("Content-Length", str(len(body))))
        try:
            writer.write(connection.send(h11.Request(method=method, target=target.target, headers=sent)) or b"")
            if body:
                writer.write(connection.send(h11.Data(data=body)) or b"")
            writer.write(connection.send(h11.EndOfMessage()) or b"")
            await writer.drain()
        except (OSError, h11.LocalProtocolError) as error:
            if isinstance(error, TimeoutError):
                raise
            raise HttpProtocolError(f"the request could not be sent ({type(error).__name__})") from error
        status: int | None = None
        received: tuple[tuple[str, str], ...] = ()
        chunks: list[bytes] = []
        size = 0
        while True:
            try:
                event = connection.next_event()
            except h11.RemoteProtocolError as error:
                raise HttpProtocolError("the response broke HTTP/1.1") from error
            if event is h11.NEED_DATA:
                try:
                    data = await reader.read(READ_CHUNK_BYTES)
                except OSError as error:
                    if isinstance(error, TimeoutError):
                        raise
                    raise HttpProtocolError(f"the connection failed mid-response ({type(error).__name__})") from error
                connection.receive_data(data)
                continue
            if isinstance(event, h11.InformationalResponse):
                continue
            if isinstance(event, h11.Response):
                status = event.status_code
                received = tuple((bytes(k).decode("latin-1").lower(), bytes(v).decode("latin-1")) for k, v in event.headers)
                continue
            if isinstance(event, h11.Data):
                size += len(event.data)
                if size > max_bytes:
                    raise HttpBodyTooLarge(f"the response is larger than {max_bytes} bytes")
                chunks.append(bytes(event.data))
                continue
            if isinstance(event, h11.EndOfMessage):
                break
            raise HttpProtocolError("the connection closed before the response ended")
        if status is None:
            raise HttpProtocolError("no response status")
        response = HttpResponse(status=status, headers=received, body=b"")
        return HttpResponse(status=status, headers=received, body=_decode(b"".join(chunks), response.header("content-encoding"), max_bytes))
