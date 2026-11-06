"""Fault injection for E11 (contract "Fault injection"; ORION_FAULTS, parsed and gated by settings.py).

A tool fault replaces that tool's HTTP calls with a transport that answers every request itself,
so the tool's normal error mapping, guards and the client's retry rules all run unchanged:

- `timeout`: each request fails as the 55 s HTTP timeout would, without waiting it out, so the step
  retries once and fails (TIMEOUT).
- `error`: HTTP 500 (HTTP_ERROR, not retried).
- `cache_busy`: HTTP 429 CACHE_BUSY (retried once after the 10-20 s backoff).
- `empty`: the endpoint's zero answer (a count of 0, no ids, no genes, no samples, no curve), so the
  tool returns its full Observed with status `empty` where its guards say so. The fingerprint's
  three version reads have no zero form: in-process they go to the stack (only its ranking is
  empty); through Gateway they fail.

settings.Settings already refuses ORION_FAULTS outside mode L and a test Runtime (ORION_TEST_RUNTIME=1).
kg_query makes no HTTP call, so a fault on it changes nothing.
"""

import json
from collections.abc import Sequence
from typing import Final
from urllib.parse import urlsplit

from pydantic import JsonValue

from .http import HttpConnectError, HttpResponse, HttpTimeout, HttpTransport

TOOL_FAULTS: Final = frozenset({"timeout", "error", "empty", "cache_busy"})
_JSON: Final = (("content-type", "application/json"),)


def _response(status: int, body: JsonValue) -> HttpResponse:
    return HttpResponse(status=status, headers=_JSON, body=json.dumps(body).encode("utf-8"))


def _request_json(body: bytes | None) -> dict[str, JsonValue]:
    if not body:
        return {}
    try:
        value = json.loads(body)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def empty_body(method: str, path: str, request: dict[str, JsonValue]) -> JsonValue | None:
    """The zero answer of an endpoint, shaped as the real one; None for a read with no zero form."""
    if path == "/guppy/graphql":
        return {"data": {"CaseCentric__aggregation": {"case_centric": {"_totalCount": 0}}}}
    if path == "/analysis/v0/cases/":
        return {"data": [], "pagination": {"total": 0, "size": request.get("size", 0), "offset": request.get("offset", 0)}}
    if path == "/analysis/v0/genomic/gene_cases":
        return []
    if path == "/analysis/v0/survival/":
        return {"results": [], "overallStats": {}}
    if path == "/protein-paint/termdb/topMutatedGenes":
        return {"genes": []}
    if path == "/protein-paint/termdb":
        return []
    if path == "/protein-paint/termdb/DE":
        samplelst = request.get("samplelst")
        listed = samplelst.get("groups") if isinstance(samplelst, dict) else None
        groups = [g for g in listed if isinstance(g, dict)] if isinstance(listed, list) else []
        if request.get("preAnalysis") is True:
            return {"data": {str(g.get("name")): 0 for g in groups}}
        sizes = [len(values) if isinstance(values := g.get("values"), list) else 0 for g in groups] + [0, 0]
        return {
            "method": request.get("method"), "sample_size1": sizes[0], "sample_size2": sizes[1],
            "data": {"dots": [], "totalRows": 0, "totalSignificantRows": 0, "totalSignificantUp": 0, "totalSignificantDown": 0, "cacheId": None},
        }
    return None


class FaultTransport:
    """Answers every request with the fault; `fallback` serves what the fault has no form for."""

    def __init__(self, kind: str, fallback: HttpTransport | None = None) -> None:
        if kind not in TOOL_FAULTS:
            raise ValueError(f"{kind!r} is not a tool fault")
        self.kind = kind
        self._fallback = fallback

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
        if self.kind == "timeout":
            raise HttpTimeout("ORION_FAULTS timeout: answered as the HTTP timeout, without waiting it out")
        if self.kind == "error":
            return _response(500, {"error": "ORION_FAULTS error"})
        if self.kind == "cache_busy":
            return _response(429, {"error": "CACHE_BUSY", "message": "ORION_FAULTS cache_busy"})
        zero = empty_body(method, urlsplit(url).path, _request_json(body))
        if zero is not None:
            return _response(200, zero)
        if self._fallback is None:
            raise HttpConnectError(f"ORION_FAULTS empty has no zero form for {method} {urlsplit(url).path}")
        return await self._fallback.send(method, url, headers, body, timeout_s=timeout_s, max_bytes=max_bytes)
