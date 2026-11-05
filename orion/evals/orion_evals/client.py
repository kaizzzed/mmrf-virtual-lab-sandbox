"""Drive the agent over HTTP: one POST per turn, the response read as SSE (contract "Wire format").

Two targets share one client:

- **agent** (mode L direct): `POST <url>/invocations` with the headers the BFF sends in mode L
  (`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: orion-<threadId>`, `traceparent`).
- **bff**: `POST <origin>/api/orion/agui`; a non-SSE answer is a transport error carrying the
  BFF's `{"error": {"code", "message"}}` body.

Run records come from a `RecordSource`: the agent's run directory (mode L volume), the BFF's
record and view routes, or none.
"""

import http.client
import json
import secrets
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from .sse import SseError, SseReader
from .wire import END_STATUSES, REJECTION_CODES, WAITING_STATUSES

DEFAULT_AGENT_URL: Final = "http://orion-agent:8080/invocations"
READ_TIMEOUT_S: Final = 90.0  # heartbeats arrive every 15 s while an invocation streams
TURN_TIMEOUT_S: Final = 900.0
POST_TERMINAL_GRACE_S: Final = 2.0

Target = Literal["agent", "bff"]
EventHook = Callable[[dict[str, Any], "TurnResult"], "str | None"]
CLOSE: Final = "close"


class Response(Protocol):
    status: int
    content_type: str

    def lines(self) -> Iterator[str]: ...

    def read_text(self) -> str: ...

    def set_timeout(self, seconds: float) -> None: ...

    def close(self) -> None: ...


class Transport(Protocol):
    def post(self, body: Mapping[str, Any]) -> Response: ...


@dataclass
class TurnResult:
    """Everything one invocation returned, in order, with timings in seconds from the POST."""

    action: str
    request: dict[str, Any]
    thread_id: str
    run_id: str
    started_wall: float
    events: list[dict[str, Any]] = field(default_factory=list)
    event_times: list[float] = field(default_factory=list)
    http_status: int | None = None
    content_type: str | None = None
    bff_error: dict[str, Any] | None = None
    transport_error: str | None = None
    disconnected: bool = False
    elapsed_s: float = 0.0
    oversized_frames: list[int] = field(default_factory=list)

    @property
    def terminal(self) -> dict[str, Any] | None:
        for event in reversed(self.events):
            if event["type"] in ("RUN_FINISHED", "RUN_ERROR"):
                return event
        return None

    @property
    def terminal_count(self) -> int:
        return sum(1 for e in self.events if e["type"] in ("RUN_FINISHED", "RUN_ERROR"))

    @property
    def status(self) -> str | None:
        """The terminal status: RUN_FINISHED's result.status, or "Failed"/"Rejected" for RUN_ERROR."""
        end = self.terminal
        if end is None:
            return None
        if end["type"] == "RUN_FINISHED":
            result = end.get("result") or {}
            return str(result.get("status"))
        return "Rejected" if end.get("code") in REJECTION_CODES else "Failed"

    @property
    def code(self) -> str | None:
        end = self.terminal
        if end is None:
            return None
        if end["type"] == "RUN_ERROR":
            return str(end.get("code"))
        code = (end.get("result") or {}).get("code")
        return None if code is None else str(code)

    @property
    def rejected(self) -> bool:
        end = self.terminal
        return bool(end and end["type"] == "RUN_ERROR" and end.get("code") in REJECTION_CODES)

    def snapshots(self, run_id: str | None = None) -> list[dict[str, Any]]:
        out = [e["snapshot"] for e in self.events if e["type"] == "STATE_SNAPSHOT"]
        return out if run_id is None else [s for s in out if s.get("runId") == run_id]

    def last_view(self, run_id: str | None = None) -> dict[str, Any] | None:
        views = self.snapshots(run_id or self.run_id)
        return max(views, key=lambda v: int(v.get("revision", 0))) if views else None

    def first_plan_s(self) -> float | None:
        """Seconds from the POST to the first snapshot of this run that carries a plan (NFR-01)."""
        for event, at in zip(self.events, self.event_times, strict=True):
            if event["type"] == "STATE_SNAPSHOT":
                snap = event["snapshot"]
                if snap.get("runId") == self.run_id and snap.get("plan") is not None:
                    return at
        return None

    def tool_call_args(self) -> dict[str, Any] | None:
        """The concatenated `save_cohorts` TOOL_CALL_ARGS deltas, parsed, or None."""
        deltas = [e.get("delta", "") for e in self.events if e["type"] == "TOOL_CALL_ARGS"]
        if not deltas:
            return None
        parsed = json.loads("".join(deltas))
        return parsed if isinstance(parsed, dict) else None

    def text_message(self) -> str | None:
        parts = [e.get("delta", "") for e in self.events if e["type"] == "TEXT_MESSAGE_CONTENT"]
        return "".join(parts) if parts else None

    def summary(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "threadId": self.thread_id,
            "runId": self.run_id,
            "status": self.status,
            "code": self.code,
            "httpStatus": self.http_status,
            "bffError": self.bff_error,
            "transportError": self.transport_error,
            "disconnected": self.disconnected,
            "events": len(self.events),
            "elapsedS": round(self.elapsed_s, 3),
            "firstPlanS": None if (t := self.first_plan_s()) is None else round(t, 3),
        }


# --- HTTP ---------------------------------------------------------------------------------


class _HttpResponse:
    def __init__(self, conn: http.client.HTTPConnection, response: http.client.HTTPResponse, sock: socket.socket | None) -> None:
        self._conn = conn
        self._response = response
        # http.client drops conn.sock once the response owns the stream, so keep the socket here.
        self._sock = sock
        self.status = response.status
        self.content_type = response.getheader("content-type", "") or ""

    def lines(self) -> Iterator[str]:
        while True:
            raw = self._response.readline()
            if not raw:
                return
            yield raw.decode("utf-8")

    def read_text(self) -> str:
        return self._response.read().decode("utf-8", errors="replace")

    def set_timeout(self, seconds: float) -> None:
        if self._sock is not None:
            try:
                self._sock.settimeout(seconds)
            except OSError:
                pass

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._conn.close()


def traceparent() -> str:
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


class HttpTransport:
    """POST a RunAgentInput to the agent (mode L) or to the BFF route and return the open response."""

    def __init__(self, url: str, target: Target = "agent", read_timeout_s: float = READ_TIMEOUT_S) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"not an http(s) URL: {url!r}")
        if parts.username or parts.password:
            raise ValueError("credentials never go in a URL")
        self.url = url
        self.target = target
        self._parts = parts
        self._timeout = read_timeout_s

    def post(self, body: Mapping[str, Any]) -> Response:
        parts = self._parts
        connection_type = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = connection_type(parts.hostname or "", parts.port, timeout=self._timeout)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "traceparent": traceparent()}
        if self.target == "agent":
            headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] = f"orion-{body['threadId']}"
        path = parts.path or "/"
        conn.request("POST", path + (f"?{parts.query}" if parts.query else ""), body=json.dumps(body).encode("utf-8"), headers=headers)
        sock = conn.sock
        return _HttpResponse(conn, conn.getresponse(), sock if isinstance(sock, socket.socket) else None)


class AgentClient:
    """Sends one turn and reads its stream to the end (or until a hook asks to close it)."""

    def __init__(self, transport: Transport, turn_timeout_s: float = TURN_TIMEOUT_S) -> None:
        self.transport = transport
        self.turn_timeout_s = turn_timeout_s
        self._lock = threading.Lock()
        self.turns: list[TurnResult] = []

    def turn(self, body: dict[str, Any], on_event: EventHook | None = None) -> TurnResult:
        orion = body["forwardedProps"]["orion"]
        result = TurnResult(str(orion.get("action")), body, body["threadId"], body["runId"], time.time())
        start = time.monotonic()
        try:
            response = self.transport.post(body)
        except OSError as error:
            result.transport_error = f"{type(error).__name__}: {error}"
            result.elapsed_s = time.monotonic() - start
            self._keep(result)
            return result
        result.http_status = response.status
        result.content_type = response.content_type
        try:
            if not response.content_type.startswith("text/event-stream"):
                text = response.read_text()
                result.transport_error = f"HTTP {response.status}, not an event stream"
                try:
                    parsed = json.loads(text)
                    body_error = parsed.get("error") if isinstance(parsed, dict) else None
                    result.bff_error = body_error if isinstance(body_error, dict) else None
                except json.JSONDecodeError:
                    result.bff_error = None
                return result
            reader = SseReader()
            lines = response.lines()
            while True:
                try:
                    line = next(lines)
                except StopIteration:
                    last = reader.close()
                    if last is not None:
                        result.events.append(last.event)
                        result.event_times.append(time.monotonic() - start)
                    break
                except TimeoutError:
                    if result.terminal is not None:
                        break  # the stream ended at its terminal event; the server kept the socket open
                    raise
                frame = reader.feed_line(line)
                if frame is None:
                    continue
                result.events.append(frame.event)
                result.event_times.append(time.monotonic() - start)
                if on_event is not None and on_event(frame.event, result) == CLOSE:
                    result.disconnected = True
                    break
                if frame.event["type"] in ("RUN_FINISHED", "RUN_ERROR"):
                    # Read on briefly so a second terminal event (a contract breach) is still seen.
                    response.set_timeout(POST_TERMINAL_GRACE_S)
                if time.monotonic() - start > self.turn_timeout_s:
                    result.transport_error = f"turn exceeded {self.turn_timeout_s:.0f} s"
                    break
            result.oversized_frames = list(reader.oversized)
        except (OSError, http.client.HTTPException, SseError) as error:
            result.transport_error = f"{type(error).__name__}: {error}"
        finally:
            response.close()
            result.elapsed_s = time.monotonic() - start
            self._keep(result)
        return result

    def _keep(self, result: TurnResult) -> None:
        with self._lock:
            self.turns.append(result)


# --- Run records ----------------------------------------------------------------------------


class RecordSource(Protocol):
    name: str

    def record(self, run_id: str, thread_id: str | None = None) -> dict[str, Any] | None: ...

    def view(self, run_id: str, thread_id: str | None = None) -> dict[str, Any] | None: ...

    def location(self, run_id: str) -> str | None: ...


class NoRecordSource:
    name = "none"

    def record(self, run_id: str, thread_id: str | None = None) -> dict[str, Any] | None:
        return None

    def view(self, run_id: str, thread_id: str | None = None) -> dict[str, Any] | None:
        return None

    def location(self, run_id: str) -> str | None:
        return None


class DirRecordSource:
    """`${ORION_RUN_DIR}/runs/<runId>.json`, mounted read-only (contract "Where records live")."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.name = f"dir:{run_dir}"

    def _path(self, run_id: str) -> Path:
        if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
            raise ValueError("a run id is 32 lowercase hex characters")
        return self.run_dir / "runs" / f"{run_id}.json"

    def record(self, run_id: str, thread_id: str | None = None) -> dict[str, Any] | None:
        path = self._path(run_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or (thread_id is not None and data.get("threadId") != thread_id):
            return None
        return data

    def view(self, run_id: str, thread_id: str | None = None) -> dict[str, Any] | None:
        """The stored runView. The BFF view route's expired-lease rewrite is not applied here."""
        record = self.record(run_id, thread_id)
        view = None if record is None else record.get("runView")
        return view if isinstance(view, dict) else None

    def location(self, run_id: str) -> str | None:
        return str(self._path(run_id))


class BffRecordSource:
    """`GET /api/orion/runs/<runId>/record` and `/view` on the fork's BFF."""

    def __init__(self, origin: str, timeout_s: float = 30.0) -> None:
        parts = urllib.parse.urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError("an http(s) origin without credentials")
        self.origin = origin.rstrip("/")
        self.name = f"bff:{self.origin}"
        self._parts = parts
        self._timeout = timeout_s

    def _get(self, path: str) -> dict[str, Any] | None:
        parts = self._parts
        connection_type = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = connection_type(parts.hostname or "", parts.port, timeout=self._timeout)
        try:
            conn.request("GET", path, headers={"Accept": "application/json"})
            response = conn.getresponse()
            text = response.read().decode("utf-8")
            if response.status != 200:
                return None
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            return None
        finally:
            conn.close()

    def _route(self, run_id: str, kind: str, thread_id: str | None) -> str:
        query = f"?threadId={thread_id}" if thread_id else ""
        return f"/api/orion/runs/{urllib.parse.quote(run_id)}/{kind}{query}"

    def record(self, run_id: str, thread_id: str | None = None) -> dict[str, Any] | None:
        return self._get(self._route(run_id, "record", thread_id))

    def view(self, run_id: str, thread_id: str | None = None) -> dict[str, Any] | None:
        return self._get(self._route(run_id, "view", thread_id))

    def location(self, run_id: str) -> str | None:
        return self.origin + self._route(run_id, "record", None)


def wait_settled(
    records: RecordSource,
    run_id: str,
    thread_id: str | None,
    timeout_s: float,
    poll_s: float = 2.0,
) -> dict[str, Any] | None:
    """Poll the view until the run rests (a waiting or end status) or the timeout passes."""
    deadline = time.monotonic() + timeout_s
    view: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        view = records.view(run_id, thread_id)
        if view is not None and view.get("status") in END_STATUSES | WAITING_STATUSES:
            return view
        time.sleep(poll_s)
    return view
