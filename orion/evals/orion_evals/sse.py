"""An incremental reader for the contract's wire format ("Wire format").

Each event is one frame, `data: <compact JSON>` and a blank line. Readers ignore `event:`, `id:`
and `retry:` fields and `:` comment lines, and accept `\\r\\n`. A frame may carry several `data:`
lines, which join with a newline (the SSE rule). Every frame is at most 32 KB.
"""

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Final

FRAME_LIMIT_BYTES: Final = 32 * 1024


class SseError(ValueError):
    """A frame that is not one JSON event."""


@dataclass
class Frame:
    event: dict[str, Any]
    size: int


@dataclass
class SseReader:
    """Feed it decoded lines (or raw text) and collect events in order."""

    oversized: list[int] = field(default_factory=list)
    _data: list[str] = field(default_factory=list)
    _size: int = 0

    def feed_line(self, line: str) -> Frame | None:
        line = line.rstrip("\n").rstrip("\r")
        self._size += len(line.encode("utf-8")) + 1
        if line == "":
            if not self._data:
                self._size = 0
                return None
            text = "\n".join(self._data)
            size = self._size
            self._data, self._size = [], 0
            try:
                event = json.loads(text)
            except json.JSONDecodeError as error:
                raise SseError(f"a data frame is not JSON: {error}") from error
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise SseError("a data frame is not an AG-UI event")
            if size > FRAME_LIMIT_BYTES:
                self.oversized.append(size)
            return Frame(event, size)
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._data.append(value)
        return None

    def feed(self, text: str) -> Iterator[Frame]:
        for line in text.replace("\r\n", "\n").split("\n"):
            frame = self.feed_line(line)
            if frame is not None:
                yield frame

    def close(self) -> Frame | None:
        """A stream that ends without the blank line still yields its last frame."""
        return self.feed_line("") if self._data else None


def parse_events(lines: Iterable[str]) -> list[dict[str, Any]]:
    reader = SseReader()
    events = [frame.event for line in lines if (frame := reader.feed_line(line)) is not None]
    last = reader.close()
    if last is not None:
        events.append(last.event)
    return events


def parse_text(text: str) -> list[dict[str, Any]]:
    return parse_events(text.replace("\r\n", "\n").split("\n"))
