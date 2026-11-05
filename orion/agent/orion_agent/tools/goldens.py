"""The golden request bodies the server tools start from (orion/golden, RFA rev 25 A2 "Tools").

Goldens are request bodies captured once from the native viewers, so a tool and its chart send the
same request. A tool reads them from `ORION_GOLDEN_DIR` when that directory exists (the agent
image bakes them at /golden), else from `golden_snapshot.json` beside this module: the same
requests, copied verbatim with their `payloadSha256` (each payload in the viewer's own key order,
which the body sent keeps), so the Gateway Lambda and an image built
without the golden context still have them. Loading checks every payload against its recorded
`payloadSha256` (canonical JSON, keys sorted, no whitespace), so neither source can drift silently.

Refresh the snapshot after a golden capture, from orion/agent:

    python -m orion_agent.tools.goldens write ../golden     # rewrite golden_snapshot.json
    python -m orion_agent.tools.goldens check ../golden     # exit 1 if it differs from the goldens
"""

import argparse
import copy
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from ..models import canonical_json, sha256_hex
from .schemas import GOLDEN_DIR_ENV, TOOL_REGISTRY, RequestTemplate

SNAPSHOT_PATH: Final = Path(__file__).with_name("golden_snapshot.json")
SNAPSHOT_SCHEMA: Final = "orion.golden-snapshot/v1"
GOLDEN_SCHEMA: Final = "orion.golden/v1"
# The golden files the catalog's request templates name, and the request index each uses.
GOLDEN_REQUESTS: Final[tuple[tuple[str, int], ...]] = tuple(
    sorted({(t.golden, t.request_index) for spec in TOOL_REGISTRY.values() for t in spec.requests if t.golden is not None})
)


class GoldenError(RuntimeError):
    """A golden file is missing, malformed, or its payload does not match its payloadSha256."""


@dataclass(frozen=True)
class GoldenRequest:
    file: str
    index: int
    method: str
    path: str
    payload: JsonValue  # the request's query (a captured GET) or body (a POST)
    payload_sha256: str


def payload_sha256(payload: JsonValue) -> str:
    return sha256_hex(canonical_json(payload, portable=False))


def _request_from(file: str, index: int, raw: JsonValue) -> GoldenRequest:
    if not isinstance(raw, dict):
        raise GoldenError(f"{file} request {index} is not an object")
    query, body = raw.get("query"), raw.get("body")
    if (query is None) == (body is None):
        raise GoldenError(f"{file} request {index} needs exactly one of query and body")
    payload = query if query is not None else body
    method, path, recorded = raw.get("method"), raw.get("path"), raw.get("payloadSha256")
    if not isinstance(method, str) or not isinstance(path, str) or not isinstance(recorded, str):
        raise GoldenError(f"{file} request {index} lacks method, path or payloadSha256")
    if payload_sha256(payload) != recorded:
        raise GoldenError(f"{file} request {index}: the payload does not match its payloadSha256")
    return GoldenRequest(file=file, index=index, method=method, path=path, payload=payload, payload_sha256=recorded)


class GoldenStore:
    """The golden requests the tools use, keyed by (file, request index)."""

    def __init__(self, requests: Mapping[tuple[str, int], GoldenRequest], source: str) -> None:
        missing = [f"{file}#{index}" for file, index in GOLDEN_REQUESTS if (file, index) not in requests]
        if missing:
            raise GoldenError(f"{source} lacks the golden requests {', '.join(missing)}")
        self._requests = dict(requests)
        self.source = source

    @classmethod
    def from_dir(cls, directory: Path) -> "GoldenStore":
        requests: dict[tuple[str, int], GoldenRequest] = {}
        for file, index in GOLDEN_REQUESTS:
            path = directory / file
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise GoldenError(f"cannot read the golden {file} under {directory}") from error
            if not isinstance(document, dict) or document.get("schema") != GOLDEN_SCHEMA:
                raise GoldenError(f"{file} is not an {GOLDEN_SCHEMA} file")
            listed = document.get("requests")
            if not isinstance(listed, list) or index >= len(listed):
                raise GoldenError(f"{file} has no request {index}")
            requests[(file, index)] = _request_from(file, index, listed[index])
        return cls(requests, str(directory))

    @classmethod
    def from_snapshot(cls, path: Path = SNAPSHOT_PATH) -> "GoldenStore":
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise GoldenError(f"cannot read the golden snapshot {path.name}") from error
        if not isinstance(document, dict) or document.get("schema") != SNAPSHOT_SCHEMA:
            raise GoldenError(f"{path.name} is not an {SNAPSHOT_SCHEMA} file")
        listed = document.get("requests")
        if not isinstance(listed, list):
            raise GoldenError(f"{path.name} lists no requests")
        requests: dict[tuple[str, int], GoldenRequest] = {}
        for raw in listed:
            file = raw.get("file") if isinstance(raw, dict) else None
            index = raw.get("index") if isinstance(raw, dict) else None
            if not isinstance(file, str) or not isinstance(index, int) or isinstance(index, bool):
                raise GoldenError(f"{path.name} has a request without file and index")
            requests[(file, index)] = _request_from(file, index, raw)
        return cls(requests, path.name)

    @classmethod
    def load(cls, golden_dir: Path | None = None) -> "GoldenStore":
        """ORION_GOLDEN_DIR (or `golden_dir`) when the directory exists, else the packaged snapshot."""
        named = golden_dir or (Path(os.environ[GOLDEN_DIR_ENV]) if os.environ.get(GOLDEN_DIR_ENV) else None)
        if named is not None and named.is_dir():
            return cls.from_dir(named)
        return cls.from_snapshot()

    def request(self, file: str, index: int) -> GoldenRequest:
        try:
            return self._requests[(file, index)]
        except KeyError:
            raise GoldenError(f"no golden request {file}#{index}") from None

    def payload(self, template: RequestTemplate) -> JsonValue:
        """A fresh copy of the template's starting payload: its golden request's, or its inline body."""
        if template.golden is None:
            return copy.deepcopy(template.inline)
        return copy.deepcopy(self.request(template.golden, template.request_index).payload)

    def snapshot(self) -> dict[str, JsonValue]:
        requests: list[JsonValue] = []
        for file, index in GOLDEN_REQUESTS:
            request = self._requests[(file, index)]
            is_get = request.method == "GET"
            requests.append(
                {
                    "file": file, "index": index, "method": request.method, "path": request.path,
                    "query": request.payload if is_get else None, "body": None if is_get else request.payload,
                    "payloadSha256": request.payload_sha256,
                }
            )
        return {"schema": SNAPSHOT_SCHEMA, "source": "orion/golden (orion.golden/v1), verbatim", "requests": requests}


def snapshot_text(store: GoldenStore) -> str:
    """The snapshot file: one compact request per line, so a re-capture diffs request by request."""
    document = store.snapshot()
    listed = document["requests"]
    assert isinstance(listed, list)
    # Each entry's own keys sorted; the payload inside keeps the viewer's key order, which the
    # request body keeps on the wire (vlab.wire_json: ProteinPaint's DE cache key depends on it).
    lines = ",\n".join(
        json.dumps(dict(sorted(r.items())) if isinstance(r, dict) else r, separators=(",", ":"), ensure_ascii=False)
        for r in listed
    )
    head = {k: v for k, v in document.items() if k != "requests"}
    rest = json.dumps(head, sort_keys=True, separators=(",", ":"), ensure_ascii=False)[1:]
    return '{"requests":[\n' + lines + "\n]," + rest + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m orion_agent.tools.goldens", description="Refresh or check golden_snapshot.json")
    parser.add_argument("command", choices=("write", "check"))
    parser.add_argument("golden_dir", type=Path)
    args = parser.parse_args(argv)
    expected = snapshot_text(GoldenStore.from_dir(args.golden_dir))
    current = SNAPSHOT_PATH.read_text(encoding="utf-8") if SNAPSHOT_PATH.is_file() else ""
    if args.command == "check":
        if current != expected:
            print(f"{SNAPSHOT_PATH.name} differs from {args.golden_dir}; run: python -m orion_agent.tools.goldens write {args.golden_dir}")
            return 1
        print(f"{SNAPSHOT_PATH.name} matches {args.golden_dir}")
        return 0
    if current != expected:
        SNAPSHOT_PATH.write_text(expected, encoding="utf-8")
        print(f"wrote {SNAPSHOT_PATH.name} from {args.golden_dir}")
    else:
        print(f"{SNAPSHOT_PATH.name} unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
