"""The HTTP client against a local server: SSE turns, BFF errors, dropped streams, the record sources."""

import json
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from helpers import fixture_text, rewrite_ids

from orion_evals import wire
from orion_evals.client import CLOSE, AgentClient, BffRecordSource, DirRecordSource, HttpTransport, TurnResult, wait_settled

SEEN: list[dict[str, Any]] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        return None

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        SEEN.append({"path": self.path, "headers": dict(self.headers), "body": body})
        mode = body["forwardedProps"]["orion"].get("question", "")
        if mode == "busy":
            payload = json.dumps({"error": {"code": "SESSION_BUSY", "message": "an upstream call is open"}}).encode()
            self.send_response(409)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        text = rewrite_ids(fixture_text("j1-complete.events.jsonl"), body["threadId"], body["runId"])
        for frame in text.split("\n\n"):
            if frame.strip():
                self.wfile.write((frame + "\n\n").encode())
                self.wfile.flush()
        if mode == "linger":
            time.sleep(5)  # keep the socket open after the terminal event

    def do_GET(self) -> None:  # noqa: N802
        match = re.match(r"^/api/orion/runs/([0-9a-f]{32})/(view|record)", self.path)
        if not match:
            self.send_response(404)
            self.end_headers()
            return
        record = json.loads(fixture_text("run-record.j1-complete.json"))
        payload = json.dumps(record["runView"] if match.group(2) == "view" else record).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class Client(unittest.TestCase):
    server: ThreadingHTTPServer
    origin: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.origin = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def body(self, question: str) -> dict[str, Any]:
        return wire.run_agent_input(wire.new_id(), wire.new_id(), wire.ask(question, wire.all_cases_context(1000)), question=question)

    def test_a_turn_reads_the_stream_and_sends_the_mode_l_headers(self) -> None:
        client = AgentClient(HttpTransport(f"{self.origin}/invocations", "agent"))
        body = self.body("Compare survival")
        turn = client.turn(body)
        self.assertEqual(turn.status, "Complete")
        self.assertEqual(turn.terminal_count, 1)
        self.assertEqual(turn.snapshots()[-1]["status"], "Complete")
        self.assertIsNotNone(turn.first_plan_s())
        self.assertIsNone(turn.transport_error)
        headers = SEEN[-1]["headers"]
        self.assertEqual(headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"], f"orion-{body['threadId']}")
        self.assertRegex(headers["traceparent"], r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertEqual(SEEN[-1]["body"], body)

    def test_the_bff_target_sends_no_session_header(self) -> None:
        AgentClient(HttpTransport(f"{self.origin}/api/orion/agui", "bff")).turn(self.body("x"))
        self.assertNotIn("X-Amzn-Bedrock-AgentCore-Runtime-Session-Id", SEEN[-1]["headers"])

    def test_a_bff_error_is_a_transport_error_with_its_code(self) -> None:
        turn = AgentClient(HttpTransport(f"{self.origin}/api/orion/agui", "bff")).turn(self.body("busy"))
        self.assertEqual(turn.http_status, 409)
        self.assertEqual((turn.bff_error or {}).get("code"), "SESSION_BUSY")
        self.assertEqual(turn.events, [])
        self.assertIsNone(turn.status)

    def test_a_hook_can_drop_the_stream(self) -> None:
        client = AgentClient(HttpTransport(f"{self.origin}/invocations"))

        def hook(event: dict[str, Any], _turn: TurnResult) -> str | None:
            return CLOSE if event["type"] == "STEP_STARTED" else None

        turn = client.turn(self.body("x"), hook)
        self.assertTrue(turn.disconnected)
        self.assertIsNone(turn.terminal)

    def test_a_socket_held_open_after_the_terminal_event_ends_the_turn_quickly(self) -> None:
        started = time.monotonic()
        turn = AgentClient(HttpTransport(f"{self.origin}/invocations")).turn(self.body("linger"))
        self.assertEqual(turn.status, "Complete")
        self.assertLess(time.monotonic() - started, 4.5)

    def test_an_unreachable_agent_is_reported(self) -> None:
        turn = AgentClient(HttpTransport("http://127.0.0.1:9/invocations", read_timeout_s=2)).turn(self.body("x"))
        self.assertIsNone(turn.http_status)
        self.assertIsNotNone(turn.transport_error)

    def test_urls_with_credentials_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            HttpTransport("http://user:secret@orion-agent:8080/invocations")
        with self.assertRaises(ValueError):
            BffRecordSource("http://user:secret@frontend:3000")

    def test_the_bff_record_routes(self) -> None:
        records = BffRecordSource(self.origin)
        run_id = "725ff3c629bddfbbadaad7e5513f479b"
        record = records.record(run_id)
        self.assertEqual((record or {}).get("schema"), "orion.run-record/v1")
        view = wait_settled(records, run_id, None, timeout_s=5, poll_s=0.1)
        self.assertEqual((view or {}).get("status"), "Complete")


class DirRecords(unittest.TestCase):
    def test_reads_a_record_and_its_view_and_checks_the_thread(self) -> None:
        record = json.loads(fixture_text("run-record.j1-complete.json"))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "runs").mkdir()
            (Path(tmp) / "runs" / f"{record['runId']}.json").write_text(json.dumps(record))
            source = DirRecordSource(Path(tmp))
            self.assertEqual((source.record(record["runId"]) or {}).get("revision"), record["revision"])
            self.assertEqual((source.view(record["runId"]) or {}).get("status"), "Complete")
            self.assertIsNone(source.record(record["runId"], thread_id="0" * 32))
            self.assertIsNone(source.record("f" * 32))
            with self.assertRaises(ValueError):
                source.record("../etc/passwd")


if __name__ == "__main__":
    unittest.main()
