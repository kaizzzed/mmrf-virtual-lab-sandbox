"""The SSE reader follows the contract's wire format and reads every fixture stream."""

import unittest

from helpers import contracts_dir

from orion_evals.sse import FRAME_LIMIT_BYTES, SseError, SseReader, parse_text


class Reader(unittest.TestCase):
    def test_ignores_fields_and_comments_and_accepts_crlf(self) -> None:
        text = ': keep-alive\r\nevent: x\r\nid: 7\r\nretry: 1000\r\ndata: {"type":"RUN_STARTED","threadId":"a","runId":"b"}\r\n\r\ndata: {"type":"RUN_FINISHED"}\n\n'
        events = parse_text(text)
        self.assertEqual([e["type"] for e in events], ["RUN_STARTED", "RUN_FINISHED"])

    def test_joins_multiline_data_and_keeps_a_last_frame_without_blank_line(self) -> None:
        events = parse_text('data: {"type":\ndata: "CUSTOM"}\n\ndata: {"type":"RUN_ERROR","code":"X"}')
        self.assertEqual([e["type"] for e in events], ["CUSTOM", "RUN_ERROR"])

    def test_rejects_a_frame_that_is_not_an_event(self) -> None:
        with self.assertRaises(SseError):
            parse_text("data: not json\n\n")
        with self.assertRaises(SseError):
            parse_text('data: {"no":"type"}\n\n')

    def test_records_frames_over_32_kb(self) -> None:
        reader = SseReader()
        big = '{"type":"CUSTOM","value":"' + "x" * FRAME_LIMIT_BYTES + '"}'
        frames = list(reader.feed(f"data: {big}\n\n"))
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(reader.oversized), 1)

    def test_every_fixture_stream_parses_with_one_terminal_event(self) -> None:
        paths = sorted((contracts_dir() / "fixtures").glob("*.events.jsonl"))
        self.assertGreaterEqual(len(paths), 15)
        for path in paths:
            with self.subTest(path.name):
                reader = SseReader()
                frames = list(reader.feed(path.read_text(encoding="utf-8")))
                self.assertEqual(reader.oversized, [])
                self.assertEqual(frames[0].event["type"], "RUN_STARTED")
                terminals = [f for f in frames if f.event["type"] in ("RUN_FINISHED", "RUN_ERROR")]
                self.assertEqual(len(terminals), 1)
                self.assertIs(terminals[0], frames[-1])


if __name__ == "__main__":
    unittest.main()
