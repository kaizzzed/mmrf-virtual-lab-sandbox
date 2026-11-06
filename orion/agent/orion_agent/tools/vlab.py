"""Requests to the Virtual Lab through its identity gateway (`ORION_VLAB_ORIGIN`), one Provenance each.

The allow-list is the enforcement point until the Cedar policy lands (A4 "Security at sandbox
level"): only the (method, path) pairs of the catalog's request templates can be sent, as JSON
POSTs of their payload or payload-free GETs, to an origin of the local stack or a CloudFront
deployment. The Basic credential is attached only over https, or to a loopback host.

`requestSha256` hashes the canonical JSON of the payload sent (`null` for a payload-free GET:
models.Provenance requires a hash), `responseSha256` the canonical JSON of the parsed response
body, null when the body is not JSON. Both use canonical_json(portable=False), as record hashes do.

The body on the wire is the payload's own JSON, keys in the payload's order (`wire_json`), not the
canonical form: a golden-based payload keeps the order the native viewer sends. ProteinPaint keys
its DE result cache (`data.cacheId`) on the body as sent, key order included, so only then does the
viewer's identical request read the entry `de_run` made, as the viz card's request list states.
"""

import json
import re
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import JsonValue

from ..models import Provenance, canonical_json, sha256_hex
from .credentials import CredentialSource, NoCredential
from .http import HttpError, HttpResponse, HttpTransport
from .schemas import MAX_UPSTREAM_BYTES, TOOL_REGISTRY

Method = Literal["GET", "POST"]
LOCAL_HOSTS: Final = frozenset({"identity", "localhost", "127.0.0.1", "::1"})
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
_CLOUDFRONT: Final = re.compile(r"^[a-z0-9]+\.cloudfront\.net$")
# Every (method, path) a server tool may send: the catalog's request templates (schemas.py).
ALLOWED_REQUESTS: Final[frozenset[tuple[str, str]]] = frozenset(
    (template.method, template.path) for spec in TOOL_REGISTRY.values() for template in spec.requests
)


class OriginError(ValueError):
    """ORION_VLAB_ORIGIN is not the local stack or a CloudFront deployment."""


class VlabRequestError(Exception):
    """A request that got no response (timeout, connection, protocol or size), with its Provenance."""

    def __init__(self, cause: HttpError, provenance: Provenance) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.provenance = provenance


def wire_json(payload: JsonValue) -> str:
    """A request body as sent: compact JSON in the payload's own key order (as the browser's
    JSON.stringify writes it), non-ASCII unescaped, NaN and infinities refused."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _is_local(host: str) -> bool:
    return host in LOCAL_HOSTS or host.endswith(".localhost")


def validate_origin(url: str) -> str:
    """The origin without a trailing slash: http to the local stack, or https to a CloudFront host
    (the same rule as identity/access.mjs and scripts/sandbox_connection.py)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise OriginError("ORION_VLAB_ORIGIN must be a bare origin, with no credentials, path or query")
    if parts.scheme == "http" and _is_local(host):
        return url.rstrip("/")
    if parts.scheme == "https" and _CLOUDFRONT.match(host) and parts.port in (None, 443):
        return url.rstrip("/")
    raise OriginError("ORION_VLAB_ORIGIN must be the local stack over http or a CloudFront deployment over https")


@dataclass(frozen=True)
class Exchange:
    """One request's response, its parsed JSON body (or None), and the record's Provenance."""

    response: HttpResponse
    payload: JsonValue
    parsed: JsonValue
    is_json: bool
    provenance: Provenance


def _parse_json(body: bytes) -> tuple[bool, JsonValue]:
    if not body.strip():
        return False, None
    try:
        value: JsonValue = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        return False, None
    return True, value


def _reject_constant(name: str) -> JsonValue:
    raise ValueError(f"{name} is not JSON")


class VlabClient:
    """Sends allow-listed requests to one origin; every request, answered or not, yields a Provenance."""

    def __init__(
        self,
        origin: str,
        transport: HttpTransport,
        *,
        credential: CredentialSource | None = None,
        allow: Collection[tuple[str, str]] = ALLOWED_REQUESTS,
        max_bytes: int = MAX_UPSTREAM_BYTES,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.origin = validate_origin(origin)
        host = urlsplit(self.origin).hostname or ""
        self._may_send_credential = self.origin.startswith("https://") or host in LOOPBACK_HOSTS
        self._transport = transport
        self._credential = credential or NoCredential()
        self._allow = frozenset(allow)
        self._max_bytes = max_bytes
        self._monotonic = monotonic or time.monotonic

    def with_transport(self, transport: HttpTransport) -> "VlabClient":
        """The same origin, credential and allow-list over another transport (fault injection)."""
        return VlabClient(
            self.origin, transport, credential=self._credential, allow=self._allow, max_bytes=self._max_bytes, monotonic=self._monotonic
        )

    async def send(self, method: Method, path: str, payload: JsonValue, *, timeout_s: float) -> Exchange:
        """POST `payload` as JSON, or GET when `payload` is None. Raises VlabRequestError, with the
        request's Provenance, when no response arrived."""
        if (method, path) not in self._allow:
            raise PermissionError(f"{method} {path} is not an allow-listed tool request")
        if method == "GET" and payload is not None:
            raise ValueError("a GET carries no payload")
        body = None if method == "GET" else wire_json(payload).encode("utf-8")
        request_sha256 = sha256_hex(canonical_json(payload, portable=False))
        headers: list[tuple[str, str]] = [("Accept", "application/json")]
        if body is not None:
            headers.append(("Content-Type", "application/json"))
        authorization = await self._credential.authorization()
        if authorization is not None:
            if not self._may_send_credential:
                raise PermissionError("the Basic credential is sent only over https or to a loopback host")
            headers.append(("Authorization", authorization))
        started = self._monotonic()
        try:
            response = await self._transport.send(
                method, self.origin + path, headers, body, timeout_s=timeout_s, max_bytes=self._max_bytes
            )
        except HttpError as error:
            elapsed = max(0, round((self._monotonic() - started) * 1000))
            provenance = Provenance(
                method=method, path=path, request_sha256=request_sha256, http_status=None, response_sha256=None, elapsed_ms=elapsed
            )
            raise VlabRequestError(error, provenance) from error
        elapsed = max(0, round((self._monotonic() - started) * 1000))
        is_json, parsed = _parse_json(response.body)
        response_sha256 = sha256_hex(canonical_json(parsed, portable=False)) if is_json else None
        provenance = Provenance(
            method=method, path=path, request_sha256=request_sha256, http_status=response.status,
            response_sha256=response_sha256, elapsed_ms=elapsed,
        )
        return Exchange(response=response, payload=payload, parsed=parsed, is_json=is_json, provenance=provenance)

